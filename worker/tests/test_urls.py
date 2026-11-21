import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("BOT_TOKEN", "123:test")

from app.config import load_settings  # noqa: E402
from app.downloader import extract_urls, is_supported  # noqa: E402

S = load_settings()


def test_extracts_and_strips_punctuation():
    text = "look https://www.instagram.com/reel/ABC123/?igsh=x, and (https://youtu.be/abc)."
    assert extract_urls(text) == [
        "https://www.instagram.com/reel/ABC123/?igsh=x",
        "https://youtu.be/abc",
    ]


def test_dedupes_and_hidden_links():
    assert extract_urls("https://x.com/a/status/1", ["https://x.com/a/status/1", "https://vm.tiktok.com/Z/"]) == [
        "https://x.com/a/status/1",
        "https://vm.tiktok.com/Z/",
    ]


def test_supported_domains():
    ok = [
        "https://www.instagram.com/p/xyz/",
        "https://vm.tiktok.com/ZM123/",
        "https://m.youtube.com/watch?v=abc",
        "https://fb.watch/abc/",
        "https://www.facebook.com/reel/1",
        "https://x.com/u/status/1",
    ]
    assert all(is_supported(u, S) for u in ok)


def test_rejects_unknown_internal_and_playlists():
    bad = [
        "https://evil.example.com/video",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost:8080/x",
        "https://notinstagram.com/p/1",
        "https://www.youtube.com/playlist?list=PL123",
        "ftp://instagram.com/x",
    ]
    assert not any(is_supported(u, S) for u in bad)
