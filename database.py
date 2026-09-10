"""SQLite database for persistent alert storage."""

import aiosqlite
import datetime
import logging
import os
import re

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts.db")
_db: aiosqlite.Connection | None = None

# Symbols are always stored normalized: uppercase alphanumerics ending in a
# quote asset. Validation here is intentionally permissive (any XXXUSDT-style
# pair) — existence is verified against the exchange API at the bot layer.
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")

# Alert kinds supported by the engine.
_ALERT_TYPES = ("price", "pct", "trail", "move", "funding")


def normalize_symbol(symbol: str) -> str:
    """Normalize user input to canonical form, e.g. 'btc' -> 'BTCUSDT'."""
    s = (symbol or "").strip().upper().replace(" ", "").replace("-", "").replace("/", "")
    if not s.endswith("USDT"):
        s = f"{s}USDT"
    return s


def parse_symbols(text: str) -> list:
    """Parse 'BTC, eth, SOLUSDT' -> ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'] (deduped)."""
    out = []
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        sym = normalize_symbol(part)
        if sym not in out:
            out.append(sym)
    return out


def is_valid_symbol(symbol: str) -> bool:
    return bool(_SYMBOL_RE.match(normalize_symbol(symbol)))


async def get_db() -> aiosqlite.Connection:
    """Get or create the shared database connection."""
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA busy_timeout = 5000")
        await _db.execute("PRAGMA foreign_keys = ON")
    return _db


async def init_db() -> None:
    """Create tables and apply migrations (idempotent, safe on existing DBs)."""
    db = await get_db()
    await db.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT    NOT NULL,
            target      REAL    NOT NULL CHECK(target > 0),
            condition   TEXT    NOT NULL CHECK(condition IN ('above', 'below')),
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS fired_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id    INTEGER,
            symbol      TEXT    NOT NULL,
            condition   TEXT    NOT NULL,
            target      REAL    NOT NULL,
            price       REAL    NOT NULL,
            detail      TEXT    DEFAULT '',
            fired_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pending_notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT    NOT NULL,
            message     TEXT    NOT NULL,
            tags        TEXT    DEFAULT '',
            priority    TEXT    DEFAULT 'high',
            symbol      TEXT    DEFAULT '',
            attempts    INTEGER DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            symbol      TEXT PRIMARY KEY,
            added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS kv_store (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor = await db.execute("PRAGMA table_info(alerts)")
    columns = [row[1] async for row in cursor]
    await cursor.close()

    for col, ddl in (
        ("is_persistent", "ALTER TABLE alerts ADD COLUMN is_persistent INTEGER DEFAULT 0"),
        ("last_triggered_at", "ALTER TABLE alerts ADD COLUMN last_triggered_at TIMESTAMP"),
        ("alert_type", "ALTER TABLE alerts ADD COLUMN alert_type TEXT DEFAULT 'price'"),
        ("expires_at", "ALTER TABLE alerts ADD COLUMN expires_at TIMESTAMP"),
        ("snoozed_until", "ALTER TABLE alerts ADD COLUMN snoozed_until TIMESTAMP"),
        ("cooldown_sec", "ALTER TABLE alerts ADD COLUMN cooldown_sec INTEGER"),
        ("pct", "ALTER TABLE alerts ADD COLUMN pct REAL"),
        ("window_min", "ALTER TABLE alerts ADD COLUMN window_min INTEGER"),
        ("base_price", "ALTER TABLE alerts ADD COLUMN base_price REAL"),
        ("peak_price", "ALTER TABLE alerts ADD COLUMN peak_price REAL"),
        ("funding_rate", "ALTER TABLE alerts ADD COLUMN funding_rate REAL"),
    ):
        if col not in columns:
            await db.execute(ddl)
    # Backfill alert_type for rows created before the column existed.
    await db.execute("UPDATE alerts SET alert_type = 'price' WHERE alert_type IS NULL OR alert_type = ''")

    await db.execute("CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_fired_symbol ON fired_log(symbol)")
    await db.commit()


def _colmap() -> dict:
    """Map alert column name -> SELECT index (SELECT * order)."""
    return {
        "id": 0, "symbol": 1, "target": 2, "condition": 3, "created_at": 4,
        "is_persistent": 5, "last_triggered_at": 6, "alert_type": 7,
        "expires_at": 8, "snoozed_until": 9, "cooldown_sec": 10, "pct": 11,
        "window_min": 12, "base_price": 13, "peak_price": 14, "funding_rate": 15,
    }


def alert_field(row, name: str, default=None):
    """Read a possibly-missing column from a SELECT * row by name."""
    idx = _colmap().get(name)
    if idx is None or row is None or idx >= len(row):
        return default
    val = row[idx]
    return default if val is None else val


async def add_alert(symbol: str, target: float, condition: str, is_persistent: bool = False,
                    alert_type: str = "price", expires_at=None, cooldown_sec=None,
                    pct=None, window_min=None, base_price=None, peak_price=None,
                    funding_rate=None) -> int:
    """Add a new alert. Returns the alert ID."""
    symbol = normalize_symbol(symbol)
    if not is_valid_symbol(symbol):
        raise ValueError(f"Invalid symbol: {symbol!r}")
    if condition not in ("above", "below"):
        raise ValueError(f"Invalid condition: {condition!r}")
    if alert_type not in _ALERT_TYPES:
        raise ValueError(f"Invalid alert type: {alert_type!r}")
    if not (target > 0 and target != float("inf") and target == target):
        raise ValueError(f"Invalid target price: {target!r}")
    if cooldown_sec is not None and not (60 <= int(cooldown_sec) <= 30 * 86400):
        raise ValueError("cooldown must be 60s..30d")
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO alerts (symbol, target, condition, is_persistent, alert_type,
                               expires_at, cooldown_sec, pct, window_min,
                               base_price, peak_price, funding_rate)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (symbol, target, condition, 1 if is_persistent else 0, alert_type,
         expires_at, cooldown_sec, pct, window_min, base_price, peak_price, funding_rate),
    )
    await db.commit()
    alert_id = cursor.lastrowid
    await cursor.close()
    return alert_id


