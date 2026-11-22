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
from .downloader import (
    DownloadError,
    MediaItem,
    extract_urls,
    fetch_media,
    is_supported,
    platform_of,
)
from .stats import Stats, format_report

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


def _uid(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


# Stats must never get in the way of delivering media, so every write is guarded.
async def _touch(stats: Stats, user_id: int | None) -> None:
    if user_id is None:
        return
    try:
        await asyncio.to_thread(stats.touch, user_id)
    except Exception:
        log.warning("stats write failed", exc_info=True)


async def _track(
    stats: Stats, kind: str, user_id: int | None, platform: str | None = None
) -> None:
    try:
        await asyncio.to_thread(stats.record, kind, user_id, platform)
    except Exception:
        log.warning("stats write failed", exc_info=True)


@router.message(Command("start"))
async def cmd_start(message: Message, stats: Stats) -> None:
    uid = _uid(message)
    await _touch(stats, uid)
    await _track(stats, "start", uid)
    await message.answer(WELCOME)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(WELCOME)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    uid = _uid(message)
    if uid is not None:
        await message.answer(f"Your Telegram user ID: <code>{uid}</code>")


@router.message(Command("stats"))
async def cmd_stats(message: Message, settings: Settings, stats: Stats) -> None:
    uid = _uid(message)
    if not settings.admin_ids:
        await message.answer(
            "Stats aren't set up yet. Add your Telegram user ID "
            f"(<code>{uid}</code>) as <code>ADMIN_IDS</code> and restart the bot."
        )
        return
    if uid not in settings.admin_ids:
        return  # stay silent for everyone else
    snap = await asyncio.to_thread(stats.snapshot)
    await message.answer(format_report(snap))


async def _handle_url(
    message: Message,
    url: str,
    settings: Settings,
    sem: asyncio.Semaphore,
    cache: FileIdCache,
    stats: Stats,
) -> None:
    bot = message.bot
    private = message.chat.type == ChatType.PRIVATE
    uid = _uid(message)
    platform = platform_of(url)

    # 1) Instant path: we've sent this exact link before.
    cached = cache.get(url)
    if cached:
        try:
            await _send_all(message, _specs_from_cache(cached), from_cache=True)
            await _track(stats, "delivered", uid, platform)
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
            await _track(stats, "delivered", uid, platform)
        except DownloadError as e:
            log.info("download failed for %s: %s", url, e)
            await _track(stats, "failed", uid, platform)
            if private:
                await message.reply(f"⚠️ {e}")
        except Exception:
            log.exception("unexpected error for %s", url)
            await _track(stats, "failed", uid, platform)
            if private:
                await message.reply("⚠️ Something went wrong. Please try again in a moment.")


@router.message(F.text | F.caption)
async def on_message(
    message: Message,
    settings: Settings,
    sem: asyncio.Semaphore,
    cache: FileIdCache,
    stats: Stats,
) -> None:
    if settings.allowed_users and (
        not message.from_user or message.from_user.id not in settings.allowed_users
    ):
        return

    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    hidden = [e.url for e in entities if e.type == "text_link" and e.url]
    urls = [u for u in extract_urls(text, hidden) if is_supported(u, settings)][:3]

    # Count someone as a user when they send a link, or write to the bot privately
    # (not for ordinary chatter in a group).
    if urls or message.chat.type == ChatType.PRIVATE:
        await _touch(stats, _uid(message))

    if not urls:
        if message.chat.type == ChatType.PRIVATE and not text.startswith("/"):
            await message.reply("Send me a link from Instagram, TikTok, YouTube, Facebook, X…")
        return

    try:  # instant "I saw it" feedback
        await message.react([ReactionTypeEmoji(emoji="👀")])
    except Exception:
        pass

    await asyncio.gather(
        *(_handle_url(message, u, settings, sem, cache, stats) for u in urls)
    )


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

    try:
        stats = Stats(settings.stats_db_path)
    except Exception:
        log.exception(
            "Can't open the stats file at %s; stats will be kept in memory only",
            settings.stats_db_path,
        )
        stats = Stats(":memory:")

    dp = Dispatcher(
        settings=settings,
        sem=asyncio.Semaphore(settings.max_concurrent),
        cache=FileIdCache(settings.cache_size),
        stats=stats,
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
