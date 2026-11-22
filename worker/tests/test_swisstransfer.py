"""SwissTransfer tests run against a small fake SwissTransfer server (no internet needed)."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("BOT_TOKEN", "123:test")

import app.swisstransfer as st  # noqa: E402
from app.downloader import DownloadError, TooLargeError, fetch_media, is_supported  # noqa: E402
from app.config import load_settings  # noqa: E402

LINK = "https://www.swisstransfer.com/d/abcd1234-aaaa-bbbb"
SETTINGS = SimpleNamespace(max_file_mb=1, max_file_bytes=1024 * 1024, download_timeout=10)


def file_entry(n, name, body, size=None):
    return {
        "UUID": f"file-000{n}-aaaa",
        "fileName": name,
        "fileSizeInBytes": len(body) if size is None else size,
    }, body


def serve(monkeypatch, files, *, info_status=200, message=None, dl_status=200, allow_host=True, work):
    """Run `work()` while a fake SwissTransfer is up. `files` = [(entry, body), ...]."""
    bodies = {e["UUID"]: b for e, b in files}

    async def info(request):
        if info_status != 200:
            return web.Response(status=info_status)
        if message:
            return web.json_response({"result": "success", "data": {"message": message}})
        data = {
            "downloadHost": request.host,
            "linkUUID": request.match_info["id"],
            "container": {"UUID": "c-1", "files": [e for e, _ in files]},
        }
        return web.json_response({"result": "success", "data": data})

    async def download(request):
        if dl_status != 200:
            return web.Response(status=dl_status)
        return web.Response(body=bodies[request.match_info["file"]])

    async def go():
        app = web.Application()
        app.router.add_get("/api/links/{id}", info)
        app.router.add_get("/api/download/{link}/{file}", download)
        server = TestServer(app)
        await server.start_server()
        try:
            monkeypatch.setattr(st, "API_BASE", f"http://127.0.0.1:{server.port}/api")
            monkeypatch.setattr(st, "DOWNLOAD_SCHEME", "http")
            if allow_host:
                monkeypatch.setattr(st, "_host_ok", lambda h: True)  # tests talk to 127.0.0.1
            return await work()
        finally:
            await server.close()

    return asyncio.run(go())


def test_transfer_id_parsing():
    assert st.transfer_id("https://www.swisstransfer.com/d/abcd1234-aaaa-bbbb") == "abcd1234-aaaa-bbbb"
    assert st.transfer_id("https://swisstransfer.com/dl/abcd1234-aaaa?x=1") == "abcd1234-aaaa"
    assert st.transfer_id("https://www.swisstransfer.com/") is None
    assert st.transfer_id("https://www.swisstransfer.com/d/short") is None


def test_links_are_accepted():
    s = load_settings()
    assert is_supported(LINK, s)
    assert is_supported("https://www.vecteezy.com/video/12345-some-clip", s)


def test_downloads_files_safely(tmp_path, monkeypatch):
    files = [
        file_entry(1, "clip.mp4", b"v" * 2048),
        file_entry(2, "../../evil.txt", b"x" * 100),
        file_entry(3, "report.pdf", b"p" * 300),
        file_entry(4, "report.pdf", b"q" * 300),
    ]
    out = tmp_path / "out"
    out.mkdir()

    async def work():
        return await fetch_media(LINK, out, SETTINGS)  # goes through the normal entry point

    items = serve(monkeypatch, files, work=work)
    by_name = {i.path.name: i for i in items}
    assert set(by_name) == {"clip.mp4", "evil.txt", "report.pdf", "report_2.pdf"}
    assert by_name["clip.mp4"].kind == "video"
    assert by_name["report.pdf"].kind == "document"
    assert all(i.path.parent == out for i in items)  # nothing escaped the folder
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]


def test_oversized_transfer_gives_clear_error(tmp_path, monkeypatch):
    big = 5 * 1024 * 1024
    files = [file_entry(1, "movie.mp4", b"", size=big)]

    async def work():
        return await fetch_media(LINK, tmp_path, SETTINGS)

    with pytest.raises(TooLargeError, match=r"5\.0 MB"):
        serve(monkeypatch, files, work=work)


def test_oversized_files_skipped_when_others_fit(tmp_path, monkeypatch):
    files = [
        file_entry(1, "movie.mp4", b"", size=9 * 1024 * 1024),
        file_entry(2, "notes.txt", b"hello"),
    ]

    async def work():
        return await fetch_media(LINK, tmp_path, SETTINGS)

    items = serve(monkeypatch, files, work=work)
    assert [i.path.name for i in items] == ["notes.txt"]


def test_wrong_advertised_size_is_caught(tmp_path, monkeypatch):
    files = [file_entry(1, "liar.bin", b"z" * (2 * 1024 * 1024), size=10)]

    async def work():
        return await fetch_media(LINK, tmp_path, SETTINGS)

    with pytest.raises(TooLargeError):
        serve(monkeypatch, files, work=work)
    assert list(tmp_path.iterdir()) == []  # partial file cleaned up


def test_password_protected(tmp_path, monkeypatch):
    async def work():
        return await fetch_media(LINK, tmp_path, SETTINGS)

    with pytest.raises(DownloadError, match="password"):
        serve(monkeypatch, [], message="Transfer need a password", work=work)


def test_expired_link(tmp_path, monkeypatch):
    async def work():
        return await fetch_media(LINK, tmp_path, SETTINGS)

    with pytest.raises(DownloadError, match="expired"):
        serve(monkeypatch, [], info_status=404, work=work)


def test_download_limit_reached(tmp_path, monkeypatch):
    async def work():
        return await fetch_media(LINK, tmp_path, SETTINGS)

    with pytest.raises(DownloadError, match="download limit"):
        serve(monkeypatch, [file_entry(1, "a.txt", b"a")], dl_status=500, work=work)


def test_untrusted_download_host_is_refused(tmp_path, monkeypatch):
    async def work():
        return await fetch_media(LINK, tmp_path, SETTINGS)

    # The fake server advertises 127.0.0.1 as the download host, which must be rejected.
    with pytest.raises(DownloadError, match="unexpected"):
        serve(monkeypatch, [file_entry(1, "a.txt", b"a")], allow_host=False, work=work)
