"""Settings, read from environment variables (or a local .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int_set(name: str, raw: str) -> frozenset[int]:
    try:
        return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)
    except ValueError:
        raise SystemExit(
            f"{name} must be numeric Telegram user IDs separated by commas (e.g. 123456789,987654321). "
            "Send /id to the bot to see yours."
        )


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    token: str
    # Telegram's cloud Bot API caps uploads at 50 MB. If you run a local
    # Bot API server (TELEGRAM_API_URL), you can raise MAX_FILE_MB up to 2000.
    max_file_mb: int
    max_concurrent: int
    download_timeout: int
    allowed_users: frozenset[int]  # empty = everyone
    cookies_file: str | None
    allow_any_domain: bool
    api_url: str | None
    cache_size: int
    admin_ids: frozenset[int]  # who may use /stats; empty = stats disabled
    stats_db_path: str

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "BOT_TOKEN is missing. Copy .env.example to .env and paste the token from @BotFather."
        )

    cookies = os.getenv("COOKIES_FILE", "").strip() or None
    if cookies and not os.path.isfile(cookies):
        print(f"[warn] COOKIES_FILE={cookies} not found; continuing without cookies.")
        cookies = None

    return Settings(
        token=token,
        max_file_mb=int(os.getenv("MAX_FILE_MB", "50")),
        max_concurrent=int(os.getenv("MAX_CONCURRENT", "4")),
        download_timeout=int(os.getenv("DOWNLOAD_TIMEOUT", "120")),
        allowed_users=_int_set("ALLOWED_USER_IDS", os.getenv("ALLOWED_USER_IDS", "")),
        cookies_file=cookies,
        allow_any_domain=_bool(os.getenv("ALLOW_ANY_DOMAIN"), False),
        api_url=os.getenv("TELEGRAM_API_URL", "").strip() or None,
        cache_size=int(os.getenv("CACHE_SIZE", "500")),
        admin_ids=_int_set("ADMIN_IDS", os.getenv("ADMIN_IDS", "")),
        stats_db_path=os.getenv("STATS_DB_PATH", "").strip() or "data/stats.db",
    )
