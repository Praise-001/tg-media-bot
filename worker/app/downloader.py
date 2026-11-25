"""Link detection and media downloading (yt-dlp first, gallery-dl fallback)."""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from .config import Settings

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TRAILING_PUNCT = ".,;:!?)]}>'\""

# Hosts we accept by default (also matches any subdomain, e.g. vm.tiktok.com).
SUPPORTED_DOMAINS = (
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "fb.watch",
    "fb.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "threads.net",
    "threads.com",
    "pinterest.com",
    "pin.it",
    "reddit.com",
    "redd.it",
    "snapchat.com",
    "vimeo.com",
    "twitch.tv",
    "linkedin.com",
    "bsky.app",
    "swisstransfer.com",
    "vecteezy.com",
)

VIDEO_SEND_EXT = {".mp4", ".mov", ".m4v"}
VIDEO_OTHER_EXT = {".webm", ".mkv", ".avi"}
PHOTO_EXT = {".jpg", ".jpeg", ".png"}
OTHER_IMAGE_EXT = {".webp", ".gif", ".heic"}
PHOTO_MAX_BYTES = 10 * 1024 * 1024  # Telegram's photo upload limit


class DownloadError(Exception):
    """Raised with a message that is safe to show to the user."""


class TooLargeError(DownloadError):
    pass


@dataclass
class MediaItem:
    path: Path
    kind: str  # "video" | "photo" | "document" | "audio"
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    title: str | None = None  # audio only
    performer: str | None = None  # audio only


# --------------------------------------------------------------------------- URLs


def extract_urls(text: str, extra: list[str] | None = None) -> list[str]:
    """Pull unique URLs out of free text, plus any hidden link entities."""
    found: list[str] = []
    for raw in URL_RE.findall(text or ""):
        found.append(raw.rstrip(TRAILING_PUNCT))
    found.extend(extra or [])
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


PLATFORM_DOMAINS = {
    "Instagram": ("instagram.com",),
    "TikTok": ("tiktok.com",),
    "YouTube": ("youtube.com", "youtu.be"),
    "Facebook": ("facebook.com", "fb.watch", "fb.com"),
    "X/Twitter": ("twitter.com", "x.com"),
    "Threads": ("threads.net", "threads.com"),
    "Pinterest": ("pinterest.com", "pin.it"),
    "Reddit": ("reddit.com", "redd.it"),
    "SwissTransfer": ("swisstransfer.com",),
    "Vecteezy": ("vecteezy.com",),
}


