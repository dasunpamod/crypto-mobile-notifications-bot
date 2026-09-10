"""Configuration loaded from environment variables / .env file.

All values are validated defensively so a malformed .env can never crash
the process at import time. Call :func:`validate_config` at startup for
human-readable warnings.
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _parse_user_id(raw: str) -> int:
    try:
        return int((raw or "").strip())
    except (ValueError, TypeError):
        return 0


# Path to SQLite database file (empty = default alerts.db in app directory).
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "").strip().strip("'\"")

# Telegram bot token from @BotFather.
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip("'\"")

# Telegram user id that may control the bot (0 = unset → deny-all by default).
TELEGRAM_USER_ID: int = _parse_user_id(os.getenv("TELEGRAM_USER_ID", "0"))

# ntfy.sh topic name (pick something unique and non-guessable).
NTFY_TOPIC: str = os.getenv("NTFY_TOPIC", "").strip().strip("'\"")

# ntfy server URL (default: public ntfy.sh, can self-host).
NTFY_SERVER: str = os.getenv("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/") or "https://ntfy.sh"

# Whether to also send alert notifications via Telegram (in addition to ntfy).
SEND_TELEGRAM_ALERTS: bool = os.getenv("SEND_TELEGRAM_ALERTS", "false").strip().lower() in (
    "1", "true", "yes", "on",
)

# Daily briefing time (HH:MM, 24h UTC). Defaults to 08:00 UTC.
DAILY_BRIEFING_TIME: str = os.getenv("DAILY_BRIEFING_TIME", "08:00").strip() or "08:00"

# Symbols included in the daily briefing (comma separated, e.g. "BTCUSDT,ETHUSDT").
DAILY_BRIEFING_SYMBOLS: tuple = tuple(
    s.strip().upper()
    for s in os.getenv("DAILY_BRIEFING_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT").split(",")
    if s.strip()
) or ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")

# Cooldown between repeat firings of a persistent alert, in seconds.
PERSISTENT_ALERT_COOLDOWN_SEC: int = 3600
try:
    PERSISTENT_ALERT_COOLDOWN_SEC = max(
        60, int(os.getenv("PERSISTENT_ALERT_COOLDOWN_SEC", "3600").strip())
    )
except (ValueError, TypeError, AttributeError):
    PERSISTENT_ALERT_COOLDOWN_SEC = 3600

# How long alerts stay muted with the pause button, in hours.
PAUSE_DURATION_HOURS: int = 1
try:
    PAUSE_DURATION_HOURS = min(24, max(1, int(os.getenv("PAUSE_DURATION_HOURS", "1").strip())))
except (ValueError, TypeError, AttributeError):
    PAUSE_DURATION_HOURS = 1


def _parse_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(hi, max(lo, int(os.getenv(name, str(default)).strip())))
    except (ValueError, TypeError, AttributeError):
        return default


def _parse_bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _parse_symbols(raw: str, default: tuple) -> tuple:
    """Parse 'BTC,ETHUSDT' -> ('BTCUSDT', 'ETHUSDT'). Local copy (no db import)."""
    out = []
    for part in (raw or "").split(","):
        s = part.strip().upper().replace(" ", "").replace("-", "").replace("/", "")
        if not s:
            continue
        if not s.endswith("USDT"):
            s = f"{s}USDT"
        if 3 <= len(s) <= 25 and s.replace("USDT", "").isalnum():
            out.append(s)
    return tuple(out) or default


# Safety caps (override the module-level constants elsewhere).
MAX_ALERTS: int = _parse_int("MAX_ALERTS", 100, 1, 1000)
MAX_SYMBOLS: int = _parse_int("MAX_SYMBOLS", 100, 1, 500)

# How long REST price lookups are cached, in seconds.
PRICE_CACHE_TTL_SEC: int = _parse_int("PRICE_CACHE_TTL_SEC", 10, 2, 300)

# Default watchlist for one-tap price checks (BTC ETH SOL HYPE + your own).
WATCHLIST_SYMBOLS: tuple = _parse_symbols(
    os.getenv("WATCHLIST_SYMBOLS", "BTC,ETH,SOL,HYPE"),
    ("BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT"),
)

# Quiet hours in UTC ("HH-HH" or "HH:MM-HH:MM", e.g. "23-07"). Empty = off.
# Alerts stay armed but never fire inside the window.
QUIET_HOURS_UTC: str = os.getenv("QUIET_HOURS_UTC", "").strip()


def quiet_hours_range() -> tuple | None:
    """Return (start_min, end_min) minutes-since-midnight, or None if disabled."""
    raw = (QUIET_HOURS_UTC or "").strip()
    if not raw:
        return None
    try:
        start_s, end_s = raw.split("-")
        def _mins(s: str) -> int:
            s = s.strip()
            if ":" in s:
                h, m = s.split(":")
                h, m = int(h), int(m)
            else:
                h, m = int(s), 0
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            return h * 60 + m
        return _mins(start_s), _mins(end_s)
    except (ValueError, AttributeError):
        return None


def quiet_now() -> bool:
    rng = quiet_hours_range()
    if not rng:
        return False
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    cur = now.hour * 60 + now.minute
    start, end = rng
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


# Display timezone for timestamps (e.g. "Asia/Colombo"). Falls back to UTC.
TIMEZONE: str = os.getenv("TIMEZONE", "UTC").strip() or "UTC"


def local_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TIMEZONE)
    except Exception:
        import datetime as _dt
        return _dt.timezone.utc


def now_local():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).astimezone(local_tz())


# Outgoing webhook called on every trigger (JSON POST). Empty = off.
WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "").strip()

# Healthcheck ping URL (Uptime Kuma / healthchecks.io). Empty = off.
HEALTHCHECK_URL: str = os.getenv("HEALTHCHECK_URL", "").strip()
HEARTBEAT_INTERVAL_SEC: int = _parse_int("HEARTBEAT_INTERVAL_SEC", 300, 10, 86400)

# ntfy auth for private servers (token preferred, else user/password).
NTFY_USER: str = os.getenv("NTFY_USER", "").strip().strip("'\"")
NTFY_PASSWORD: str = os.getenv("NTFY_PASSWORD", "").strip().strip("'\"")
NTFY_TOKEN: str = os.getenv("NTFY_TOKEN", "").strip().strip("'\"")

# Include 24h change in the daily briefing.
BRIEFING_24H_CHANGE: bool = _parse_bool("BRIEFING_24H_CHANGE", True)

_VALID_PRIORITIES = ("max", "high", "default", "low", "min")
ALERT_PRIORITY_ONESHOT: str = os.getenv("ALERT_PRIORITY_ONESHOT", "high").strip().lower()
if ALERT_PRIORITY_ONESHOT not in _VALID_PRIORITIES:
    ALERT_PRIORITY_ONESHOT = "high"
ALERT_PRIORITY_REPEAT: str = os.getenv("ALERT_PRIORITY_REPEAT", "default").strip().lower()
if ALERT_PRIORITY_REPEAT not in _VALID_PRIORITIES:
    ALERT_PRIORITY_REPEAT = "default"


def validate_config() -> list:
    """Return a list of human-readable configuration problems (empty = ok)."""
    problems = []
    if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN is missing or malformed. Get one from @BotFather.")
    if not TELEGRAM_USER_ID:
        problems.append(
            "TELEGRAM_USER_ID is not set — the bot will refuse all commands until it is set."
        )
    try:
        hour, minute = DAILY_BRIEFING_TIME.split(":")
        if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
            raise ValueError
    except ValueError:
        problems.append(f"DAILY_BRIEFING_TIME={DAILY_BRIEFING_TIME!r} is invalid, expected HH:MM (UTC).")
    return problems

