"""Telegram bot: send a social-media link, get the video/images back fast."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import tempfile
from collections import OrderedDict
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    ReactionTypeEmoji,
)
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.media_group import MediaGroupBuilder

from .config import Settings, load_settings
from .downloader import (
    URL_RE,
    DownloadError,
    MediaItem,
    extract_urls,
    fetch_audio,
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


class LinkStore:
    """Remembers which link an 'Audio only' button belongs to (button data is limited to
    64 bytes, too short for a URL). Kept in memory, so buttons on old messages stop
    working after the bot restarts."""

    def __init__(self, size: int = 2000) -> None:
        self._data: OrderedDict[str, str] = OrderedDict()
        self._size = size

    def add(self, url: str) -> str:
        key = secrets.token_urlsafe(6)
        self._data[key] = url
        while len(self._data) > self._size:
            self._data.popitem(last=False)
        return key

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def pop(self, key: str) -> str | None:
        return self._data.pop(key, None)


AUDIO_WORD = re.compile(r"\b(audio|mp3)\b", re.IGNORECASE)


def wants_audio(text: str) -> bool:
    """'audio <link>', '<link> mp3' or '/audio <link>' -> True (the link itself is ignored)."""
    return bool(AUDIO_WORD.search(URL_RE.sub(" ", text or "")))


WELCOME = (
    "👋 <b>Send me a link</b> and I'll send the video or photos back.\n\n"
    "🎬 / 🎵 I'll ask whether you want the <b>Video</b> or just the <b>Audio</b>. "
    "To skip the question and get only the sound, add the word <b>audio</b> "
    "(<code>audio https://…</code>).\n\n"
    "Works with Instagram, TikTok, YouTube, Facebook, X/Twitter, Reddit, "
    "Pinterest, Threads, SwissTransfer and more.\n"
    "You can also add me to a group — I'll grab any link posted there."
)


# ------------------------------------------------------------------------ sending


def _file_id(msg: Message) -> tuple[str, str] | None:
    if msg.video:
        return "video", msg.video.file_id
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    if msg.audio:
        return "audio", msg.audio.file_id
    if msg.document:
        return "document", msg.document.file_id
    return None


async def _send_single(
    message: Message, kind: str, media, w, h, dur, title=None, performer=None, markup=None
) -> Message:
    if kind == "video":
        return await message.reply_video(
            media, width=w, height=h, duration=dur, supports_streaming=True, reply_markup=markup
        )
    if kind == "photo":
        return await message.reply_photo(media)
    if kind == "audio":
        return await message.reply_audio(media, duration=dur, title=title, performer=performer)
    return await message.reply_document(media)


def _pad(spec: tuple) -> tuple:
    """(kind, media, width, height, duration[, title, performer]) -> always 7 fields."""
    return tuple(spec) + (None,) * (7 - len(spec))


async def _send_all(
    message: Message,
    specs: list[tuple],
    *,
    from_cache: bool,
    markup: InlineKeyboardMarkup | None = None,
) -> list[Cached]:
    """Send items (videos/photos grouped in albums of up to 10). Returns file_ids."""
    sent: list[Cached] = []
    specs = [_pad(x) for x in specs]
    visual = [x for x in specs if x[0] in ("video", "photo")]
    singles = [x for x in specs if x[0] not in ("video", "photo")]  # documents, audio
    # Telegram albums can't carry buttons, so the button only goes on a lone video.
    button_ok = markup is not None and len(specs) == 1 and specs[0][0] == "video"

    async def run_group(chunk: list[tuple]) -> None:
        if len(chunk) == 1:
            kind, media, w, h, dur, title, performer = chunk[0]
            m = await _send_single(
                message, kind, media, w, h, dur, title, performer, markup if button_ok else None
            )
            fid = _file_id(m)
            if fid:
                sent.append((fid[0], fid[1], w, h, dur))
            return
        builder = MediaGroupBuilder()
        for kind, media, w, h, dur, *_ in chunk:
            if kind == "video":
                builder.add_video(
                    media=media, width=w, height=h, duration=dur, supports_streaming=True
                )
            else:
                builder.add_photo(media=media)
        msgs = await message.reply_media_group(media=builder.build())
        for m, (_, _, w, h, dur, *_rest) in zip(msgs, chunk):
            fid = _file_id(m)
            if fid:
                sent.append((fid[0], fid[1], w, h, dur))

    for i in range(0, len(visual), 10):
        await run_group(visual[i : i + 10])
    for item in singles:
        await run_group([item])
    return sent


def _nice_name(item: MediaItem) -> str | None:
    """Audio files are named after the track instead of the download id."""
    if item.kind == "audio" and item.title:
        base = " ".join(re.sub(r'[\\/:*?"<>|]+', " ", item.title).split())[:60]
        if base:
            return base + item.path.suffix
    return None


def _specs_from_items(items: list[MediaItem]) -> list[tuple]:
    return [
        (
            i.kind,
            FSInputFile(i.path, filename=_nice_name(i)),
            i.width,
            i.height,
            i.duration,
            i.title,
            i.performer,
        )
        for i in items
    ]


def _specs_from_cache(cached: list[Cached]) -> list[tuple]:
    return [(k, fid, w, h, d) for k, fid, w, h, d in cached]


def _audio_markup(
    links: LinkStore | None, url: str, platform: str, kinds: list[str]
) -> InlineKeyboardMarkup | None:
    """The 'Audio only' button: shown under a lone video that yt-dlp can also pull audio from."""
    if links is None or platform == "SwissTransfer" or kinds != ["video"]:
        return None
    button = InlineKeyboardButton(text="🎵 Audio only", callback_data=f"aud:{links.add(url)}")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def _choice_markup(links: LinkStore, url: str) -> InlineKeyboardMarkup:
    """The Video / Audio pop-up shown right after someone sends a link."""
    key = links.add(url)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 Video", callback_data=f"pick:v:{key}"),
                InlineKeyboardButton(text="🎵 Audio", callback_data=f"pick:a:{key}"),
            ]
        ]
    )


async def _ask_format(message: Message, url: str, links: LinkStore) -> None:
    await message.reply(
        f"What do you want from this link?\n<code>{html.escape(url[:80])}</code>",
        reply_markup=_choice_markup(links, url),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


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


async def _track_delivered(stats: Stats, uid: int | None, platform: str, audio: bool) -> None:
    await _track(stats, "delivered", uid, platform)
    if audio:
        await _track(stats, "audio", uid, platform)


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
    links: LinkStore | None = None,
    mode: str = "video",
    explicit: bool = False,
    user_id: int | None = None,
) -> None:
    """mode 'video' = the normal download; 'audio' = just the sound.
    explicit = the person asked for this specifically, so always show errors (even in groups).
    user_id overrides who is credited in stats (needed when `message` is the bot's own)."""
    bot = message.bot
    show_errors = explicit or message.chat.type == ChatType.PRIVATE
    uid = user_id if user_id is not None else _uid(message)
    platform = platform_of(url)
    audio = mode == "audio"
    key = f"audio:{url}" if audio else url

    # 1) Instant path: we've sent this exact link before.
    cached = cache.get(key)
    if cached:
        try:
            markup = None if audio else _audio_markup(links, url, platform, [c[0] for c in cached])
            await _send_all(message, _specs_from_cache(cached), from_cache=True, markup=markup)
            await _track_delivered(stats, uid, platform, audio)
            return
        except Exception:  # stale file_id -> fall through to a fresh download
            log.warning("cached file_id failed for %s", url, exc_info=True)

    # 2) Fresh download.
    async with sem:
        try:
            action = ChatActionSender.upload_voice if audio else ChatActionSender.upload_video
            async with action(bot=bot, chat_id=message.chat.id):
                with tempfile.TemporaryDirectory(prefix="dl_") as tmp:
                    fetch = fetch_audio if audio else fetch_media
                    items = await fetch(url, Path(tmp), settings)
                    markup = (
                        None
                        if audio
                        else _audio_markup(links, url, platform, [i.kind for i in items])
                    )
                    sent = await _send_all(
                        message, _specs_from_items(items), from_cache=False, markup=markup
                    )
                    if sent:
                        cache.put(key, sent)
            await _track_delivered(stats, uid, platform, audio)
        except DownloadError as e:
            log.info("download failed for %s: %s", url, e)
            await _track(stats, "failed", uid, platform)
            if show_errors:
                await message.reply(f"⚠️ {e}")
        except Exception:
            log.exception("unexpected error for %s", url)
            await _track(stats, "failed", uid, platform)
            if show_errors:
                await message.reply("⚠️ Something went wrong. Please try again in a moment.")


@router.message(F.text | F.caption)
async def on_message(
    message: Message,
    settings: Settings,
    sem: asyncio.Semaphore,
    cache: FileIdCache,
    stats: Stats,
    links: LinkStore,
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

    mode = "audio" if wants_audio(text) else "video"
    ask = mode == "video" and settings.ask_format
    # With the pop-up on, nothing is downloaded until the person picks. SwissTransfer files
    # have no audio to offer, so those are sent straight away.
    to_ask = [u for u in urls if ask and platform_of(u) != "SwissTransfer"]
    direct = [u for u in urls if u not in to_ask]
    # If the pop-up is off, a lone video carries an "Audio only" button instead.
    button_links = None if settings.ask_format else links

    for u in to_ask:
        try:
            await _ask_format(message, u, links)
        except Exception:
            log.warning("couldn't show the format pop-up; sending the video", exc_info=True)
            direct.append(u)
    await asyncio.gather(
        *(
            _handle_url(
                message, u, settings, sem, cache, stats, button_links, mode,
                explicit=(mode == "audio"),
            )
            for u in direct
        )
    )


@router.callback_query(F.data.startswith("pick:"))
async def on_format_pick(
    cb: CallbackQuery,
    settings: Settings,
    sem: asyncio.Semaphore,
    cache: FileIdCache,
    stats: Stats,
    links: LinkStore,
) -> None:
    if settings.allowed_users and cb.from_user.id not in settings.allowed_users:
        await cb.answer()
        return
    parts = (cb.data or "").split(":", 2)
    mode = {"v": "video", "a": "audio"}.get(parts[1]) if len(parts) == 3 else None
    # pop: one pop-up = one choice, so a double tap can't send the media twice.
    url = links.pop(parts[2]) if mode else None
    if not url or not isinstance(cb.message, Message):
        await cb.answer("That button has expired. Please send the link again.", show_alert=True)
        return
    await cb.answer("Getting the audio…" if mode == "audio" else "Getting the video…")
    await _touch(stats, cb.from_user.id)
    prompt = cb.message
    # Reply to the person's own message when we can, so the result sits under their link.
    target = prompt.reply_to_message if isinstance(prompt.reply_to_message, Message) else prompt
    if target is not prompt:  # (never delete the message we are about to reply to)
        try:
            await prompt.delete()
        except Exception:
            pass
    await _handle_url(
        target, url, settings, sem, cache, stats, None,
        mode=mode, explicit=True, user_id=cb.from_user.id,
    )


@router.callback_query(F.data.startswith("aud:"))
async def on_audio_button(
    cb: CallbackQuery,
    settings: Settings,
    sem: asyncio.Semaphore,
    cache: FileIdCache,
    stats: Stats,
    links: LinkStore,
) -> None:
    if settings.allowed_users and cb.from_user.id not in settings.allowed_users:
        await cb.answer()
        return
    url = links.get((cb.data or "")[4:])
    if not url or not isinstance(cb.message, Message):
        await cb.answer("That button has expired. Please send the link again.", show_alert=True)
        return
    await cb.answer("Getting the audio…")  # also stops the button's loading spinner
    await _touch(stats, cb.from_user.id)
    await _handle_url(
        cb.message, url, settings, sem, cache, stats, links,
        mode="audio", explicit=True, user_id=cb.from_user.id,
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
        links=LinkStore(),
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
    except KeyboardInterrupt:
        pass
    # A SystemExit (e.g. "BOT_TOKEN is missing") is deliberately NOT caught here, so its
    # message reaches the logs and the process exits with an error code.
