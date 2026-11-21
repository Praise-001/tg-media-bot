"""Telegram bot: send a social-media link, get the video/images back fast."""
from __future__ import annotations

import asyncio
import logging
import tempfile
from collections import OrderedDict
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message, ReactionTypeEmoji
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.media_group import MediaGroupBuilder

from .config import Settings, load_settings
from .downloader import DownloadError, MediaItem, extract_urls, fetch_media, is_supported

log = logging.getLogger("bot")
router = Router()

# url -> [(kind, telegram_file_id, width, height, duration)]
# A repeat link is re-sent from Telegram's own servers: instant, no download.
Cached = tuple[str, str, int | None, int | None, int | None]


class FileIdCache:
    def __init__(self, size: int) -> None:
        self._data: OrderedDict[str, list[Cached]] = OrderedDict()
        self._size = size

    def get(self, key: str) -> list[Cached] | None:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return None

    def put(self, key: str, value: list[Cached]) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._size:
            self._data.popitem(last=False)


WELCOME = (
    "👋 <b>Send me a link</b> and I'll send the video or photos back.\n\n"
    "Works with Instagram, TikTok, YouTube, Facebook, X/Twitter, Reddit, "
    "Pinterest, Threads and more.\n"
    "You can also add me to a group — I'll grab any link posted there."
)


# ------------------------------------------------------------------------ sending


def _file_id(msg: Message) -> tuple[str, str] | None:
    if msg.video:
        return "video", msg.video.file_id
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    if msg.document:
        return "document", msg.document.file_id
    return None


async def _send_single(message: Message, kind: str, media, w, h, dur) -> Message:
    if kind == "video":
        return await message.reply_video(
            media, width=w, height=h, duration=dur, supports_streaming=True
        )
    if kind == "photo":
        return await message.reply_photo(media)
    return await message.reply_document(media)


async def _send_all(
    message: Message, specs: list[Cached | tuple], *, from_cache: bool
) -> list[Cached]:
    """Send items (videos/photos grouped in albums of up to 10). Returns file_ids."""
    sent: list[Cached] = []
    visual = [x for x in specs if x[0] in ("video", "photo")]
    docs = [x for x in specs if x[0] == "document"]

    async def run_group(chunk: list) -> None:
        if len(chunk) == 1:
            kind, media, w, h, dur = chunk[0]
            m = await _send_single(message, kind, media, w, h, dur)
            fid = _file_id(m)
            if fid:
                sent.append((fid[0], fid[1], w, h, dur))
            return
        builder = MediaGroupBuilder()
        for kind, media, w, h, dur in chunk:
            if kind == "video":
                builder.add_video(
                    media=media, width=w, height=h, duration=dur, supports_streaming=True
                )
            else:
                builder.add_photo(media=media)
        msgs = await message.reply_media_group(media=builder.build())
        for m, (_, _, w, h, dur) in zip(msgs, chunk):
            fid = _file_id(m)
            if fid:
                sent.append((fid[0], fid[1], w, h, dur))

    for i in range(0, len(visual), 10):
        await run_group(visual[i : i + 10])
    for d in docs:
        await run_group([d])
    return sent


def _specs_from_items(items: list[MediaItem]) -> list[tuple]:
    return [
        (i.kind, FSInputFile(i.path), i.width, i.height, i.duration) for i in items
    ]


def _specs_from_cache(cached: list[Cached]) -> list[tuple]:
    return [(k, fid, w, h, d) for k, fid, w, h, d in cached]


# ---------------------------------------------------------------------- handlers


@router.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME)


async def _handle_url(
    message: Message,
    url: str,
    settings: Settings,
    sem: asyncio.Semaphore,
    cache: FileIdCache,
) -> None:
    bot = message.bot
    private = message.chat.type == ChatType.PRIVATE

    # 1) Instant path: we've sent this exact link before.
    cached = cache.get(url)
    if cached:
        try:
            await _send_all(message, _specs_from_cache(cached), from_cache=True)
            return
        except Exception:  # stale file_id -> fall through to a fresh download
            log.warning("cached file_id failed for %s", url, exc_info=True)

    # 2) Fresh download.
    async with sem:
        try:
            async with ChatActionSender.upload_video(bot=bot, chat_id=message.chat.id):
                with tempfile.TemporaryDirectory(prefix="dl_") as tmp:
                    items = await fetch_media(url, Path(tmp), settings)
                    sent = await _send_all(
                        message, _specs_from_items(items), from_cache=False
                    )
                    if sent:
                        cache.put(url, sent)
        except DownloadError as e:
            log.info("download failed for %s: %s", url, e)
            if private:
                await message.reply(f"⚠️ {e}")
        except Exception:
            log.exception("unexpected error for %s", url)
            if private:
                await message.reply("⚠️ Something went wrong. Please try again in a moment.")


@router.message(F.text | F.caption)
async def on_message(
    message: Message, settings: Settings, sem: asyncio.Semaphore, cache: FileIdCache
) -> None:
    if settings.allowed_users and (
        not message.from_user or message.from_user.id not in settings.allowed_users
    ):
        return

    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    hidden = [e.url for e in entities if e.type == "text_link" and e.url]
    urls = [u for u in extract_urls(text, hidden) if is_supported(u, settings)][:3]

    if not urls:
        if message.chat.type == ChatType.PRIVATE and not text.startswith("/"):
            await message.reply("Send me a link from Instagram, TikTok, YouTube, Facebook, X…")
        return

    try:  # instant "I saw it" feedback
        await message.react([ReactionTypeEmoji(emoji="👀")])
    except Exception:
        pass

    await asyncio.gather(*(_handle_url(message, u, settings, sem, cache) for u in urls))


# -------------------------------------------------------------------------- main


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = load_settings()

    session = None
    if settings.api_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.api_url, is_local=True))
    bot = Bot(
        settings.token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(
        settings=settings,
        sem=asyncio.Semaphore(settings.max_concurrent),
        cache=FileIdCache(settings.cache_size),
    )
    dp.include_router(router)

    me = await bot.get_me()
    log.info("Starting @%s (id %s)", me.username, me.id)
    # An old build may have left a webhook on this bot; polling won't work until it's gone.
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