def platform_of(url: str) -> str:
    """Friendly site name for stats ('Other' if it isn't one we name)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "Other"
    for name, domains in PLATFORM_DOMAINS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return name
    return "Other"


def is_supported(url: str, settings: Settings) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if "/playlist" in parsed.path:  # don't pull whole playlists
        return False
    if settings.allow_any_domain:
        return True
    host = parsed.hostname.lower()
    return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)


# ------------------------------------------------------------------ classification


def _classify(path: Path, meta: dict | None) -> MediaItem | None:
    ext = path.suffix.lower()
    size = path.stat().st_size
    meta = meta or {}
    if ext in VIDEO_SEND_EXT:
        return MediaItem(
            path,
            "video",
            width=meta.get("width"),
            height=meta.get("height"),
            duration=int(meta["duration"]) if meta.get("duration") else None,
        )
    if ext in VIDEO_OTHER_EXT:
        return MediaItem(path, "document")
    if ext in PHOTO_EXT and size <= PHOTO_MAX_BYTES:
        return MediaItem(path, "photo")
    if ext in PHOTO_EXT | OTHER_IMAGE_EXT:
        return MediaItem(path, "document")
    return None  # .part, .json, subtitles, thumbnails, etc.


def _collect(outdir: Path, meta_by_path: dict[Path, dict]) -> list[MediaItem]:
    items: list[MediaItem] = []
    for p in sorted(outdir.iterdir()):
        if p.is_file():
            item = _classify(p, meta_by_path.get(p))
            if item:
                items.append(item)
    return items


# ------------------------------------------------------------------------- yt-dlp


def _ytdlp_common(url: str, s: Settings) -> dict:
    """Options every yt-dlp call shares: cookies, the YouTube extras, JS runtimes."""
    opts: dict = {
        # YouTube needs a JavaScript runtime to solve its player challenge. The Docker image
        # ships Deno; Node works too if that's what the host has. Missing ones are ignored.
        "js_runtimes": {"deno": {}, "node": {}},
    }
    if s.cookies_file:
        opts["cookiefile"] = s.cookies_file
    if platform_of(url) == "YouTube":
        proxy = getattr(s, "youtube_proxy", None)
        if proxy:
            opts["proxy"] = proxy
        pot = getattr(s, "pot_provider_url", None)
        if pot:  # needs the bgutil plugin (in requirements.txt) and a running provider server
            opts["extractor_args"] = {"youtubepot-bgutilhttp": {"base_url": [pot]}}
    return opts


def _format_selector(max_mb: int, low_quality: bool, platform: str = "") -> str:
    # Prefer a single pre-merged mp4 (no ffmpeg merge step = fastest).
    if low_quality:
        return "bv*[height<=480]+ba/b[height<=480]/w"
    m = max_mb
    if platform == "YouTube":
        # YouTube's ready-made mp4 is only 360p. Take 720p picture + sound and join them
        # (a quick copy, no re-encoding); the per-stream size filter keeps it near the limit,
        # and if it still comes out too big the caller retries at 480p.
        v = max(m - 8, 5)
        return (
            f"bv*[height<=720][ext=mp4][filesize<?{v}M]+ba[ext=m4a]/"
            f"b[ext=mp4][filesize<?{m}M]/b[filesize<?{m}M]/"
            "bv*[height<=720]+ba/b"
        )
    return (
        f"b[ext=mp4][filesize<?{m}M]/b[filesize<?{m}M]/"
        "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b"
    )


# Used when a pre-merged file turns out to be silent: take the best picture AND the best
# sound as separate streams and let ffmpeg join them.
MERGE_SELECTOR = "bv*[height<=720]+ba/bv*+ba/b"


def has_audio_stream(path: Path) -> bool | None:
    """True/False from ffprobe, or None if it can't be checked (then we assume it's fine)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return bool(r.stdout.strip())


def _silent_videos(items: list["MediaItem"]) -> list["MediaItem"]:
    return [i for i in items if i.kind == "video" and has_audio_stream(i.path) is False]


def _ytdlp_download(
    url: str, outdir: Path, s: Settings, low_quality: bool, fmt: str | None = None
) -> list[MediaItem]:
    opts = {
        "outtmpl": str(outdir / "%(id)s_%(autonumber)s.%(ext)s"),
        "format": fmt or _format_selector(s.max_file_mb, low_quality, platform_of(url)),
        "merge_output_format": "mp4",
        "noplaylist": True,
        # carousels: first 10 items. A YouTube link is always one video.
        "playlist_items": "1" if platform_of(url) == "YouTube" else "1-10",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "concurrent_fragment_downloads": 8,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 15,
        "writethumbnail": False,
    }
    opts.update(_ytdlp_common(url, s))

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    meta: dict[Path, dict] = {}
    entries = (info or {}).get("entries") or [info]
    for entry in entries:
        if not entry:
            continue
        for rd in entry.get("requested_downloads") or []:
            fp = rd.get("filepath")
            if fp:
                meta[Path(fp)] = entry
                log.info(
                    "yt-dlp picked format %s (vcodec=%s acodec=%s) for %s",
                    rd.get("format_id"), rd.get("vcodec"), rd.get("acodec"), url,
                )
    return _collect(outdir, meta)


# ---------------------------------------------------------------------------- audio

AUDIO_EXT = {".m4a", ".mp3"}  # the two formats Telegram accepts as "audio"