async def update_last_triggered(alert_id: int) -> None:
    """Update the last_triggered_at timestamp for a persistent alert."""
    db = await get_db()
    await db.execute("UPDATE alerts SET last_triggered_at = CURRENT_TIMESTAMP WHERE id = ?", (alert_id,))
    await db.commit()


async def touch_peak(alert_id: int, peak: float) -> None:
    db = await get_db()
    await db.execute("UPDATE alerts SET peak_price = ? WHERE id = ?", (peak, alert_id))
    await db.commit()


async def rebase_alert(alert_id: int, base_price: float) -> None:
    """Reset the %/move window anchor (used by pct/move/trail alerts)."""
    db = await get_db()
    await db.execute(
        "UPDATE alerts SET base_price = ?, peak_price = ?, last_triggered_at = CURRENT_TIMESTAMP WHERE id = ?",
        (base_price, base_price, alert_id),
    )
    await db.commit()


async def set_snooze(alert_id: int, until_iso: str | None) -> None:
    db = await get_db()
    await db.execute("UPDATE alerts SET snoozed_until = ? WHERE id = ?", (until_iso, alert_id))
    await db.commit()


async def set_target(alert_id: int, target: float, condition: str) -> None:
    if condition not in ("above", "below"):
        raise ValueError("bad condition")
    if not (target > 0 and target != float("inf") and target == target):
        raise ValueError("bad target")
    db = await get_db()
    await db.execute(
        "UPDATE alerts SET target = ?, condition = ?, alert_type = 'price' WHERE id = ?",
        (target, condition, alert_id),
    )
    await db.commit()


async def prune_expired() -> int:
    """Delete expired alerts. Returns count removed."""
    db = await get_db()
    cursor = await db.execute(
        "DELETE FROM alerts WHERE expires_at IS NOT NULL AND expires_at != '' "
        "AND datetime(expires_at) <= datetime('now')"
    )
    await db.commit()
    count = cursor.rowcount
    await cursor.close()
    return count


async def log_fired(alert_id, symbol, condition, target, price, detail="") -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO fired_log (alert_id, symbol, condition, target, price, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (alert_id, symbol, condition, target, price, detail or ""),
    )
    # Keep the log bounded (last 500 rows).
    await db.execute(
        "DELETE FROM fired_log WHERE id NOT IN "
        "(SELECT id FROM fired_log ORDER BY id DESC LIMIT 500)"
    )
    await db.commit()


