"""SwissTransfer downloads (links like https://www.swisstransfer.com/d/<id>).

SwissTransfer is not supported by yt-dlp, so this talks to its web API directly:
  1. GET  <API_BASE>/links/<id>                      -> file list + the download host
  2. GET  https://<downloadHost>/api/download/<link>/<file>  -> the file itself

The request flow and the headers below were taken from the open-source `swish-dl`
client. SwissTransfer's API is unofficial/undocumented, so if it changes, this is
the one file to fix.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from .config import Settings
from .downloader import DownloadError, MediaItem, TooLargeError, _classify

log = logging.getLogger(__name__)

API_BASE = "https://www.swisstransfer.com/api"
DOWNLOAD_SCHEME = "https"
HEADERS = {
    "User-Agent": "swisstransfer-webext/1.0",
    "Cookie": "webext=1",
    "Referer": "swish/1.0.2",
}
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# The download host must belong to SwissTransfer's operator, never an arbitrary server.
ALLOWED_HOST_SUFFIXES = ("swisstransfer.com", "infomaniak.com", "infomaniak.ch")
MAX_FILES = 10
CHUNK = 256 * 1024

NEEDS_PASSWORD = "That transfer is password-protected, and I can't open those yet."
EXPIRED = "That SwissTransfer link wasn't found. It may have expired or been deleted."


def transfer_id(url: str) -> str | None:
    """'https://www.swisstransfer.com/d/<id>' (or /dl/<id>) -> '<id>'."""
    try:
        parts = [p for p in urlparse(url).path.split("/") if p]
    except ValueError:
        return None
    if len(parts) >= 2 and parts[0] in ("d", "dl") and ID_RE.match(parts[-1]):
        return parts[-1]
    return None


def _host_ok(host: object) -> bool:
    if not isinstance(host, str) or not host:
        return False
    h = host.lower().split(":")[0]
    return any(h == d or h.endswith("." + d) for d in ALLOWED_HOST_SUFFIXES)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _safe_name(name: str, used: set[str]) -> str:
    """Keep only the file name (no folders), drop odd characters, avoid clashes."""
    base = Path(str(name).replace("\\", "/")).name.strip().strip(".")
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "_", base)[:120] or "file"
    candidate, n = base, 1
    stem, dot, ext = base.rpartition(".")
    while candidate.lower() in used:
        n += 1
        candidate = f"{stem}_{n}.{ext}" if dot and stem else f"{base}_{n}"
    used.add(candidate.lower())
    return candidate


async def _get_info(session: aiohttp.ClientSession, tid: str) -> dict:
    async with session.get(f"{API_BASE}/links/{tid}") as resp:
        if resp.status in (404, 410):
            raise DownloadError(EXPIRED)
        if resp.status != 200:
            log.info("swisstransfer info HTTP %s", resp.status)
            raise DownloadError("SwissTransfer didn't accept that link right now. Try again later.")
        try:
            payload = await resp.json(content_type=None)
        except Exception:
            raise DownloadError("SwissTransfer sent a reply I couldn't read.")

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise DownloadError(EXPIRED)
    message = data.get("message")
    if message in ("Transfer need a password", "The password is wrong"):
        raise DownloadError(NEEDS_PASSWORD)
    if message == "All file are not finish to virus check":
        raise DownloadError("SwissTransfer is still scanning those files. Try again in a minute.")
    return data


async def _download_file(
    session: aiohttp.ClientSession, url: str, dest: Path, limit: int
) -> None:
    async with session.get(url, max_redirects=5) as resp:
        if resp.status == 500:  # what SwissTransfer returns once the download limit is used up
            raise DownloadError("That transfer has reached its download limit.")
        if resp.status in (404, 410):
            raise DownloadError(EXPIRED)
        if resp.status != 200:
            log.info("swisstransfer download HTTP %s", resp.status)
            raise DownloadError("SwissTransfer wouldn't let me download that file.")
        total = 0
        try:
            with dest.open("wb") as fh:
                async for chunk in resp.content.iter_chunked(CHUNK):
                    total += len(chunk)
                    if total > limit:  # the advertised size was wrong; stop at the limit
                        raise TooLargeError(
                            f"That file is larger than Telegram's {limit // (1024 * 1024)} MB bot limit."
                        )
                    fh.write(chunk)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise


async def fetch_swisstransfer(url: str, outdir: Path, s: Settings) -> list[MediaItem]:
    tid = transfer_id(url)
    if not tid:
        raise DownloadError("That doesn't look like a SwissTransfer download link.")

    timeout = aiohttp.ClientTimeout(total=s.download_timeout)
    try:
        async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
            data = await _get_info(session, tid)

            host = data.get("downloadHost")
            link_uuid = data.get("linkUUID")
            files = (data.get("container") or {}).get("files") or []
            if not _host_ok(host) or not isinstance(link_uuid, str) or not ID_RE.match(link_uuid):
                log.warning("unexpected swisstransfer response (host=%r)", host)
                raise DownloadError("SwissTransfer sent an unexpected reply for that link.")

            usable: list[dict] = []
            sizes: list[int] = []
            for f in files:
                if not isinstance(f, dict) or not ID_RE.match(str(f.get("UUID", ""))):
                    continue
                size = int(f.get("fileSizeInBytes") or 0)
                sizes.append(size)
                if size <= s.max_file_bytes:
                    usable.append(f)
            if not files:
                raise DownloadError("That transfer has no files in it.")
            if not usable:
                raise TooLargeError(
                    f"That transfer is {_human(max(sizes))}, more than the {s.max_file_mb} MB "
                    "Telegram lets a bot send."
                )
            if len(usable) < len(sizes):
                log.info("swisstransfer %s: skipping %d oversized file(s)", tid, len(sizes) - len(usable))

            items: list[MediaItem] = []
            used: set[str] = set()
            for f in usable[:MAX_FILES]:
                dest = outdir / _safe_name(f.get("fileName") or "file", used)
                file_url = f"{DOWNLOAD_SCHEME}://{host}/api/download/{link_uuid}/{f['UUID']}"
                await _download_file(session, file_url, dest, s.max_file_bytes)
                items.append(_classify(dest, None) or MediaItem(dest, "document"))
            return items
    except asyncio.TimeoutError:
        raise DownloadError("The download took too long.")
    except aiohttp.ClientError as e:
        log.info("swisstransfer network error: %s", e)
        raise DownloadError("I couldn't reach SwissTransfer right now.")
