"""Audio-only option: real extraction with ffmpeg, plus the button / keyword / callback wiring."""
import asyncio
import functools
import http.server
import os
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("BOT_TOKEN", "123:test")

from aiogram.enums import ChatType  # noqa: E402

import app.downloader as dl  # noqa: E402
import app.main as m  # noqa: E402
from app.stats import Stats  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


# ------------------------------------------------------------ real extraction


@pytest.fixture
def served_clip(tmp_path):
    """A 2-second video with a sound track, served from a throwaway local web server."""
    root = tmp_path / "www"
    root.mkdir()
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(root / "clip.mp4")],
        check=True,
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/clip.mp4"
    srv.shutdown()


def _cfg(**over):
    base = dict(max_file_mb=50, max_file_bytes=50 * 1024 * 1024, download_timeout=60,
                cookies_file=None)
    base.update(over)
    return SimpleNamespace(**base)


@needs_ffmpeg
def test_extracts_audio_from_a_video_link(served_clip, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    items = asyncio.run(dl.fetch_audio(served_clip, out, _cfg()))
    assert len(items) == 1
    item = items[0]
    assert item.kind == "audio" and item.path.suffix == ".m4a"
    assert item.path.stat().st_size > 1000
    assert item.duration in (1, 2)
    # it really is audio-only, not the video renamed
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
         str(item.path)], capture_output=True, text=True, check=True,
    ).stdout.split()
    assert probe == ["audio"]