async def get_fired_history(limit: int = 15) -> list:
    db = await get_db()
    cursor = await db.execute(
        "SELECT alert_id, symbol, condition, target, price, detail, fired_at "
        "FROM fired_log ORDER BY id DESC LIMIT ?",
        (max(1, min(int(limit), 50)),),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def queue_notification(title: str, message: str, tags: str = "",
                            priority: str = "high", symbol: str = "") -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO pending_notifications (title, message, tags, priority, symbol) "
        "VALUES (?, ?, ?, ?, ?)",
        (title, message, tags or "", priority or "high", symbol or ""),
    )
    await db.commit()


async def pop_pending_notifications(limit: int = 20) -> list:
    db = await get_db()
    cursor = await db.execute(
        "SELECT id, title, message, tags, priority, symbol, attempts "
        "FROM pending_notifications ORDER BY id LIMIT ?",
        (max(1, min(int(limit), 50)),),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def ack_notification(nid: int) -> None:
    db = await get_db()
    await db.execute("DELETE FROM pending_notifications WHERE id = ?", (nid,))
    await db.commit()


async def bump_notification(nid: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE pending_notifications SET attempts = attempts + 1 WHERE id = ?", (nid,)
    )
    # Drop poison entries after 25 attempts (~2 days at hourly flush).
    await db.execute("DELETE FROM pending_notifications WHERE attempts > 25")
    await db.commit()


async def pending_count() -> int:
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) FROM pending_notifications")
    row = await cursor.fetchone()
    await cursor.close()
    return row[0] if row else 0


async def get_watchlist() -> list:
    db = await get_db()
    cursor = await db.execute("SELECT symbol FROM watchlist ORDER BY symbol")
    rows = await cursor.fetchall()
    await cursor.close()
    return [r[0] for r in rows]


async def add_watch(symbol: str) -> bool:
    symbol = normalize_symbol(symbol)
    if not is_valid_symbol(symbol):
        raise ValueError(f"Invalid symbol: {symbol!r}")
    db = await get_db()
    cursor = await db.execute(
        "INSERT OR IGNORE INTO watchlist (symbol) VALUES (?)", (symbol,)
    )
    await db.commit()
    added = cursor.rowcount > 0
    await cursor.close()
    return added


async def remove_watch(symbol: str) -> bool:
    db = await get_db()
    cursor = await db.execute(
        "DELETE FROM watchlist WHERE symbol = ?", (normalize_symbol(symbol),)
    )
    await db.commit()
    removed = cursor.rowcount > 0
    await cursor.close()
    return removed


async def kv_get(key: str, default: str = "") -> str:
    db = await get_db()
    cursor = await db.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
    row = await cursor.fetchone()
    await cursor.close()
    return row[0] if row else default


async def kv_set(key: str, value: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO kv_store (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP",
        (key, value),
    )
    await db.commit()


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def iso_in(days=0, hours=0, minutes=0) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        days=days, hours=hours, minutes=minutes
    )
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def get_alert(alert_id: int):
    """Get a single alert by ID. Returns tuple or None."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def remove_alert(alert_id: int) -> bool:
    """Remove an alert by ID. Returns True if it existed."""
    db = await get_db()
    cursor = await db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    await db.commit()
    removed = cursor.rowcount > 0
    await cursor.close()
    return removed


async def remove_all_alerts() -> int:
    """Remove all alerts. Returns the count removed."""
    db = await get_db()
    cursor = await db.execute("DELETE FROM alerts")
    await db.commit()
    count = cursor.rowcount
    await cursor.close()
    return count


async def count_alerts() -> int:
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) FROM alerts")
    row = await cursor.fetchone()
    await cursor.close()
    return row[0] if row else 0


async def get_all_alerts() -> list:
    """Get all alerts ordered by ID."""
    db = await get_db()
    cursor = await db.execute("SELECT * FROM alerts ORDER BY id")
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def get_alerts_for_symbol(symbol: str) -> list:
    """Get all alerts for a specific symbol."""
    db = await get_db()
    cursor = await db.execute(
        "SELECT * FROM alerts WHERE symbol = ?", (normalize_symbol(symbol),)
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def get_active_symbols() -> set[str]:
    """Get the set of symbols that have active alerts."""
    db = await get_db()
    cursor = await db.execute("SELECT DISTINCT symbol FROM alerts")
    rows = await cursor.fetchall()
    await cursor.close()
    return {row[0] for row in rows}


async def close_db() -> None:
    """Close the database connection."""
    global _db
    if _db:
        try:
            await _db.close()
        except Exception as e:
            logger.warning(f"Error closing database: {e}")
        finally:
            _db = None

