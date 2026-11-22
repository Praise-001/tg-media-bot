"""Usage statistics, kept in a small SQLite file so they survive restarts."""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DAY = 86400  # seconds. Unix time is UTC, so "today" = since 00:00 UTC.

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    user_id  INTEGER,
    kind     TEXT NOT NULL,      -- 'start' | 'delivered' | 'failed'
    platform TEXT
);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts);
"""

# Always shown in the report, in this order (even at 0).
FIXED_PLATFORMS = ("Instagram", "TikTok", "YouTube", "SwissTransfer")


@dataclass
class Snapshot:
    users: int = 0
    active_today: int = 0
    active_7d: int = 0
    delivered: int = 0
    delivered_today: int = 0
    failed: int = 0
    failed_today: int = 0
    starts: int = 0
    by_platform: dict[str, int] = field(default_factory=dict)


class Stats:
    def __init__(self, path: str) -> None:
        parent = Path(path).parent
        parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    # ------------------------------------------------------------------ writes

    def touch(self, user_id: int, now: int | None = None) -> None:
        """Remember that this person used the bot just now."""
        now = int(time.time()) if now is None else now
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO users(user_id, first_seen, last_seen) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_seen = excluded.last_seen",
                (user_id, now, now),
            )

    def record(
        self,
        kind: str,
        user_id: int | None = None,
        platform: str | None = None,
        now: int | None = None,
    ) -> None:
        now = int(time.time()) if now is None else now
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO events(ts, user_id, kind, platform) VALUES(?, ?, ?, ?)",
                (now, user_id, kind, platform),
            )

    # ------------------------------------------------------------------- reads

    def snapshot(self, now: int | None = None) -> Snapshot:
        now = int(time.time()) if now is None else now
        today = now - now % DAY
        week = now - 7 * DAY

        def one(sql: str, *args: int | str) -> int:
            return self._db.execute(sql, args).fetchone()[0]

        with self._lock:
            snap = Snapshot(
                users=one("SELECT COUNT(*) FROM users"),
                active_today=one("SELECT COUNT(*) FROM users WHERE last_seen >= ?", today),
                active_7d=one("SELECT COUNT(*) FROM users WHERE last_seen >= ?", week),
                delivered=one("SELECT COUNT(*) FROM events WHERE kind = 'delivered'"),
                delivered_today=one(
                    "SELECT COUNT(*) FROM events WHERE kind = 'delivered' AND ts >= ?", today
                ),
                failed=one("SELECT COUNT(*) FROM events WHERE kind = 'failed'"),
                failed_today=one(
                    "SELECT COUNT(*) FROM events WHERE kind = 'failed' AND ts >= ?", today
                ),
                starts=one("SELECT COUNT(*) FROM events WHERE kind = 'start'"),
            )
            rows = self._db.execute(
                "SELECT COALESCE(platform, 'Other'), COUNT(*) FROM events "
                "WHERE kind = 'delivered' GROUP BY 1"
            ).fetchall()
        snap.by_platform = {name: n for name, n in rows}
        return snap


def format_report(s: Snapshot) -> str:
    lines = [
        "<b>Bot stats (UTC)</b>",
        f"People who've used it: {s.users}",
        f"Active today: {s.active_today}",
        f"Active last 7 days: {s.active_7d}",
        f"Posts delivered: {s.delivered} (today: {s.delivered_today})",
    ]
    for name in FIXED_PLATFORMS:
        lines.append(f"{name}: {s.by_platform.get(name, 0)}")
    others = sorted(
        ((n, c) for n, c in s.by_platform.items() if n not in FIXED_PLATFORMS and c),
        key=lambda x: (-x[1], x[0]),
    )
    for name, count in others:
        lines.append(f"{name}: {count}")
    lines.append(f"Failed requests: {s.failed} (today: {s.failed_today})")
    lines.append(f"/start presses: {s.starts}")
    return "\n".join(lines)