@needs_ffmpeg
def test_audio_too_big_is_refused(served_clip, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(dl.TooLargeError):
        asyncio.run(dl.fetch_audio(served_clip, out, _cfg(max_file_bytes=10)))


def test_audio_from_swisstransfer_is_refused(tmp_path):
    with pytest.raises(dl.DownloadError):
        asyncio.run(dl.fetch_audio("https://www.swisstransfer.com/d/abc", tmp_path, _cfg()))


def test_audio_failure_is_a_friendly_error(tmp_path):
    with pytest.raises(dl.DownloadError) as e:
        asyncio.run(dl.fetch_audio("http://127.0.0.1:1/none.mp4", tmp_path, _cfg()))
    assert "audio" in str(e.value)


# ------------------------------------------------------------ keyword detection


@pytest.mark.parametrize(
    "text, expected",
    [
        ("audio https://youtu.be/abc", True),
        ("https://youtu.be/abc mp3", True),
        ("MP3 please https://x.com/a/status/1", True),
        ("/audio https://youtu.be/abc", True),
        ("https://youtu.be/abc", False),
        ("https://example.com/audio/clip https://youtu.be/abc", False),  # word is inside a URL
        ("https://soundcloud.com/mp3-guy/track", False),
        ("check this out https://youtu.be/abc", False),
        ("", False),
    ],
)
def test_wants_audio(text, expected):
    assert m.wants_audio(text) is expected


# ------------------------------------------------------------ button + link store


def test_link_store_roundtrip_and_limit():
    s = m.LinkStore(size=2)
    a, b, c = s.add("https://a"), s.add("https://b"), s.add("https://c")
    assert s.get(a) is None  # oldest dropped
    assert s.get(b) == "https://b" and s.get(c) == "https://c"
    assert len(f"aud:{c}".encode()) <= 64


def test_button_only_under_a_single_video():
    links = m.LinkStore()
    ig = "https://www.instagram.com/reel/abc/"
    kb = m._audio_markup(links, ig, "Instagram", ["video"])
    assert kb.inline_keyboard[0][0].text == "🎵 Audio only"
    data = kb.inline_keyboard[0][0].callback_data
    assert data.startswith("aud:") and links.get(data[4:]) == ig
    assert m._audio_markup(links, ig, "Instagram", ["photo"]) is None
    assert m._audio_markup(links, ig, "Instagram", ["video", "video"]) is None
    assert m._audio_markup(links, ig, "Instagram", ["photo", "video"]) is None
    assert m._audio_markup(links, "https://www.swisstransfer.com/d/x", "SwissTransfer", ["video"]) is None
    assert m._audio_markup(None, ig, "Instagram", ["video"]) is None


# ------------------------------------------------------------ handler behaviour


def _msg(uid=7, chat_type=ChatType.PRIVATE, text=None):
    return SimpleNamespace(
        bot=None,
        chat=SimpleNamespace(type=chat_type, id=1),
        from_user=SimpleNamespace(id=uid),
        text=text, caption=None, entities=None, caption_entities=None,
        answer=AsyncMock(), reply=AsyncMock(), react=AsyncMock(),
    )


class _Action:
    used = []

    @classmethod
    def upload_video(cls, **kw):
        cls.used.append("video")
        return cls()

    @classmethod
    def upload_voice(cls, **kw):
        cls.used.append("voice")
        return cls()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _setup(monkeypatch, tmp_path):
    _Action.used = []
    monkeypatch.setattr(m, "ChatActionSender", _Action)
    calls = SimpleNamespace(video=0, audio=0, markups=[], specs=[])

    async def fake_media(*a, **k):
        calls.video += 1
        return [dl.MediaItem(tmp_path / "v.mp4", "video")]

    async def fake_audio(*a, **k):
        calls.audio += 1
        return [dl.MediaItem(tmp_path / "a.m4a", "audio", duration=5, title="Song", performer="Me")]

    async def fake_send(message, specs, *, from_cache, markup=None):
        calls.markups.append(markup)
        calls.specs.append(specs)
        kind = specs[0][0]
        return [(kind, "FID_" + kind, None, None, None)]

    monkeypatch.setattr(m, "fetch_media", fake_media)
    monkeypatch.setattr(m, "fetch_audio", fake_audio)
    monkeypatch.setattr(m, "_send_all", fake_send)
    return calls


def test_video_reply_carries_the_audio_button(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    stats = Stats(str(tmp_path / "s.db"))
    links, cache = m.LinkStore(), m.FileIdCache(10)
    url = "https://www.instagram.com/reel/abc/"

    async def run():
        await m._handle_url(_msg(), url, _cfg(), asyncio.Semaphore(1), cache, stats, links)
        await m._handle_url(_msg(), url, _cfg(), asyncio.Semaphore(1), cache, stats, links)

    asyncio.run(run())
    assert calls.video == 1 and calls.audio == 0  # second send came from the cache
    assert all(mk is not None for mk in calls.markups)  # button on fresh AND cached sends
    assert stats.snapshot().audio == 0


def test_audio_mode_sends_audio_only_and_counts_it(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    stats = Stats(str(tmp_path / "s.db"))
    links, cache = m.LinkStore(), m.FileIdCache(10)
    url = "https://www.instagram.com/reel/abc/"

    async def run():
        sem = asyncio.Semaphore(1)
        await m._handle_url(_msg(), url, _cfg(), sem, cache, stats, links, mode="audio", explicit=True)
        await m._handle_url(_msg(), url, _cfg(), sem, cache, stats, links, mode="audio", explicit=True)
        await m._handle_url(_msg(), url, _cfg(), sem, cache, stats, links)  # normal video still works

    asyncio.run(run())
    assert calls.audio == 1 and calls.video == 1  # audio repeat used its own cache entry
    assert cache.get("audio:" + url)[0][0] == "audio" and cache.get(url)[0][0] == "video"
    assert calls.markups[0] is None  # no button on an audio reply
    assert _Action.used[0] == "voice"
    snap = stats.snapshot()
    assert snap.audio == 2 and snap.delivered == 3


def test_audio_errors_are_shown_even_in_groups(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)

    async def boom(*a, **k):
        raise dl.DownloadError("I couldn't get the audio from that link.")

    monkeypatch.setattr(m, "fetch_audio", boom)
    stats = Stats(str(tmp_path / "s.db"))
    group = _msg(chat_type=ChatType.SUPERGROUP)
    plain = _msg(chat_type=ChatType.SUPERGROUP)
    url = "https://youtu.be/abc"

    async def run():
        sem, cache = asyncio.Semaphore(1), m.FileIdCache(5)
        await m._handle_url(group, url, _cfg(), sem, cache, stats, None, mode="audio", explicit=True)
        monkeypatch.setattr(m, "fetch_media", boom)
        await m._handle_url(plain, url, _cfg(), sem, cache, stats)  # ordinary group failure: quiet

    asyncio.run(run())
    group.reply.assert_called_once()
    plain.reply.assert_not_called()
    assert stats.snapshot().failed == 2 and stats.snapshot().audio == 0


def _on_message(msg, tmp_path, ask_format=False, links=None):
    stats = Stats(str(tmp_path / "s.db"))
    links, cache = links or m.LinkStore(), m.FileIdCache(10)
    cfg = SimpleNamespace(
        allowed_users=frozenset(), allow_any_domain=False, max_file_mb=50, max_file_bytes=50 << 20,
        download_timeout=60, cookies_file=None, ask_format=ask_format,
    )
    asyncio.run(m.on_message(msg, cfg, asyncio.Semaphore(2), cache, stats, links))
    return stats


def test_keyword_in_a_message_triggers_audio(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    stats = _on_message(_msg(text="audio https://www.instagram.com/reel/abc/"), tmp_path)
    assert calls.audio == 1 and calls.video == 0
    assert stats.snapshot().audio == 1


def test_slash_audio_command_triggers_audio(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    _on_message(_msg(text="/audio https://youtu.be/abc"), tmp_path)
    assert calls.audio == 1 and calls.video == 0


def test_plain_link_is_still_a_video(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    _on_message(_msg(text="https://www.instagram.com/reel/abc/"), tmp_path)
    assert calls.video == 1 and calls.audio == 0


# ------------------------------------------------------------ the button press


def _cb(data, uid=7, message=True):
    msg = _msg()
    cb = SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=uid), answer=AsyncMock(),
        message=msg if message else None,
    )
    return cb, msg


def _run_cb(cb, tmp_path, links, cfg=None, cache=None):
    stats = Stats(str(tmp_path / "s.db"))
    cfg = cfg or _cfg(allowed_users=frozenset())
    cache = cache or m.FileIdCache(10)
    asyncio.run(m.on_audio_button(cb, cfg, asyncio.Semaphore(1), cache, stats, links))
    return stats


def test_button_press_sends_audio_and_credits_the_presser(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    # the isinstance check wants a real Message; stand in for it
    monkeypatch.setattr(m, "Message", SimpleNamespace)
    links = m.LinkStore()
    key = links.add("https://www.instagram.com/reel/abc/")
    cb, msg = _cb(f"aud:{key}", uid=42)
    stats = _run_cb(cb, tmp_path, links)
    assert calls.audio == 1
    cb.answer.assert_called_once()
    snap = stats.snapshot()
    assert snap.audio == 1 and snap.users == 1  # credited to user 42, not the bot


def test_expired_button_says_so(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "Message", SimpleNamespace)
    cb, _ = _cb("aud:unknown")
    _run_cb(cb, tmp_path, m.LinkStore())
    assert calls.audio == 0
    args, kwargs = cb.answer.call_args
    assert "expired" in args[0] and kwargs.get("show_alert") is True


def test_button_respects_allowed_users(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "Message", SimpleNamespace)
    links = m.LinkStore()
    key = links.add("https://www.instagram.com/reel/abc/")
    cb, _ = _cb(f"aud:{key}", uid=99)
    _run_cb(cb, tmp_path, links, cfg=_cfg(allowed_users=frozenset({1})))
    assert calls.audio == 0


# ------------------------------------------------------------ real sending code


def _sent_msg(kind):
    ns = SimpleNamespace(video=None, photo=None, audio=None, document=None)
    if kind == "video":
        ns.video = SimpleNamespace(file_id="V1")
    elif kind == "audio":
        ns.audio = SimpleNamespace(file_id="A1")
    return ns


def _replying():
    msg = SimpleNamespace()
    msg.reply_video = AsyncMock(return_value=_sent_msg("video"))
    msg.reply_audio = AsyncMock(return_value=_sent_msg("audio"))
    msg.reply_photo = AsyncMock()
    msg.reply_document = AsyncMock()
    msg.reply_media_group = AsyncMock()
    return msg


def test_send_audio_uses_reply_audio_with_metadata_and_caches_the_file_id():
    msg = _replying()
    sent = asyncio.run(
        m._send_all(msg, [("audio", "MEDIA", None, None, 5, "Song", "Me")], from_cache=False)
    )
    msg.reply_audio.assert_called_once_with("MEDIA", duration=5, title="Song", performer="Me")
    assert sent == [("audio", "A1", None, None, 5)]


def test_cached_audio_resends_by_file_id():
    msg = _replying()
    asyncio.run(m._send_all(msg, m._specs_from_cache([("audio", "A1", None, None, 5)]), from_cache=True))
    msg.reply_audio.assert_called_once()


def test_button_is_attached_to_a_lone_video_only():
    kb = m._audio_markup(m.LinkStore(), "https://youtu.be/a", "YouTube", ["video"])
    one = _replying()
    asyncio.run(m._send_all(one, [("video", "M", 1, 2, 3)], from_cache=False, markup=kb))
    assert one.reply_video.call_args.kwargs["reply_markup"] is kb
    album = _replying()
    album.reply_media_group = AsyncMock(return_value=[_sent_msg("video"), _sent_msg("video")])
    asyncio.run(
        m._send_all(album, [("video", "M", 1, 2, 3), ("video", "N", 1, 2, 3)], from_cache=False, markup=kb)
    )
    album.reply_video.assert_not_called()  # albums go out as a group, with no button


def test_audio_file_is_named_after_the_track(tmp_path):
    p = tmp_path / "x9f3.m4a"
    p.write_bytes(b"x")
    item = dl.MediaItem(p, "audio", title='My: "Song"/1', performer="Me")
    spec = m._specs_from_items([item])[0]
    assert spec[1].filename == "My Song 1.m4a"


# ------------------------------------------------------------ the Video / Audio pop-up


def test_link_shows_the_popup_and_downloads_nothing_yet(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    links = m.LinkStore()
    msg = _msg(text="https://www.instagram.com/reel/abc/")
    _on_message(msg, tmp_path, ask_format=True, links=links)
    assert calls.video == 0 and calls.audio == 0
    msg.reply.assert_called_once()
    kb = msg.reply.call_args.kwargs["reply_markup"]
    labels = [b.text for b in kb.inline_keyboard[0]]
    assert labels == ["🎬 Video", "🎵 Audio"]
    for b in kb.inline_keyboard[0]:
        assert len(b.callback_data.encode()) <= 64


def test_audio_keyword_skips_the_popup(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    msg = _msg(text="audio https://www.instagram.com/reel/abc/")
    _on_message(msg, tmp_path, ask_format=True)
    assert calls.audio == 1
    msg.reply.assert_not_called()


def test_swisstransfer_skips_the_popup(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    msg = _msg(text="https://www.swisstransfer.com/d/aaaa-bbbb")
    _on_message(msg, tmp_path, ask_format=True)
    assert calls.video == 1
    msg.reply.assert_not_called()


def test_each_link_gets_its_own_popup(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    msg = _msg(text="https://youtu.be/aaaaaaaaaaa https://vm.tiktok.com/abc/")
    _on_message(msg, tmp_path, ask_format=True)
    assert msg.reply.call_count == 2


def _pick(choice, links, key, tmp_path, uid=42, reply_to=True):
    original = _msg(uid=5)
    prompt = _msg()
    prompt.reply_to_message = original if reply_to else None
    prompt.delete = AsyncMock()
    cb = SimpleNamespace(
        data=f"pick:{choice}:{key}", from_user=SimpleNamespace(id=uid),
        answer=AsyncMock(), message=prompt,
    )
    stats = Stats(str(tmp_path / "s.db"))
    cfg = _cfg(allowed_users=frozenset())
    asyncio.run(m.on_format_pick(cb, cfg, asyncio.Semaphore(1), m.FileIdCache(10), stats, links))
    return cb, prompt, original, stats


def test_picking_video_sends_the_video(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "Message", SimpleNamespace)
    links = m.LinkStore()
    key = links.add("https://www.instagram.com/reel/abc/")
    cb, prompt, original, stats = _pick("v", links, key, tmp_path)
    assert calls.video == 1 and calls.audio == 0
    assert calls.markups == [None]  # no extra button: they already chose
    prompt.delete.assert_called_once()  # the pop-up goes away
    assert stats.snapshot().delivered == 1 and stats.snapshot().audio == 0


def test_picking_audio_sends_only_the_audio(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "Message", SimpleNamespace)
    links = m.LinkStore()
    key = links.add("https://www.instagram.com/reel/abc/")
    _, prompt, _, stats = _pick("a", links, key, tmp_path)
    assert calls.audio == 1 and calls.video == 0
    assert stats.snapshot().audio == 1


def test_double_tap_sends_once(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "Message", SimpleNamespace)
    links = m.LinkStore()
    key = links.add("https://www.instagram.com/reel/abc/")
    _pick("v", links, key, tmp_path)
    cb, *_ = _pick("a", links, key, tmp_path)  # second tap on the same pop-up
    assert calls.video == 1 and calls.audio == 0
    assert "expired" in cb.answer.call_args.args[0]


def test_pick_without_original_message_keeps_the_popup(tmp_path, monkeypatch):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "Message", SimpleNamespace)
    links = m.LinkStore()
    key = links.add("https://youtu.be/aaaaaaaaaaa")
    _, prompt, _, _ = _pick("v", links, key, tmp_path, reply_to=False)
    prompt.delete.assert_not_called()  # we still need to reply to it


def test_pick_with_garbage_data_is_harmless(tmp_path, monkeypatch):
    calls = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "Message", SimpleNamespace)
    cb, *_ = _pick("x", m.LinkStore(), "nope", tmp_path)
    assert calls.video == 0 and calls.audio == 0
    assert "expired" in cb.answer.call_args.args[0]