def _audio_format_selector(max_mb: int) -> str:
    # Prefer an m4a track as-is (no conversion = fast); otherwise take the best audio and convert.
    m = max_mb
    return f"ba[ext=m4a][filesize<?{m}M]/ba[filesize<?{m}M]/ba/b"


def _probe_duration(path: Path) -> int | None:
    """Length in whole seconds from the file itself (some sites don't report it). Best effort."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return round(float(out)) if out else None
    except Exception:
        return None


def _ytdlp_audio_download(url: str, outdir: Path, s: Settings) -> MediaItem:
    opts = {
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
        "format": _audio_format_selector(s.max_file_mb),
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "concurrent_fragment_downloads": 8,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 15,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "128"}
        ],
    }
    opts.update(_ytdlp_common(url, s))

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    entries = [e for e in ((info or {}).get("entries") or [info]) if e]
    meta = entries[0] if entries else {}
    files = sorted(p for p in outdir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT)
    if not files:
        raise DownloadError("I couldn't get any audio from that link.")
    title = (meta.get("track") or meta.get("title") or "").strip()
    performer = (meta.get("artist") or meta.get("uploader") or meta.get("channel") or "").strip()
    return MediaItem(
        files[0],
        "audio",
        duration=int(meta["duration"]) if meta.get("duration") else _probe_duration(files[0]),
        title=title[:64] or None,
        performer=performer[:64] or None,
    )


async def fetch_audio(url: str, outdir: Path, s: Settings) -> list[MediaItem]:
    """Download just the sound of a video link. Raises DownloadError on failure."""
    if platform_of(url) == "SwissTransfer":
        raise DownloadError("I can only pull audio out of video links, not SwissTransfer files.")
    try:
        item = await asyncio.wait_for(
            asyncio.to_thread(_ytdlp_audio_download, url, outdir, s),
            timeout=s.download_timeout,
        )
    except yt_dlp.utils.DownloadError as e:
        log.warning("yt-dlp audio failed for %s: %s", url, str(e)[:400])
        reason = explain_error(str(e))
        if reason:
            raise DownloadError(reason)
        if "audio codec" in str(e) or "no audio" in str(e).lower():
            raise DownloadError(
                "That video has no sound track I can download. It may be silent, "
                "or the site isn't giving the sound to the server."
            )
        raise DownloadError("I couldn't get the audio from that link.")
    except asyncio.TimeoutError:
        raise DownloadError("The download took too long.")
    if item.path.stat().st_size > s.max_file_bytes:
        raise TooLargeError(f"That audio is larger than Telegram's {s.max_file_mb} MB bot limit.")
    return [item]


# ---------------------------------------------------------------------- gallery-dl


async def _gallerydl_download(url: str, outdir: Path, s: Settings) -> list[MediaItem]:
    cmd = [sys.executable, "-m", "gallery_dl", "-D", str(outdir), "--range", "1-10"]
    if s.cookies_file:
        cmd += ["--cookies", s.cookies_file]
    cmd.append(url)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=s.download_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise DownloadError("The download took too long.")
    items = _collect(outdir, {})
    if not items:  # say why, so a failure can be diagnosed from the logs
        detail = err.decode(errors="ignore").strip()[-400:] or "no output"
        log.info("gallery-dl got nothing for %s (exit code %s): %s", url, proc.returncode, detail)
    return items


# ---------------------------------------------------------------------- public API


def _clear(outdir: Path) -> None:
    for p in outdir.iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)


def _oversize(items: list[MediaItem], s: Settings) -> bool:
    return any(i.path.stat().st_size > s.max_file_bytes for i in items)


def explain_error(msg: str) -> str | None:
    """A plain-language reason for the failures people actually hit, or None if unknown."""
    low = msg.lower()
    if "not a bot" in low:
        log.warning(
            "YouTube's bot check blocked this server. Fix: set YOUTUBE_PROXY, add YouTube "
            "cookies (COOKIES_B64) or run a PO-token server (POT_PROVIDER_URL)."
        )
        return "YouTube is blocking the bot's server right now. Please try again later."
    if "confirm your age" in low or "age-restricted" in low or "age restricted" in low:
        return "That video is age-restricted, so I can't download it."
    if "private video" in low:
        return "That video is private."
    if "members-only" in low or "members only" in low or "join this channel" in low:
        return "That video is for channel members only."
    if "live event" in low or "is live" in low or "live stream" in low:
        return "I can't download live streams. Try again once it has ended."
    if "video unavailable" in low or "this video is not available" in low or "has been removed" in low:
        return "That video isn't available (removed, private, or blocked where the bot runs)."
    if "requested format is not available" in low and "youtube" in low:
        log.warning(
            "YouTube gave no downloadable formats. Usually the JavaScript runtime (Deno) is "
            "missing or yt-dlp is out of date."
        )
    return None


async def _ensure_sound(url: str, items: list[MediaItem], outdir: Path, s: Settings) -> list[MediaItem]:
    """Some sites hand out a ready-made mp4 that has no sound. If a video has none, try once
    more with separate picture + sound streams. If that doesn't help, send what we have."""
    silent = _silent_videos(items)
    if not silent:
        return items
    log.info("video for %s has no audio stream; retrying with separate audio", url)
    retry_dir = outdir / "retry"
    retry_dir.mkdir(exist_ok=True)
    try:
        new = await asyncio.wait_for(
            asyncio.to_thread(_ytdlp_download, url, retry_dir, s, False, MERGE_SELECTOR),
            timeout=s.download_timeout,
        )
    except Exception as e:  # keep the silent video rather than failing the request
        log.info("retry with separate audio failed for %s: %s", url, str(e)[:300])
        return items
    if new and not _oversize(new, s) and not _silent_videos(new):
        for i in items:  # swap in the version that has sound
            i.path.unlink(missing_ok=True)
        return new
    log.warning("no audio track available for %s: the source itself seems to be silent", url)
    return items


