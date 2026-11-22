import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("BOT_TOKEN", "123:test")

from aiogram.enums import ChatType  # noqa: E402

import app.main as m  # noqa: E402
from app.downloader import platform_of  # noqa: E402
from app.stats import DAY, Snapshot, Stats, format_report  # noqa: E402

# Noon UTC on an arbitrary day, so "today" starts 12h earlier.
NOW = 1_800_000_000 - (1_800_000_000 % DAY) + 12 * 3600


def test_counts_and_windows(tmp_path):
    s = Stats(str(tmp_path / "s.db"))
    s.touch(1, now=NOW)  # active today
    s.touch(2, now=NOW - 3 * DAY)  # active this week
    s.touch(3, now=NOW - 10 * DAY)  # older
    s.record("start", 1, now=NOW)
    s.record("delivered", 1, "Instagram", now=NOW)
    s.record("delivered", 2, "TikTok", now=NOW - 2 * DAY)
    s.record("failed", 1, "YouTube", now=NOW)
    snap = s.snapshot(now=NOW)
    assert (snap.users, snap.active_today, snap.active_7d) == (3, 1, 2)
    assert (snap.delivered, snap.delivered_today) == (2, 1)
    assert (snap.failed, snap.failed_today, snap.starts) == (1, 1, 1)
    assert snap.by_platform == {"Instagram": 1, "TikTok": 1}


def test_touch_updates_last_seen_not_duplicates(tmp_path):
    s = Stats(str(tmp_path / "s.db"))
    s.touch(1, now=NOW - 5 * DAY)
    s.touch(1, now=NOW)
    snap = s.snapshot(now=NOW)
    assert snap.users == 1 and snap.active_today == 1


def test_survives_restart(tmp_path):
    path = str(tmp_path / "sub" / "s.db")  # parent folder is created automatically
    Stats(path).record("delivered", 1, "Instagram", now=NOW)
    assert Stats(path).snapshot(now=NOW).delivered == 1


def test_report_format():
    snap = Snapshot(
        users=10, active_today=2, active_7d=6, delivered=105, delivered_today=0,
        failed=14, starts=9,
        by_platform={"Instagram": 97, "TikTok": 7, "Facebook": 1},
    )
    lines = format_report(snap).split("\n")
    assert lines[1:5] == [
        "People who've used it: 10",
        "Active today: 2",
        "Active last 7 days: 6",
        "Posts delivered: 105 (today: 0)",
    ]
    assert "Instagram: 97" in lines and "TikTok: 7" in lines and "YouTube: 0" in lines
    assert "Facebook: 1" in lines  # other platforms appear when used
    assert "Failed requests: 14 (today: 0)" in lines
    assert "/start presses: 9" in lines


def test_platform_of():
    assert platform_of("https://www.instagram.com/reel/x/") == "Instagram"
    assert platform_of("https://vm.tiktok.com/abc") == "TikTok"
    assert platform_of("https://youtu.be/abc") == "YouTube"
    assert platform_of("https://m.facebook.com/x") == "Facebook"
    assert platform_of("https://x.com/u/status/1") == "X/Twitter"
    assert platform_of("https://notx.com/a") == "Other"


# ------------------------------------------------------------ handler behaviour


def _msg(uid=7, chat_type=ChatType.PRIVATE):
    return SimpleNamespace(
        bot=None,
        chat=SimpleNamespace(type=chat_type, id=1),
        from_user=SimpleNamespace(id=uid),
        answer=AsyncMock(),
        reply=AsyncMock(),
    )


class _NoChatAction:
    @classmethod
    def upload_video(cls, **kwargs):
        return cls()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_stats_command_admin_only(tmp_path):
    stats = Stats(str(tmp_path / "s.db"))
    stats.record("delivered", 1, "Instagram")

    async def run():
        admin, other, nobody = _msg(5), _msg(6), _msg(9)
        cfg = SimpleNamespace(admin_ids=frozenset({5}))
        await m.cmd_stats(admin, cfg, stats)
        await m.cmd_stats(other, cfg, stats)
        await m.cmd_stats(nobody, SimpleNamespace(admin_ids=frozenset()), stats)
        return admin, other, nobody

    admin, other, nobody = asyncio.run(run())
    assert "Posts delivered: 1" in admin.answer.call_args.args[0]
    other.answer.assert_not_called()  # non-admins get no reply
    assert "9" in nobody.answer.call_args.args[0]  # setup hint shows their ID


def test_start_counts_users_and_presses(tmp_path):
    stats = Stats(str(tmp_path / "s.db"))

    async def run():
        await m.cmd_start(_msg(1), stats)
        await m.cmd_start(_msg(1), stats)
        await m.cmd_start(_msg(2), stats)

    asyncio.run(run())
    snap = stats.snapshot()
    assert snap.users == 2 and snap.starts == 3


def test_delivered_and_failed_are_recorded(tmp_path, monkeypatch):
    stats = Stats(str(tmp_path / "s.db"))
    monkeypatch.setattr(m, "ChatActionSender", _NoChatAction)
    cache = m.FileIdCache(10)
    cfg = SimpleNamespace(max_file_mb=50)
    ig = "https://www.instagram.com/reel/abc/"
    tt = "https://vm.tiktok.com/xyz/"

    async def ok_fetch(*a, **k):
        return []

    async def ok_send(*a, **k):
        return [("video", "FILE_ID", None, None, None)]

    async def bad_fetch(*a, **k):
        raise m.DownloadError("nope")

    async def run():
        sem = asyncio.Semaphore(1)
        monkeypatch.setattr(m, "fetch_media", ok_fetch)
        monkeypatch.setattr(m, "_send_all", ok_send)
        await m._handle_url(_msg(), ig, cfg, sem, cache, stats)  # fresh download
        await m._handle_url(_msg(), ig, cfg, sem, cache, stats)  # repeat, from cache
        monkeypatch.setattr(m, "fetch_media", bad_fetch)
        msg = _msg()
        await m._handle_url(msg, tt, cfg, sem, cache, stats)  # failure
        return msg

    failed_msg = asyncio.run(run())
    snap = stats.snapshot()
    assert snap.delivered == 2 and snap.by_platform == {"Instagram": 2}
    assert snap.failed == 1
    failed_msg.reply.assert_called_once()  # user still gets the error message


def test_broken_stats_never_block_delivery(tmp_path, monkeypatch):
    stats = Stats(str(tmp_path / "s.db"))
    stats.record = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
    monkeypatch.setattr(m, "ChatActionSender", _NoChatAction)

    async def ok_fetch(*a, **k):
        return []

    async def ok_send(*a, **k):
        return [("video", "F", None, None, None)]

    monkeypatch.setattr(m, "fetch_media", ok_fetch)
    monkeypatch.setattr(m, "_send_all", ok_send)
    cache = m.FileIdCache(10)

    async def run():
        msg = _msg()
        await m._handle_url(
            msg, "https://youtu.be/a", SimpleNamespace(), asyncio.Semaphore(1), cache, stats
        )
        return msg

    msg = asyncio.run(run())
    msg.reply.assert_not_called()  # no error shown to the user
    assert cache.get("https://youtu.be/a")  # delivery completed normally
