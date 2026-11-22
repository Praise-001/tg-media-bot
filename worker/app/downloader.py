"""Link detection and media downloading (yt-dlp first, gallery-dl fallback)."""
from __future__ import annotations

import asyncio
import logging
import re
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
    kind: str  # "video" | "photo" | "document"
    width: int | None = None
    height: int | None = None
    duration: int | None = None


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


def _format_selector(max_mb: int, low_quality: bool) -> str:
    # Prefer a single pre-merged mp4 (no ffmpeg merge step = fastest).
    if low_quality:
        return "bv*[height<=480]+ba/b[height<=480]/w"
    m = max_mb
    return (
        f"b[ext=mp4][filesize<?{m}M]/b[filesize<?{m}M]/"
        "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b"
    )


def _ytdlp_download(url: str, outdir: Path, s: Settings, low_quality: bool) -> list[MediaItem]:
    opts = {
        "outtmpl": str(outdir / "%(id)s_%(autonumber)s.%(ext)s"),
        "format": _format_selector(s.max_file_mb, low_quality),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlist_items": "1-10",  # carousels: first 10 items
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "concurrent_fragment_downloads": 8,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 15,
        "writethumbnail": False,
    }
    if s.cookies_file:
        opts["cookiefile"] = s.cookies_file

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
    return _collect(outdir, meta)


# ---------------------------------------------------------------------- gallery-dl


async def _gallerydl_download(url: str, outdir: Path, s: Settings) -> list[MediaItem]:
    cmd = [sys.executable, "-m", "gallery_dl", "-q", "-D", str(outdir), "--range", "1-10"]
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
    if proc.returncode not in (0, None):
        log.info("gallery-dl failed: %s", err.decode(errors="ignore")[-300:])
    return _collect(outdir, {})


# ---------------------------------------------------------------------- public API


def _clear(outdir: Path) -> None:
    for p in outdir.iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)


def _oversize(items: list[MediaItem], s: Settings) -> bool:
    return any(i.path.stat().st_size > s.max_file_bytes for i in items)


async def fetch_media(url: str, outdir: Path, s: Settings) -> list[MediaItem]:
    """Download everything attached to a link. Raises DownloadError on failure."""
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
            return items
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
    if "login" in msg.lower() or "cookies" in msg.lower() or "private" in msg.lower():
        raise DownloadError(
            "That post needs a login (private, age-restricted, or the site is blocking the server)."
        )
    raise DownloadError("I couldn't get any media from that link.")