async def fetch_media(url: str, outdir: Path, s: Settings) -> list[MediaItem]:
    """Download everything attached to a link. Raises DownloadError on failure."""
    if platform_of(url) == "SwissTransfer":  # not supported by yt-dlp; has its own downloader
        from .swisstransfer import fetch_swisstransfer

        return await fetch_swisstransfer(url, outdir, s)

    last_error: Exception | None = None

    # 1) yt-dlp: normal quality, then a smaller-quality retry if the file is too big.
    for low in (False, True):
        try:
            items = await asyncio.wait_for(
                asyncio.to_thread(_ytdlp_download, url, outdir, s, low),
                timeout=s.download_timeout,
            )
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            log.info("yt-dlp failed for %s: %s", url, str(e)[:300])
            break
        except asyncio.TimeoutError:
            raise DownloadError("The download took too long.")
        if items and not _oversize(items, s):
            return await _ensure_sound(url, items, outdir, s)
        if items:  # downloaded but too large -> retry lower quality once
            _clear(outdir)
            if low:
                raise TooLargeError(
                    f"That file is larger than Telegram's {s.max_file_mb} MB bot limit."
                )
        else:
            break

    # 2) gallery-dl fallback (image posts, carousels yt-dlp can't handle)
    _clear(outdir)
    items = await _gallerydl_download(url, outdir, s)
    if items:
        if _oversize(items, s):
            raise TooLargeError(
                f"That file is larger than Telegram's {s.max_file_mb} MB bot limit."
            )
        return items

    msg = str(last_error) if last_error else ""
    log.warning("no media for %s (yt-dlp: %s)", url, msg[:200] or "found nothing")
    reason = explain_error(msg)
    if reason:
        raise DownloadError(reason)
    if "login" in msg.lower() or "cookies" in msg.lower() or "private" in msg.lower():
        raise DownloadError(
            "That post needs a login (private, age-restricted, or the site is blocking the server)."
        )
    raise DownloadError("I couldn't get any media from that link.")
