"""YouTube support: options, format choice, error messages. (Live YouTube can't be tested offline.)"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yt_dlp

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("BOT_TOKEN", "123:test")

import app.downloader as dl  # noqa: E402

YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
SHORT = "https://youtube.com/shorts/abc123def45"
IG = "https://www.instagram.com/reel/abc/"


def _cfg(**over):
    base = dict(max_file_mb=50, max_file_bytes=50 << 20, download_timeout=30, cookies_file=None,
                youtube_proxy=None, pot_provider_url=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_youtube_links_are_recognised():
    for u in (YT, SHORT, "https://youtu.be/dQw4w9WgXcQ", "https://m.youtube.com/watch?v=x",
              "https://music.youtube.com/watch?v=x"):
        assert dl.platform_of(u) == "YouTube"
        assert dl.is_supported(u, _cfg(allow_any_domain=False))
    assert not dl.is_supported("https://www.youtube.com/playlist?list=PL123", _cfg(allow_any_domain=False))


def test_proxy_and_pot_server_apply_to_youtube_only():
    cfg = _cfg(youtube_proxy="http://user:pw@proxy:8000", pot_provider_url="http://pot:4416",
               cookies_file="/tmp/c.txt")
    yt = dl._ytdlp_common(YT, cfg)
    assert yt["proxy"] == "http://user:pw@proxy:8000"
    assert yt["extractor_args"] == {"youtubepot-bgutilhttp": {"base_url": ["http://pot:4416"]}}
    assert yt["cookiefile"] == "/tmp/c.txt"
    other = dl._ytdlp_common(IG, cfg)
    assert "proxy" not in other and "extractor_args" not in other  # Instagram traffic stays direct
    assert other["cookiefile"] == "/tmp/c.txt"


def test_nothing_extra_without_settings():
    opts = dl._ytdlp_common(YT, _cfg())
    assert "proxy" not in opts and "extractor_args" not in opts and "cookiefile" not in opts
    assert set(opts["js_runtimes"]) == {"deno", "node"}


def test_options_are_accepted_by_yt_dlp():
    opts = dl._ytdlp_common(YT, _cfg(youtube_proxy="http://p:1", pot_provider_url="http://pot:4416"))
    with yt_dlp.YoutubeDL({**opts, "quiet": True}) as ydl:  # would raise on a malformed option
        assert ydl.params["proxy"] == "http://p:1"


def test_youtube_asks_for_720p_picture_plus_sound():
    sel = dl._format_selector(50, False, "YouTube")
    assert sel.startswith("bv*[height<=720][ext=mp4]") and "+ba[ext=m4a]" in sel
    assert "filesize<?42M" in sel  # leaves room for the sound track under a 50 MB limit
    # the selector is valid for yt-dlp's parser
    with yt_dlp.YoutubeDL({"format": sel, "quiet": True}) as ydl:
        ydl.build_format_selector(sel)
    # other sites and the small retry keep their old behaviour
    assert dl._format_selector(50, False, "Instagram").startswith("b[ext=mp4]")
    assert dl._format_selector(50, True, "YouTube").startswith("bv*[height<=480]")


def test_youtube_picks_the_right_streams():
    """Run the real selector against a realistic YouTube format list."""
    def fmt(fid, ext, h=None, vc="none", ac="none", size=1_000_000):
        return {"format_id": fid, "ext": ext, "height": h, "vcodec": vc, "acodec": ac,
                "filesize": size, "protocol": "https", "url": "http://x/" + fid,
                "tbr": 100 if h is None else h}

    formats = [
        fmt("18", "mp4", 360, "avc1", "mp4a"),                  # the 360p ready-made file
        fmt("140", "m4a", None, "none", "mp4a"),                # sound only
        fmt("136", "mp4", 720, "avc1", size=20_000_000),        # 720p picture, fits
        fmt("137", "mp4", 1080, "avc1", size=60_000_000),       # 1080p, too big / too tall
        fmt("247", "webm", 720, "vp9", size=15_000_000),
    ]
    sel = dl._format_selector(50, False, "YouTube")
    with yt_dlp.YoutubeDL({"format": sel, "quiet": True}) as ydl:
        chosen = ydl.build_format_selector(sel)({"formats": formats, "has_merged_format": False})
        picked = next(iter(chosen))
    ids = [f["format_id"] for f in picked.get("requested_formats", [picked])]
    assert ids == ["136", "140"]

    # a long video whose 720p picture is huge falls back to the ready-made 360p file
    formats[2]["filesize"] = 200_000_000
    with yt_dlp.YoutubeDL({"format": sel, "quiet": True}) as ydl:
        chosen = ydl.build_format_selector(sel)({"formats": formats, "has_merged_format": False})
        picked = next(iter(chosen))
    ids = [f["format_id"] for f in picked.get("requested_formats", [picked])]
    assert ids == ["18"]


@pytest.mark.parametrize(
    "raw, expect",
    [
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies...", "blocking"),
        ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate", "age-restricted"),
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "private"),
        ("ERROR: [youtube] x: Join this channel to get access to members-only content", "members"),
        ("ERROR: [youtube] x: Video unavailable", "isn't available"),
        ("ERROR: [youtube] x: This live event will begin in 2 hours.", "live"),
    ],
)
def test_friendly_youtube_errors(raw, expect):
    assert expect in dl.explain_error(raw).lower()


def test_unknown_errors_stay_generic():
    assert dl.explain_error("ERROR: something odd") is None


def test_bot_check_reaches_the_user_as_a_clear_message(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise yt_dlp.utils.DownloadError("[youtube] x: Sign in to confirm you're not a bot")

    async def no_images(*a, **k):
        return []

    monkeypatch.setattr(dl, "_ytdlp_download", boom)
    monkeypatch.setattr(dl, "_gallerydl_download", no_images)
    with pytest.raises(dl.DownloadError) as e:
        asyncio.run(dl.fetch_media(YT, tmp_path, _cfg()))
    assert "blocking" in str(e.value)


def test_audio_bot_check_message(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise yt_dlp.utils.DownloadError("[youtube] x: Sign in to confirm you're not a bot")

    monkeypatch.setattr(dl, "_ytdlp_audio_download", boom)
    with pytest.raises(dl.DownloadError) as e:
        asyncio.run(dl.fetch_audio(YT, tmp_path, _cfg()))
    assert "blocking" in str(e.value)


def test_a_youtube_link_downloads_one_video_only(tmp_path, monkeypatch):
    seen = {}

    class FakeYDL:
        def __init__(self, opts):
            seen.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            return {"entries": []}

    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)
    dl._ytdlp_download(YT, tmp_path, _cfg(), False)
    assert seen["playlist_items"] == "1"
    assert "bv*[height<=720]" in seen["format"]
    seen.clear()
    dl._ytdlp_download(IG, tmp_path, _cfg(), False)
    assert seen["playlist_items"] == "1-10"
