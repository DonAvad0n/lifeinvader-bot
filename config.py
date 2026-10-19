from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env(BASE_DIR / ".env")


def _int_or_none(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def _time_tuple(value: str, default: tuple[int, int]) -> tuple[int, int]:
    parts = (value or "").strip().split(":")
    if len(parts) != 2:
        return default
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return default
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return hour, minute


def _data_dir() -> Path:
    raw = os.getenv("DATA_DIR", "").strip()
    if raw:
        candidate = Path(raw)
    elif Path("/app/data").is_dir():
        candidate = Path("/app/data")
    else:
        candidate = BASE_DIR
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError:
        return BASE_DIR


DATA_DIR: Path = _data_dir()

DISCORD_TOKEN: str = (
    os.getenv("DISCORD_TOKEN", "").strip() or os.getenv("BOT_TOKEN", "").strip()
)
GUILD_ID: int | None = _int_or_none(os.getenv("GUILD_ID", ""))

SHEET_ID: str = os.getenv("SHEET_ID", "1XMKdp0fTuBQte6coRJ-j25Sh4c0mPFfh4EC3YqIx23s").strip()
SHEET_GID: str = os.getenv("SHEET_GID", "530700277").strip()
SHEET_NAME: str = os.getenv("SHEET_NAME", "Сетка эфиров").strip()

TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Moscow").strip()

PANEL_CHANNEL_ID: int | None = _int_or_none(os.getenv("PANEL_CHANNEL_ID", ""))
ROLE_ID: int | None = _int_or_none(os.getenv("ROLE_ID", ""))
ROLE_NAME: str = os.getenv("ROLE_NAME", "🎤 | Эфирный отдел").strip()

REMINDER_MINUTES: int = int(os.getenv("REMINDER_MINUTES", "1"))
REMINDER_LEAD_SECONDS: int = int(os.getenv("REMINDER_LEAD_SECONDS", "300"))

SUMMARY_HOUR, SUMMARY_MINUTE = _time_tuple(os.getenv("SUMMARY_TIME", "22:17"), (22, 17))
SUMMARY_WINDOW_MINUTES: int = int(os.getenv("SUMMARY_WINDOW_MINUTES", "180"))
DAILY_POST_HOUR, DAILY_POST_MINUTE = _time_tuple(os.getenv("DAILY_POST_TIME", "08:00"), (8, 0))

DB_PATH: Path = DATA_DIR / os.getenv("DB_FILE", "efiry.sqlite3")
CACHE_TTL: int = int(os.getenv("CACHE_TTL", "180"))
TICKER_MINUTES: int = int(os.getenv("TICKER_MINUTES", "10"))

APPLY_PANEL_CHANNEL_ID: int = int(os.getenv("APPLY_PANEL_CHANNEL_ID", "1561361703277563994"))
APPLY_REVIEW_CHANNEL_ID: int = int(os.getenv("APPLY_REVIEW_CHANNEL_ID", "1561361854842937455"))
APPLY_AUDIT_CHANNEL_ID: int = int(os.getenv("APPLY_AUDIT_CHANNEL_ID", "1561362023563001966"))
TRAINEE_ROLE_ID: int = int(os.getenv("TRAINEE_ROLE_ID", "1561362581736652861"))
APPLY_ACTION_TEXT: str = os.getenv("APPLY_ACTION_TEXT", "Принят на 1").strip()
APPLY_REASON_TEXT: str = os.getenv("APPLY_REASON_TEXT", "Собеседование").strip()
EPHEMERAL_DELETE_SECONDS: int = int(os.getenv("EPHEMERAL_DELETE_SECONDS", "30"))
