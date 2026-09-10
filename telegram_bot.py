"""Telegram bot for managing crypto price alerts."""

import functools
import logging
import datetime
import time

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

import database as db
import config
from config import TELEGRAM_BOT_TOKEN
from prices import (
    get_current_price, get_prices, get_ticker, get_tickers, get_top_movers,
    cached_price, cached_ticker, ticker_price, ticker_change_24h,
)
from database import normalize_symbol, is_valid_symbol, parse_symbols

logger = logging.getLogger(__name__)

# Safety caps (env-overridable via config.MAX_ALERTS).
MAX_PRICE_VALUE = 1_000_000_000

# Simple per-user rate limit: min seconds between mutating commands.
_RATE_LIMIT_SEC = 1.0
_last_cmd_at: dict = {}


def _max_alerts() -> int:
    try:
        return max(1, int(config.MAX_ALERTS))
    except Exception:
        return 100


def _check_rate_limit(user_id: int) -> bool:
    """Return True if allowed, False if the user is sending commands too fast."""
    now = time.monotonic()
    last = _last_cmd_at.get(user_id, 0)
    if now - last < _RATE_LIMIT_SEC:
        return False
    _last_cmd_at[user_id] = now
    return True


def _fmt_pct(pct) -> str:
    try:
        return f"{float(pct) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_ts(value) -> str:
    if not value:
        return "n/a"
    try:
        import datetime as _dt
        text = str(value).strip()
        if "T" in text:
            dt = _dt.datetime.fromisoformat(text.replace("Z", ""))
        else:
            dt = _dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(config.local_tz()).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _parse_expiry(text: str):
    """Parse '7d'/'12h'/'30m' -> ISO expiry string. None if empty/invalid."""
    text = (text or "").strip().lower()
    if not text:
        return None
    try:
        if text.endswith("d"):
            return db.iso_in(days=int(text[:-1]))
        if text.endswith("h"):
            return db.iso_in(hours=int(text[:-1]))
        if text.endswith("m"):
            return db.iso_in(minutes=int(text[:-1]))
    except ValueError:
        return "invalid"
    return "invalid"


def _watchlist_defaults() -> list:
    out = list(config.WATCHLIST_SYMBOLS or ())
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT"):
        if s not in out:
            out.append(s)
    return out


async def _effective_watchlist() -> list:
    """DB watchlist if set, else env WATCHLIST_SYMBOLS, else BTC/ETH/SOL/HYPE."""
    try:
        custom = await db.get_watchlist()
    except Exception:
        custom = []
    if custom:
        return custom
    return _watchlist_defaults()


async def _send_all_prices(target, engine, context=None, prefix="*Prices*\n") -> None:
    """One-shot all-coins price board: watchlist + alert symbols, all at once."""
    watch = await _effective_watchlist()
    try:
        alert_syms = sorted(await db.get_active_symbols())
    except Exception:
        alert_syms = []
    symbols = list(watch)
    for s in alert_syms:
        if s not in symbols:
            symbols.append(s)
    symbols = symbols[:25]
    tickers = await get_tickers(symbols)
    lines = [prefix]
    for symbol in symbols:
        coin = _escape_md(symbol.replace("USDT", ""))
        cached = engine.last_prices.get(symbol) if engine else None
        ticker = tickers.get(symbol)
        price = cached if cached is not None else ticker_price(ticker)
        if price is None:
            price = cached_price(symbol)
        if price is None:
            lines.append(f"  *{coin}*: unavailable")
            continue
        pct = ticker_change_24h(ticker or cached_ticker(symbol))
        extra = f" ({_fmt_pct(pct)} 24h)" if pct is not None else ""
        lines.append(f"  *{coin}*: {format_price(price)}{extra}")
    stamp = config.now_local().strftime("%H:%M")
    lines.append(f"\n_Updated {stamp}_")
    kb = [[InlineKeyboardButton("Refresh all", callback_data="prices_all")]]
    try:
        if hasattr(target, "reply_text"):
            await target.reply_text("\n".join(lines), parse_mode="Markdown",
                                    reply_markup=InlineKeyboardMarkup(kb))
        else:
            await target.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                           reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def authorized(func):
    """Decorator: deny everyone unless they match TELEGRAM_USER_ID.

    Fail-closed: if TELEGRAM_USER_ID is unset (0), ALL users are rejected
    until the owner configures it. This prevents accidentally running a
    public bot that anyone can control.
    """
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        allowed = config.TELEGRAM_USER_ID
        if not allowed or not user or user.id != allowed:
            logger.warning(f"Rejected unauthorized access from user={getattr(user, 'id', None)}")
            try:
                if update.callback_query:
                    await update.callback_query.answer("Unauthorized.", show_alert=True)
                elif update.message:
                    await update.message.reply_text("Unauthorized.")
            except Exception:
                pass
            return
        return await func(update, context)
    return wrapper


def _escape_md(text: str) -> str:
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


def format_price(price: float) -> str:
    """Format a price dynamically based on its magnitude."""
    try:
        price = float(price)
    except (TypeError, ValueError):
        return "N/A"
    if price != price or price == float("inf") or price <= 0:
        return "N/A"
    if price < 0.001:
        return f"${price:.6f}"
    elif price < 1:
        return f"${price:.4f}"
    else:
        return f"${price:,.2f}"


async def _resolve_symbol(coin_or_symbol: str, engine=None) -> tuple | None:
    """Normalize + verify a symbol. Returns (symbol, price)."""
    symbol = normalize_symbol(coin_or_symbol)
    if not is_valid_symbol(symbol):
        return None
    price = await get_current_price(symbol)
    if price is None and engine is not None:
        price = engine.last_prices.get(symbol)
    return symbol, price


# Re-exported for backwards compatibility (main.py imports it from here).
__all__ = ["format_price", "get_current_price", "create_bot"]

# Alias kept so `from telegram_bot import get_current_price` still works
# (single source of truth lives in prices.py).


def get_main_keyboard(engine):
    """Create the persistent main menu keyboard."""
    pause_btn = "▶️ Resume Alerts" if engine.is_muted() else "⏸️ Pause Alerts"
    keyboard = [
        [KeyboardButton("📋 List Alerts"), KeyboardButton("💰 Check Price")],
        [KeyboardButton("➕ Add Alert"), KeyboardButton("❌ Remove Alert")],
        [KeyboardButton(pause_btn), KeyboardButton("❓ Help")],
        [KeyboardButton("Prices: All"), KeyboardButton("Movers"), KeyboardButton("History")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def get_list_text_and_markup(engine, page: int = 0, filt: str | None = None,
                                   per_page: int = 8):
    """Paginated alert list, optionally filtered by coin. Returns (text, markup)."""
    alerts = await db.get_all_alerts()
    if filt:
        filt = db.normalize_symbol(filt)
        alerts = [a for a in alerts if a[1] == filt]

    if not alerts:
        coin = filt.replace("USDT", "") if filt else ""
        hint = f"No active alerts{f' for {coin}' if filt else ''}."
        return hint + "\n\nTap Add Alert to create one.", None

    total_pages = max(1, (len(alerts) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = alerts[page * per_page:(page + 1) * per_page]

    keyboard = []
    lines = [f"*Active Alerts* ({len(alerts)})" + (f" — page {page + 1}/{total_pages}\n" if total_pages > 1 else "\n")]
    for alert in chunk:
        alert_id = db.alert_field(alert, "id")
        symbol = db.alert_field(alert, "symbol", "")
        target = db.alert_field(alert, "target", 0)
        condition = db.alert_field(alert, "condition", "above")
        is_persistent = bool(db.alert_field(alert, "is_persistent", 0))
        atype = db.alert_field(alert, "alert_type", "price") or "price"
        expires = db.alert_field(alert, "expires_at")
        snoozed = db.alert_field(alert, "snoozed_until")
        pct = db.alert_field(alert, "pct")
        window_min = db.alert_field(alert, "window_min")

        coin = _escape_md(symbol.replace("USDT", ""))
        if atype == "trail":
            desc = f"trail {pct}%"
        elif atype in ("pct", "move"):
            desc = f"{pct}% {'in ' + str(window_min) + 'm' if atype == 'move' else ''}".strip()
        else:
            desc = f"{condition} *{format_price(target)}*"
        flags = []
        if is_persistent or atype in ("pct", "trail", "move"):
            flags.append("repeat")
        if snoozed:
            flags.append(f"snoozed till {_fmt_ts(snoozed)}")
        if expires:
            flags.append(f"expires {_fmt_ts(expires)}")
        flag_txt = f" [{', '.join(flags)}]" if flags else " [once]"
        lines.append(f"  #{alert_id}{flag_txt} *{coin}* {desc}")
        keyboard.append([
            InlineKeyboardButton(f"Edit #{alert_id}", callback_data=f"edit_{alert_id}"),
            InlineKeyboardButton(f"Remove #{alert_id}", callback_data=f"remove_{alert_id}"),
        ])

    lines.append("")
    symbols = sorted({a[1] for a in chunk})
    tickers = await get_tickers(symbols)
    for symbol in symbols:
        price = engine.last_prices.get(symbol) or ticker_price(tickers.get(symbol))
        if price is None:
            price = cached_price(symbol)
        if price is None:
            price = await get_current_price(symbol)
        if price is not None:
            coin = _escape_md(symbol.replace("USDT", ""))
            pct = ticker_change_24h(tickers.get(symbol) or cached_ticker(symbol))
            extra = f" ({_fmt_pct(pct)})" if pct is not None else ""
            lines.append(f"  {coin}: {format_price(price)}{extra}")

    nav = []
    tag = f"|{filt}" if filt else ""
    if page > 0:
        nav.append(InlineKeyboardButton("< Prev", callback_data=f"list_{page - 1}{tag}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next >", callback_data=f"list_{page + 1}{tag}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("Refresh List", callback_data=f"refresh_list_{page}{tag}")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def _create_one_alert(symbol, current_price, condition, arg, is_trail,
                            is_move, is_funding, is_persistent, expires_at,
                            cooldown_sec, window_min=None):
    """Create a single alert row. Returns (id, description)."""
    if is_trail:
        pct = float(arg.rstrip("%"))
        if not (0 < pct <= 50):
            raise ValueError
        aid = await db.add_alert(symbol, current_price, "below", True,
                                 alert_type="trail", pct=pct, base_price=current_price,
                                 peak_price=current_price, expires_at=expires_at,
                                 cooldown_sec=cooldown_sec)
        return aid, f"trail {pct:g}%"
    if is_move:
        pct = float(arg.rstrip("%"))
        if not (0 < pct <= 50) or not window_min or not (1 <= window_min <= 1440):
            raise ValueError
        aid = await db.add_alert(symbol, current_price, condition, True,
                                 alert_type="move", pct=pct, window_min=window_min,
                                 base_price=current_price, peak_price=current_price,
                                 expires_at=expires_at, cooldown_sec=cooldown_sec)
        return aid, f"{pct:g}% in {window_min}m"
    if is_funding:
        pct = float(arg.rstrip("%"))
        if not (0 < abs(pct) <= 5):
            raise ValueError
        aid = await db.add_alert(symbol, current_price, condition, True,
                                 alert_type="funding", funding_rate=pct / 100,
                                 expires_at=expires_at, cooldown_sec=cooldown_sec)
        return aid, f"funding {pct:g}%"
    if arg.endswith("%"):
        percent = float(arg.rstrip("%"))
        if not (0 < percent <= 1000):
            raise ValueError
        target = current_price * (1 + percent / 100) if condition == "above" else current_price * (1 - percent / 100)
        if (condition == "above" and current_price >= target) or (
                condition == "below" and current_price <= target):
            raise RuntimeError("already-hit")
        aid = await db.add_alert(symbol, target, condition, is_persistent,
                                 alert_type="price", expires_at=expires_at,
                                 cooldown_sec=cooldown_sec)
        return aid, f"{condition} {format_price(target)}"
    target = float(arg.replace(",", ""))
    if not (0 < target <= MAX_PRICE_VALUE):
        raise ValueError
    if current_price is not None:
        if (condition == "above" and current_price >= target) or (
                condition == "below" and current_price <= target):
            raise RuntimeError("already-hit")
    aid = await db.add_alert(symbol, target, condition, is_persistent,
                             alert_type="price", expires_at=expires_at,
                             cooldown_sec=cooldown_sec)
    return aid, f"{condition} {format_price(target)}"


@authorized
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add — price, %, ladder, trail, move, funding. See /help for examples."""
    usage = (
        "Usage:\n"
        "`/add BTC 72500 above` [repeat] [7d] [cooldown=15m]\n"
        "`/add ETH 5% up repeat`\n"
        "`/add BTC,ETH 80000,4000 above`\n"
        "`/add BTC trail 5% below`\n"
        "`/add BTC move 3% 60m`\n"
        "`/add BTC funding 0.01% above`"
    )

    if not _check_rate_limit(update.effective_user.id):
        await update.message.reply_text("Slow down — try again in a second.")
        return

    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text(usage, parse_mode="Markdown")
        return
    max_alerts = _max_alerts()
    raw_coins, price_arg = args[0], args[1]
    cond_arg = args[2].lower()
    rest = [a.lower() for a in args[3:]]
    is_persistent = "repeat" in rest
    expires_at = None
    for token in rest:
        parsed = _parse_expiry(token)
        if parsed == "invalid":
            await update.message.reply_text("Bad expiry — use like `7d`, `12h`, `30m`.", parse_mode="Markdown")
            return
        if parsed:
            expires_at = parsed
    cooldown_sec = None
    for token in rest:
        if token.startswith("cooldown="):
            raw = token.split("=", 1)[1]
            try:
                if raw.endswith("m"):
                    cooldown_sec = int(raw[:-1]) * 60
                elif raw.endswith("h"):
                    cooldown_sec = int(raw[:-1]) * 3600
                elif raw.endswith("d"):
                    cooldown_sec = int(raw[:-1]) * 86400
                else:
                    cooldown_sec = int(raw.rstrip("s"))
            except ValueError:
                cooldown_sec = None
            if cooldown_sec is None or not (60 <= cooldown_sec <= 30 * 86400):
                await update.message.reply_text("Bad cooldown — e.g. `cooldown=15m` (min 60s).", parse_mode="Markdown")
                return
    if cooldown_sec and not is_persistent:
        is_persistent = True
    if cond_arg == "up":
        cond_arg = "above"
    elif cond_arg == "down":
        cond_arg = "below"
    condition = cond_arg
    if condition not in ("above", "below"):
        await update.message.reply_text("Condition must be `above`/`up` or `below`/`down`.", parse_mode="Markdown")
        return
    coins = parse_symbols(raw_coins)
    targets_raw = [t.strip() for t in price_arg.split(",") if t.strip()]
    ladder = len(targets_raw) > 1
    if not coins or not targets_raw:
        await update.message.reply_text(usage, parse_mode="Markdown")
        return
    if ladder and len(coins) != len(targets_raw):
        await update.message.reply_text("Ladder needs same count: `/add BTC,ETH 80000,4000 above`.", parse_mode="Markdown")
        return
    if len(coins) > 10:
        await update.message.reply_text("Max 10 coins per /add.")
        return
    if await db.count_alerts() + len(coins) > max_alerts:
        await update.message.reply_text(f"Alert limit reached ({max_alerts}). Remove one first.")
        return
    kind = price_arg.lower()
    is_trail = kind == "trail" and len(args) > 3 and args[3].endswith("%")
    is_move = kind == "move" and len(args) > 4
    is_funding = kind == "funding"
    window_min = None
    if is_move:
        try:
            window_min = int(args[4].rstrip("mM"))
        except ValueError:
            window_min = None
    ws = context.bot_data["ws"]
    engine = context.bot_data["engine"]
    created = []
    for i, raw_coin in enumerate(coins):
        symbol = normalize_symbol(raw_coin)
        if not is_valid_symbol(symbol):
            await update.message.reply_text(f"Invalid symbol `{_escape_md(raw_coin)}`.", parse_mode="Markdown")
            return
        resolved = await _resolve_symbol(symbol, engine)
        if resolved is None:
            await update.message.reply_text(f"Invalid symbol `{_escape_md(symbol)}`.", parse_mode="Markdown")
            return
        symbol, current_price = resolved
        if current_price is None:
            current_price = engine.last_prices.get(symbol)
        if current_price is not None:
            engine.last_prices[symbol] = current_price
        arg = targets_raw[i] if ladder else (args[3] if is_trail or is_move or is_funding else price_arg)
        try:
            aid, desc = await _create_one_alert(
                symbol, current_price, condition, arg, is_trail,
                is_move, is_funding, is_persistent, expires_at,
                cooldown_sec, window_min)
        except RuntimeError:
            await update.message.reply_text(
                f"Already true for {_escape_md(symbol.replace('USDT', ''))} "
                f"(now {format_price(current_price)}).", parse_mode="Markdown")
            return
        except ValueError:
            await update.message.reply_text(f"Invalid value `{_escape_md(arg)}`. See /help.", parse_mode="Markdown")
            return
        await ws.subscribe(symbol)
        created.append((aid, symbol, desc, current_price))
    lines = []
    for aid, symbol, desc, now_price in created:
        coin = _escape_md(symbol.replace("USDT", ""))
        lines.append(f"#{aid} *{coin}* {desc} (now {format_price(now_price)})")
    tag = " [repeat]" if is_persistent or is_trail or is_move else ""
    if expires_at:
        tag += f" [expires {_fmt_ts(expires_at)}]"
    await update.message.reply_text(f"Alert(s) created{tag}:\n" + "\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/remove <id>"""
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: `/remove <id>`\nUse `/list` to see alert IDs.", parse_mode="Markdown")
        return

    try:
        alert_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid ID. Use /list to see alert IDs.")
        return

    alert = await db.get_alert(alert_id)
    if not alert:
        await update.message.reply_text(f"Alert #{alert_id} not found.")
        return

    symbol = alert[1]
    await db.remove_alert(alert_id)

    remaining = await db.get_alerts_for_symbol(symbol)
    if not remaining:
        ws = context.bot_data["ws"]
        await ws.unsubscribe(symbol)

    coin = _escape_md(symbol.replace("USDT", ""))
    await update.message.reply_text(f"Alert #{alert_id} removed (*{coin}* {alert[3]} {format_price(alert[2])})", parse_mode="Markdown")


@authorized
async def cmd_removeall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/removeall — remove all active alerts (requires confirmation)."""
    args = context.args or []
    if not args or args[0].lower() != "confirm":
        count = await db.count_alerts()
        if count == 0:
            await update.message.reply_text("No active alerts to remove.")
            return
        kb = [[InlineKeyboardButton(f"Yes, remove all {count}", callback_data="removeall_confirm")]]
        await update.message.reply_text(
            f"This will delete all *{count}* alert(s). Tap to confirm, or ignore to keep them.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return
    symbols = await db.get_active_symbols()
    count = await db.remove_all_alerts()
    ws = context.bot_data["ws"]
    for symbol in symbols:
        await ws.unsubscribe(symbol)
    await update.message.reply_text(f"Removed all {count} alert(s).")


@authorized
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list [coin] — show alerts, paginated, optionally filtered by coin."""
    engine = context.bot_data["engine"]
    filt = normalize_symbol(context.args[0]) if context.args else None
    if filt and not is_valid_symbol(filt):
        await update.message.reply_text("Invalid coin filter.")
        return
    text, markup = await get_list_text_and_markup(engine, filt=filt)
    if markup:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode="Markdown")


@authorized
async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/price [coin...] — no args = all watchlist coins at once."""
    engine = context.bot_data["engine"]
    if not context.args:
        await _send_all_prices(update.message, engine)
        return
    symbols = parse_symbols(" ".join(context.args).replace(" ", ","))
    symbols = [s for s in symbols if is_valid_symbol(s)][:10]
    if not symbols:
        await update.message.reply_text("Usage: `/price BTC` or `/price` for all.", parse_mode="Markdown")
        return
    if len(symbols) == 1:
        symbol = symbols[0]
        ticker = await get_ticker(symbol)
        price = ticker_price(ticker)
        if price is None:
            price = engine.last_prices.get(symbol)
        coin = _escape_md(symbol.replace("USDT", ""))
        if price:
            extra = ""
            pct = ticker_change_24h(ticker or cached_ticker(symbol))
            if pct is not None:
                extra = f" ({_fmt_pct(pct)} 24h)"
            await update.message.reply_text(f"*{coin}*: {format_price(price)}{extra}", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"Could not fetch price for {coin} (unknown symbol or API issue).")
        return
    tickers = await get_tickers(symbols)
    lines = ["*Prices*\n"]
    for symbol in symbols:
        coin = _escape_md(symbol.replace("USDT", ""))
        price = engine.last_prices.get(symbol) or ticker_price(tickers.get(symbol))
        if price is None:
            lines.append(f"  *{coin}*: unavailable")
            continue
        pct = ticker_change_24h(tickers.get(symbol) or cached_ticker(symbol))
        extra = f" ({_fmt_pct(pct)} 24h)" if pct is not None else ""
        lines.append(f"  *{coin}*: {format_price(price)}{extra}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — connection health, prices tracked, engine stats."""
    engine = context.bot_data["engine"]
    ws = context.bot_data["ws"]
    symbols = sorted(ws.subscribed_symbols) if ws else []
    stats = engine.get_stats() if hasattr(engine, "get_stats") else {}
    conn = "connected" if getattr(ws, "connected", False) else "disconnected"
    import datetime as _dt
    uptime = ""
    try:
        started = getattr(engine, "started_at", None)
        if started:
            delta = _dt.datetime.now(_dt.timezone.utc) - started
            hrs, rem = divmod(int(delta.total_seconds()), 3600)
            mins = rem // 60
            uptime = f"  |  Uptime {hrs}h {mins}m" if hrs else f"  |  Uptime {mins}m"
    except Exception:
        pass
    try:
        pending = await db.pending_count()
    except Exception:
        pending = 0
    lines = [
        "*Status*",
        f"Connection: {conn}{uptime}",
        f"Tracked symbols: {len(symbols)}",
        f"Checks: {stats.get('checks', 0)}  |  Triggered: {stats.get('triggered', 0)}"
        + (f"  |  Errors: {stats['errors']}" if stats.get("errors") else ""),
        f"Queued notifications: {pending}",
    ]
    try:
        last = getattr(ws, "last_tick_at", {}) or {}
        stale = [s for s in symbols if (engine.last_update_at.get(s) is None)]
        if stale:
            lines.append(f"No WS data yet: {', '.join(sorted(s.replace('USDT', '') for s in stale[:8]))}")
        _ = last
    except Exception:
        pass
    if symbols:
        tickers = await get_tickers(symbols[:10])
        for s in symbols[:10]:
            p = engine.last_prices.get(s) or ticker_price(tickers.get(s))
            if p is None:
                p = cached_price(s)
            extra = ""
            pct = ticker_change_24h(tickers.get(s) or cached_ticker(s))
            if pct is not None:
                extra = f" ({_fmt_pct(pct)})"
            lines.append(f"  {_escape_md(s.replace('USDT', ''))}: {format_price(p) if p else 'no data yet'}{extra}")
        if len(symbols) > 10:
            lines.append(f"  ...and {len(symbols) - 10} more")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help or /start — show available commands."""
    engine = context.bot_data["engine"]
    if not config.TELEGRAM_USER_ID:
        await update.message.reply_text(
            "Bot is not locked to an owner yet. Set `TELEGRAM_USER_ID` in `.env` "
            "(get yours from @userinfobot), restart, then send /help again.",
            parse_mode="Markdown")
        return
    text = (
        "*Crypto Price Alert Bot*\n\n"
        "*Alerts:*\n"
        "`/add BTC 72500 above` [repeat] [7d] [cooldown=15m]\n"
        "`/add ETH 5% up repeat`\n"
        "`/add BTC,ETH 80000,4000 above` (ladder)\n"
        "`/add BTC trail 5% below` (peak pullback)\n"
        "`/add BTC move 3% 60m` (% in window)\n"
        "`/add BTC funding 0.01% above`\n"
        "`/edit 3 76000 above` — change target\n"
        "`/snooze 3 24h` — silence one alert (`/unsnooze 3`)\n"
        "`/preset dip-buy` — BTC/ETH/SOL -5% repeat\n\n"
        "*View:*\n"
        "`/list` [coin] — paginated alerts\n"
        "`/price` — all coins at once, or `/price BTC ETH`\n"
        "`/movers` — top 24h movers\n"
        "`/history` — recently fired alerts\n"
        "`/status` — health, `/health` — watchdog\n\n"
        "*Manage:*\n"
        "`/remove 3`, `/removeall` (asks first)\n"
        "`/pause [15m|2h]`, `/resume`\n"
        "`/watch BTC ETH` / `/unwatch BTC` / `/watchlist`\n"
        "`/export` backup file, `/import` restore (reply to file)\n"
        "`/backup` database file\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=get_main_keyboard(engine))


# ---------------------------------------------------------------------------
# New commands: edit / snooze / pause / movers / history / health
# ---------------------------------------------------------------------------

def _parse_duration(text: str):
    """Parse '15m'/'2h'/'7d'/seconds -> seconds. None if invalid."""
    text = (text or "").strip().lower()
    try:
        if text.endswith("m"):
            return int(text[:-1]) * 60
        if text.endswith("h"):
            return int(text[:-1]) * 3600
        if text.endswith("d"):
            return int(text[:-1]) * 86400
        return int(text.rstrip("s"))
    except ValueError:
        return None


@authorized
async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/edit <id> <price> [above|below]."""
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: `/edit 3 76000 above`", parse_mode="Markdown")
        return
    try:
        alert_id = int(args[0])
        target = float(args[1].replace(",", ""))
    except ValueError:
        await update.message.reply_text("Usage: `/edit 3 76000 above`", parse_mode="Markdown")
        return
    if not (0 < target <= MAX_PRICE_VALUE):
        await update.message.reply_text("Target price is out of range.")
        return
    alert = await db.get_alert(alert_id)
    if not alert:
        await update.message.reply_text(f"Alert #{alert_id} not found.")
        return
    atype = db.alert_field(alert, "alert_type", "price") or "price"
    if atype != "price":
        await update.message.reply_text("Only price alerts can be edited.")
        return
    condition = (args[2].lower() if len(args) > 2 else db.alert_field(alert, "condition", "above"))
    if condition == "up":
        condition = "above"
    if condition == "down":
        condition = "below"
    if condition not in ("above", "below"):
        await update.message.reply_text("Condition must be `above` or `below`.", parse_mode="Markdown")
        return
    try:
        await db.set_target(alert_id, target, condition)
    except ValueError as e:
        await update.message.reply_text(f"Could not edit: {e}")
        return
    coin = _escape_md(db.alert_field(alert, "symbol", "").replace("USDT", ""))
    await update.message.reply_text(
        f"Alert #{alert_id} updated: *{coin}* {condition} *{format_price(target)}*.", parse_mode="Markdown")


@authorized
async def cmd_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/snooze <id> <15m|2h|24h>."""
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: `/snooze 3 24h`", parse_mode="Markdown")
        return
    try:
        alert_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Usage: `/snooze 3 24h`", parse_mode="Markdown")
        return
    secs = _parse_duration(args[1])
    if not secs or not (60 <= secs <= 30 * 86400):
        await update.message.reply_text("Duration 1m..30d, e.g. `/snooze 3 24h`.", parse_mode="Markdown")
        return
    if not await db.get_alert(alert_id):
        await update.message.reply_text(f"Alert #{alert_id} not found.")
        return
    import datetime as _dt
    until = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S")
    await db.set_snooze(alert_id, until)
    await update.message.reply_text(f"Alert #{alert_id} snoozed till {_fmt_ts(until)}.")


@authorized
async def cmd_unsnooze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unsnooze <id>"""
    if not context.args:
        await update.message.reply_text("Usage: `/unsnooze 3`", parse_mode="Markdown")
        return
    try:
        alert_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: `/unsnooze 3`", parse_mode="Markdown")
        return
    if not await db.get_alert(alert_id):
        await update.message.reply_text(f"Alert #{alert_id} not found.")
        return
    await db.set_snooze(alert_id, None)
    await update.message.reply_text(f"Alert #{alert_id} unsnoozed.")


@authorized
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pause [15m|2h] — mute all alerts."""
    engine = context.bot_data["engine"]
    secs = None
    if context.args:
        secs = _parse_duration(context.args[0])
        if not secs or not (60 <= secs <= 24 * 3600):
            await update.message.reply_text("Usage: `/pause` or `/pause 15m` / `/pause 2h`.", parse_mode="Markdown")
            return
    hours = (secs / 3600) if secs else config.PAUSE_DURATION_HOURS
    engine.pause_alerts(hours)
    label = f"{secs // 60}m" if secs and secs < 3600 else f"{hours:g}h"
    await update.message.reply_text(f"Alerts paused for {label}.", reply_markup=get_main_keyboard(engine))


@authorized
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = context.bot_data["engine"]
    engine.resume_alerts()
    await update.message.reply_text("Alerts resumed.", reply_markup=get_main_keyboard(engine))


@authorized
async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/movers [n] — top 24h movers by absolute change."""
    n = 8
    if context.args:
        try:
            n = max(3, min(15, int(context.args[0])))
        except ValueError:
            pass
    rows = await get_top_movers(n)
    if not rows:
        await update.message.reply_text("Could not fetch movers right now.")
        return
    lines = ["*Top 24h movers*\n"]
    for symbol, price, pct in rows:
        coin = _escape_md(symbol.replace("USDT", ""))
        arrow = "up" if pct >= 0 else "down"
        lines.append(f"  *{coin}*: {format_price(price)} ({_fmt_pct(pct)} {arrow})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/history [n] — recently fired alerts."""
    n = 10
    if context.args:
        try:
            n = max(1, min(30, int(context.args[0])))
        except ValueError:
            pass
    rows = await db.get_fired_history(n)
    if not rows:
        await update.message.reply_text("No fired alerts yet.")
        return
    lines = ["*Recently fired*\n"]
    for aid, symbol, condition, target, price, detail, fired_at in rows:
        coin = _escape_md((symbol or "").replace("USDT", ""))
        extra = f" {detail}" if detail else ""
        lines.append(
            f"  #{aid} *{coin}* {condition} {format_price(target)}"
            f" @ {format_price(price)}{extra} — {_fmt_ts(fired_at)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/health — reconnects, queue, stream freshness."""
    engine = context.bot_data["engine"]
    ws = context.bot_data["ws"]
    import datetime as _dt
    stats = engine.get_stats() if hasattr(engine, "get_stats") else {}
    try:
        expired = await db.prune_expired()
    except Exception:
        expired = 0
    try:
        pending = await db.pending_count()
    except Exception:
        pending = 0
    lines = [
        "*Health*",
        f"WS connected: {'yes' if getattr(ws, 'connected', False) else 'no'}"
        f"  |  Reconnects: {getattr(ws, 'reconnects', 0)}",
        f"Checks: {stats.get('checks', 0)}  Triggered: {stats.get('triggered', 0)}"
        f"  Errors: {stats.get('errors', 0)}",
        f"Queued notifications: {pending}"
        + (f"  |  Pruned expired: {expired}" if expired else ""),
    ]
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        stale = []
        for sym, ts in (getattr(engine, "last_update_at", {}) or {}).items():
            age = (now - ts).total_seconds() if ts else 1e9
            if age > 300:
                stale.append(f"{sym.replace('USDT', '')} {int(age // 60)}m")
        tracked = sorted((getattr(ws, "subscribed_symbols", None) or set()))
        missing = [s for s in tracked if s not in (getattr(engine, "last_update_at", {}) or {})]
        for s in missing[:5]:
            stale.append(f"{s.replace('USDT', '')} never")
        lines.append(("Stale: " + ", ".join(stale[:8])) if stale else "Streams: fresh")
    except Exception:
        pass
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/watch BTC ETH — add coins to the price board."""
    if not context.args:
        await update.message.reply_text("Usage: `/watch BTC ETH HYPE`", parse_mode="Markdown")
        return
    added, unknown = [], []
    for raw in context.args:
        symbol = normalize_symbol(raw)
        if not is_valid_symbol(symbol):
            unknown.append(raw)
            continue
        if await get_current_price(symbol) is None:
            unknown.append(raw)
            continue
        try:
            if await db.add_watch(symbol):
                added.append(symbol.replace("USDT", ""))
        except ValueError:
            unknown.append(raw)
    msg = ""
    if added:
        msg += "Watching: " + ", ".join(added) + "."
    if unknown:
        msg += (" " if msg else "") + "Unknown: " + ", ".join(unknown) + "."
    await update.message.reply_text(msg or "Nothing changed.")


@authorized
async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: `/unwatch BTC`", parse_mode="Markdown")
        return
    removed = []
    for raw in context.args:
        if await db.remove_watch(raw):
            removed.append(normalize_symbol(raw).replace("USDT", ""))
    await update.message.reply_text(
        ("Stopped watching: " + ", ".join(removed) + ".") if removed else "None of those were watched.")


@authorized
async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = context.bot_data["engine"]
    await _send_all_prices(update.message, engine, prefix="*Watchlist*\n")


@authorized
async def cmd_preset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/preset dip-buy | breakout — one-tap alert bundles."""
    name = (context.args[0].lower() if context.args else "")
    presets = {
        "dip-buy": [("BTCUSDT", 5, "below"), ("ETHUSDT", 5, "below"), ("SOLUSDT", 5, "below")],
        "breakout": [("BTCUSDT", 3, "above"), ("ETHUSDT", 3, "above"), ("HYPEUSDT", 5, "above")],
    }
    if name not in presets:
        await update.message.reply_text("Usage: `/preset dip-buy` or `/preset breakout`", parse_mode="Markdown")
        return
    if await db.count_alerts() + len(presets[name]) > _max_alerts():
        await update.message.reply_text(f"Not enough room (limit {_max_alerts()}).")
        return
    ws = context.bot_data["ws"]
    engine = context.bot_data["engine"]
    made = []
    for symbol, pct, direction in presets[name]:
        price = await get_current_price(symbol)
        if not price:
            continue
        target = price * (1 + pct / 100) if direction == "above" else price * (1 - pct / 100)
        aid = await db.add_alert(symbol, target, direction, True, alert_type="price")
        await ws.subscribe(symbol)
        engine.last_prices[symbol] = price
        made.append(f"#{aid} {symbol.replace('USDT', '')} {direction} {format_price(target)}")
    if not made:
        await update.message.reply_text("Could not fetch prices for the preset.")
        return
    await update.message.reply_text(f"Preset *{name}* created:\n" + "\n".join(made), parse_mode="Markdown")


@authorized
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export — send alerts + watchlist as a JSON backup file."""
    import io
    import json
    alerts = await db.get_all_alerts()
    cols = ["id", "symbol", "target", "condition", "created_at", "is_persistent",
            "last_triggered_at", "alert_type", "expires_at", "snoozed_until",
            "cooldown_sec", "pct", "window_min", "base_price", "peak_price", "funding_rate"]
    data = {"alerts": [dict(zip(cols, list(a) + [None] * (len(cols) - len(a)))) for a in alerts]}
    try:
        data["watchlist"] = await db.get_watchlist()
    except Exception:
        data["watchlist"] = []
    buf = io.BytesIO(json.dumps(data, indent=2, default=str).encode("utf-8"))
    buf.name = "alerts-backup.json"
    await update.message.reply_document(document=buf, caption=f"Backup: {len(alerts)} alert(s).")


@authorized
async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/import — reply to a JSON backup file with /import to restore."""
    doc = None
    if update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
    if not doc:
        await update.message.reply_text("Reply to your backup JSON file with `/import`.", parse_mode="Markdown")
        return
    try:
        f = await doc.get_file()
        raw = await f.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f"Could not download file: {e}")
        return
    import json
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except Exception:
        await update.message.reply_text("That file is not valid JSON.")
        return
    items = data.get("alerts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        await update.message.reply_text("Backup has no `alerts` list.")
        return
    ws = context.bot_data["ws"]
    engine = context.bot_data["engine"]
    made, skipped = 0, 0
    for item in items[:100]:
        try:
            symbol = normalize_symbol(str(item.get("symbol", "")))
            target = float(item.get("target", 0))
            condition = str(item.get("condition", "above")).lower()
            if not is_valid_symbol(symbol) or condition not in ("above", "below"):
                skipped += 1
                continue
            price = await get_current_price(symbol)
            if price is None:
                skipped += 1
                continue
            atype = str(item.get("alert_type", "price") or "price")
            if atype not in ("price", "pct", "trail", "move", "funding"):
                atype = "price"
            await db.add_alert(
                symbol, target, condition, bool(item.get("is_persistent", 0)),
                alert_type=atype, expires_at=item.get("expires_at"),
                cooldown_sec=item.get("cooldown_sec"), pct=item.get("pct"),
                window_min=item.get("window_min"),
                base_price=item.get("base_price") or price,
                peak_price=item.get("peak_price") or price,
                funding_rate=item.get("funding_rate"))
            await ws.subscribe(symbol)
            engine.last_prices[symbol] = price
            made += 1
        except Exception:
            skipped += 1
    for sym in (data.get("watchlist") or [])[:25]:
        try:
            if is_valid_symbol(sym) and await get_current_price(sym) is not None:
                await db.add_watch(sym)
        except Exception:
            pass
    await update.message.reply_text(f"Imported {made} alert(s){f', skipped {skipped}' if skipped else ''}.")


@authorized
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backup — send the raw SQLite database file."""
    import os as _os
    if not _os.path.exists(db.DB_PATH):
        await update.message.reply_text("No database file yet.")
        return
    try:
        with open(db.DB_PATH, "rb") as f:
            await update.message.reply_document(document=f, caption="Database backup.")
    except Exception as e:
        await update.message.reply_text(f"Backup failed: {e}")


# ---------------------------------------------------------------------------
# Interactive UI Handlers
# ---------------------------------------------------------------------------

def _parse_duration(text: str):
    """Parse '15m'/'2h'/'7d'/seconds -> seconds. None if invalid."""
    text = (text or "").strip().lower()
    try:
        if text.endswith("m"):
            return int(text[:-1]) * 60
        if text.endswith("h"):
            return int(text[:-1]) * 3600
        if text.endswith("d"):
            return int(text[:-1]) * 86400
        return int(text.rstrip("s"))
    except ValueError:
        return None


@authorized
async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/edit <id> <price> [above|below] — change a price alert's target."""
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: `/edit 3 76000 above`", parse_mode="Markdown")
        return
    try:
        alert_id = int(args[0])
        target = float(args[1].replace(",", ""))
    except ValueError:
        await update.message.reply_text("Usage: `/edit 3 76000 above`", parse_mode="Markdown")
        return
    if not (0 < target <= MAX_PRICE_VALUE):
        await update.message.reply_text("Target price is out of range.")
        return
    alert = await db.get_alert(alert_id)
    if not alert:
        await update.message.reply_text(f"Alert #{alert_id} not found.")
        return
    atype = db.alert_field(alert, "alert_type", "price") or "price"
    if atype != "price":
        await update.message.reply_text("Only price alerts can be edited — recreate trail/move ones.")
        return
    condition = (args[2].lower() if len(args) > 2 else db.alert_field(alert, "condition", "above"))
    if condition == "up":
        condition = "above"
    if condition == "down":
        condition = "below"
    if condition not in ("above", "below"):
        await update.message.reply_text("Condition must be `above` or `below`.", parse_mode="Markdown")
        return
    try:
        await db.set_target(alert_id, target, condition)
    except ValueError as e:
        await update.message.reply_text(f"Could not edit: {e}")
        return
    coin = _escape_md(db.alert_field(alert, "symbol", "").replace("USDT", ""))
    await update.message.reply_text(f"Alert #{alert_id} updated: *{coin}* {condition} *{format_price(target)}*.",
                                    parse_mode="Markdown")


@authorized
async def cmd_snooze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/snooze <id> <15m|2h|24h> — silence one alert. /unsnooze <id> to undo."""
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: `/snooze 3 24h`", parse_mode="Markdown")
        return
    try:
        alert_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Usage: `/snooze 3 24h`", parse_mode="Markdown")
        return
    secs = _parse_duration(args[1])
    if not secs or not (60 <= secs <= 30 * 86400):
        await update.message.reply_text("Duration 1m..30d, e.g. `/snooze 3 24h`.", parse_mode="Markdown")
        return
    alert = await db.get_alert(alert_id)
    if not alert:
        await update.message.reply_text(f"Alert #{alert_id} not found.")
        return
    import datetime as _dt
    until = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S")
    await db.set_snooze(alert_id, until)
    await update.message.reply_text(f"Alert #{alert_id} snoozed till {_fmt_ts(until)}.")


@authorized
async def cmd_unsnooze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unsnooze <id>"""
    if not context.args:
        await update.message.reply_text("Usage: `/unsnooze 3`", parse_mode="Markdown")
        return
    try:
        alert_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Usage: `/unsnooze 3`", parse_mode="Markdown")
        return
    alert = await db.get_alert(alert_id)
    if not alert:
        await update.message.reply_text(f"Alert #{alert_id} not found.")
        return
    await db.set_snooze(alert_id, None)
    await update.message.reply_text(f"Alert #{alert_id} unsnoozed.")


@authorized
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pause [15m|2h] — mute all alerts (default PAUSE_DURATION_HOURS)."""
    engine = context.bot_data["engine"]
    secs = None
    if context.args:
        secs = _parse_duration(context.args[0])
        if not secs or not (60 <= secs <= 24 * 3600):
            await update.message.reply_text("Usage: `/pause` or `/pause 15m` / `/pause 2h`.", parse_mode="Markdown")
            return
    hours = (secs / 3600) if secs else config.PAUSE_DURATION_HOURS
    engine.pause_alerts(hours)
    label = f"{secs // 60}m" if secs and secs < 3600 else f"{hours:g}h"
    await update.message.reply_text(f"Alerts paused for {label}.", reply_markup=get_main_keyboard(engine))


@authorized
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = context.bot_data["engine"]
    engine.resume_alerts()
    await update.message.reply_text("Alerts resumed.", reply_markup=get_main_keyboard(engine))


@authorized
async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/movers [n] — top 24h movers by absolute change."""
    n = 8
    if context.args:
        try:
            n = max(3, min(15, int(context.args[0])))
        except ValueError:
            pass
    rows = await get_top_movers(n)
    if not rows:
        await update.message.reply_text("Could not fetch movers right now.")
        return
    lines = ["*Top 24h movers*\n"]
    for symbol, price, pct in rows:
        coin = _escape_md(symbol.replace("USDT", ""))
        arrow = "up" if pct >= 0 else "down"
        lines.append(f"  *{coin}*: {format_price(price)} ({_fmt_pct(pct)} {arrow})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/history [n] — recently fired alerts."""
    n = 10
    if context.args:
        try:
            n = max(1, min(30, int(context.args[0])))
        except ValueError:
            pass
    rows = await db.get_fired_history(n)
    if not rows:
        await update.message.reply_text("No fired alerts yet.")
        return
    lines = ["*Recently fired*\n"]
    for aid, symbol, condition, target, price, detail, fired_at in rows:
        coin = _escape_md((symbol or "").replace("USDT", ""))
        extra = f" {detail}" if detail else ""
        lines.append(f"  #{aid} *{coin}* {condition} {format_price(target)} @ {format_price(price)}{extra} — {_fmt_ts(fired_at)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/health — watchdog view: reconnects, queue, per-symbol data age."""
    engine = context.bot_data["engine"]
    ws = context.bot_data["ws"]
    import datetime as _dt
    stats = engine.get_stats() if hasattr(engine, "get_stats") else {}
    try:
        expired = await db.prune_expired()
    except Exception:
        expired = 0
    try:
        pending = await db.pending_count()
    except Exception:
        pending = 0
    lines = [
        "*Health*",
        f"WS connected: {'yes' if getattr(ws, 'connected', False) else 'no'}"
        f"  |  Reconnects: {getattr(ws, 'reconnects', 0)}",
        f"Checks: {stats.get('checks', 0)}  Triggered: {stats.get('triggered', 0)}"
        f"  Errors: {stats.get('errors', 0)}",
        f"Queued notifications: {pending}"
        + (f"  |  Pruned expired: {expired}" if expired else ""),
    ]
    try:
        now = _dt.datetime.now(_dt.timezone.utc)
        stale = []
        for sym, ts in (getattr(engine, "last_update_at", {}) or {}).items():
            age = (now - ts).total_seconds() if ts else 1e9
            if age > 300:
                stale.append(f"{sym.replace('USDT', '')} {int(age // 60)}m")
        tracked = sorted((getattr(ws, "subscribed_symbols", None) or set()))
        missing = [s for s in tracked if s not in (getattr(engine, "last_update_at", {}) or {})]
        for s in missing[:5]:
            stale.append(f"{s.replace('USDT', '')} never")
        lines.append(("Stale: " + ", ".join(stale[:8])) if stale else "Streams: fresh")
    except Exception:
        pass
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@authorized
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text from interactive buttons or wizard states."""
    engine = context.bot_data["engine"]
    text = (update.message.text or "").strip()

    # State machine for Custom Price Wizard
    awaiting_coin = context.user_data.get("awaiting_custom_price")
    if awaiting_coin:
        menu_labels = ("List Alerts", "Check Price", "Add Alert", "Remove Alert",
                       "Pause Alerts", "Resume Alerts", "Help")
        if any(label in text for label in menu_labels) or text.startswith("/"):
            context.user_data.pop("awaiting_custom_price", None)  # Cancel wizard
        else:
            try:
                target = float(text.replace(",", "").strip())
            except ValueError:
                await update.message.reply_text("Please enter a valid number, or tap a menu button to cancel.")
                return
            if not (0 < target <= MAX_PRICE_VALUE):
                await update.message.reply_text("That price is out of range. Try again or tap a menu button to cancel.")
                return
            symbol = normalize_symbol(awaiting_coin)
            resolved = await _resolve_symbol(symbol, engine)
            if resolved is None:
                context.user_data.pop("awaiting_custom_price", None)
                await update.message.reply_text(f"Invalid coin `{_escape_md(awaiting_coin)}`. Wizard cancelled.", parse_mode="Markdown")
                return
            symbol, price = resolved
            if price is None:
                price = engine.last_prices.get(symbol)

            if await db.count_alerts() >= _max_alerts():
                context.user_data.pop("awaiting_custom_price", None)
                await update.message.reply_text(f"Alert limit reached ({_max_alerts()}). Remove one first with /list.")
                return

            if price is not None:
                condition = "above" if target > price else "below"
                if target == price:
                    await update.message.reply_text("Target equals the current price — enter a different value.")
                    return
            else:
                condition = "above" if target > 1000 else "below"

            alert_id = await db.add_alert(symbol, target, condition, False)

            ws = context.bot_data["ws"]
            await ws.subscribe(symbol)
            context.user_data.pop("awaiting_custom_price", None)
            coin = _escape_md(awaiting_coin)
            if price is not None:
                await update.message.reply_text(
                    f"✅ Alert #{alert_id} added: *{coin}* {condition} *{format_price(target)}* "
                    f"(now {format_price(price)}).",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"✅ Alert #{alert_id} added: *{coin}* {condition} *{format_price(target)}*.",
                    parse_mode="Markdown",
                )
            return

    # Normal Main Menu
    if text in ("📋 List Alerts", "List Alerts"):
        await cmd_list(update, context)
    elif text in ("❓ Help", "Help"):
        await cmd_help(update, context)
    elif text in ("➕ Add Alert", "Add Alert"):
        kb = [
            [InlineKeyboardButton("BTC", callback_data="addwiz_coin_BTC"), InlineKeyboardButton("ETH", callback_data="addwiz_coin_ETH")],
            [InlineKeyboardButton("SOL", callback_data="addwiz_coin_SOL"), InlineKeyboardButton("HYPE", callback_data="addwiz_coin_HYPE")]
        ]
        await update.message.reply_text("Select a coin to set an alert for:", reply_markup=InlineKeyboardMarkup(kb))
    elif text in ("💰 Check Price", "Check Price", "Prices: All"):
        await _send_all_prices(update.message, engine)
    elif text in ("❌ Remove Alert", "Remove Alert"):
        await update.message.reply_text("Tap *List Alerts* to see inline delete buttons for all your alerts!", parse_mode="Markdown")
    elif text in ("⏸️ Pause Alerts", "Pause Alerts"):
        engine.pause_alerts(config.PAUSE_DURATION_HOURS)
        await update.message.reply_text(f"Alerts paused for {config.PAUSE_DURATION_HOURS} hour(s).", reply_markup=get_main_keyboard(engine))
    elif text in ("▶️ Resume Alerts", "Resume Alerts"):
        engine.resume_alerts()
        await update.message.reply_text("Alerts resumed.", reply_markup=get_main_keyboard(engine))
    elif text in ("Movers", "Top Movers"):
        await cmd_movers(update, context)
    elif text in ("History",):
        await cmd_history(update, context)
    elif text in ("Watchlist",):
        await cmd_watchlist(update, context)
    elif text and not text.startswith("/"):
        # Unknown free text outside the wizard — guide back to the menu.
        await update.message.reply_text("Use the menu buttons below, or /help for commands.", reply_markup=get_main_keyboard(engine))


@authorized
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline button clicks."""
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass
    data = query.data or ""
    engine = context.bot_data["engine"]

    if data == "prices_all":
        await _send_all_prices(query, engine, context=context)
        return

    if data.startswith("list_"):
        # list_<page>|<filt>
        try:
            rest = data[len("list_"):]
            page_s, _, filt = rest.partition("|")
            page = int(page_s) if page_s else 0
        except ValueError:
            page, filt = 0, None
        text, markup = await get_list_text_and_markup(engine, page=page, filt=filt or None)
        try:
            if markup:
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
            else:
                await query.edit_message_text(text, parse_mode="Markdown")
        except Exception:
            pass
        return

    if data.startswith("refresh_list"):
        rest = data[len("refresh_list"):].lstrip("_")
        page_s, _, filt = rest.partition("|")
        try:
            page = int(page_s) if page_s else 0
        except ValueError:
            page = 0
        text, markup = await get_list_text_and_markup(engine, page=page, filt=filt or None)
        stamp = f"\n_Refreshed: {datetime.datetime.now().strftime('%H:%M:%S')}_"
        try:
            if markup:
                await query.edit_message_text(text + stamp, parse_mode="Markdown", reply_markup=markup)
            else:
                await query.edit_message_text(text + stamp, parse_mode="Markdown")
        except Exception:
            pass
        return

    if data.startswith("edit_"):
        try:
            alert_id = int(data.split("_")[1])
        except (ValueError, IndexError):
            return
        alert = await db.get_alert(alert_id)
        if not alert:
            try:
                await query.edit_message_text(f"Alert #{alert_id} not found.", reply_markup=None)
            except Exception:
                pass
            return
        coin = _escape_md(db.alert_field(alert, "symbol", "").replace("USDT", ""))
        kb = [
            [InlineKeyboardButton("Snooze 12h", callback_data=f"snooze_{alert_id}_12h"),
             InlineKeyboardButton("Snooze 24h", callback_data=f"snooze_{alert_id}_24h")],
            [InlineKeyboardButton("Remove", callback_data=f"remove_{alert_id}")]
        ]
        try:
            await query.edit_message_text(
                f"*{coin}* #{alert_id} — what do you want to do?",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb),
            )
        except Exception:
            pass
        return

    if data.startswith("snooze_"):
        parts = data.split("_")
        if len(parts) != 3:
            return
        try:
            alert_id = int(parts[1])
            hours = int(parts[2].rstrip("h"))
        except ValueError:
            return
        if not await db.get_alert(alert_id):
            return
        import datetime as _dt
        until = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        await db.set_snooze(alert_id, until)
        try:
            text, markup = await get_list_text_and_markup(engine)
            await query.edit_message_text(f"Alert #{alert_id} snoozed for {hours}h.\n\n" + text,
                                          parse_mode="Markdown", reply_markup=markup or None)
        except Exception:
            pass
        return

    if data == "removeall_confirm":
        try:
            symbols = await db.get_active_symbols()
            count = await db.remove_all_alerts()
            ws = context.bot_data["ws"]
            for symbol in symbols:
                await ws.unsubscribe(symbol)
            try:
                await query.edit_message_text(f"Removed all {count} alert(s).", reply_markup=None)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Error handling removeall: {e}")
        return

    if data.startswith("remove_"):
        try:
            alert_id = int(data.split("_")[1])
        except (ValueError, IndexError):
            return
        try:
            alert = await db.get_alert(alert_id)
            if alert:
                symbol = alert[1]
                await db.remove_alert(alert_id)
                remaining = await db.get_alerts_for_symbol(symbol)
                if not remaining:
                    ws = context.bot_data["ws"]
                    await ws.unsubscribe(symbol)
                text, markup = await get_list_text_and_markup(engine)
                prefix = f"Alert #{alert_id} removed.\n\n"
                try:
                    if markup:
                        await query.edit_message_text(prefix + text, parse_mode="Markdown", reply_markup=markup)
                    else:
                        await query.edit_message_text(prefix + text, parse_mode="Markdown")
                except Exception:
                    pass
            else:
                try:
                    await query.edit_message_text(f"Alert #{alert_id} already removed.", reply_markup=None)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Error handling remove: {e}")

    elif data.startswith("price_") or data.startswith("refresh_price_"):
        coin = (data.split("_")[-1] or "").upper()
        symbol = normalize_symbol(coin)
        if not is_valid_symbol(symbol):
            try:
                await query.edit_message_text("Unknown symbol.", reply_markup=None)
            except Exception:
                pass
            return
        price = await get_current_price(symbol)
        coin_safe = _escape_md(symbol.replace("USDT", ""))
        kb = [[InlineKeyboardButton("Refresh", callback_data=f"refresh_price_{coin}")]]
        text = f"*{coin_safe}*: {format_price(price)}" if price else f"Failed to fetch {coin_safe}."
        if data.startswith("refresh_price_"):
            text += f"\n_Updated: {datetime.datetime.now().strftime('%H:%M:%S')}_"
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass

    elif data.startswith("addwiz_coin_"):
        coin = (data.split("_")[-1] or "").upper()
        if not is_valid_symbol(normalize_symbol(coin)):
            try:
                await query.edit_message_text("Unknown coin.", reply_markup=None)
            except Exception:
                pass
            return
        coin_safe = _escape_md(coin)
        kb = [
            [InlineKeyboardButton("5% Pump (Repeat)", callback_data=f"addwiz_type_{coin}_5pump")],
            [InlineKeyboardButton("5% Drop (Repeat)", callback_data=f"addwiz_type_{coin}_5drop")],
            [InlineKeyboardButton("Custom Price", callback_data=f"addwiz_type_{coin}_custom")]
        ]
        try:
            await query.edit_message_text(
                f"Selected *{coin_safe}*. What kind of alert?",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb),
            )
        except Exception:
            pass

    elif data.startswith("addwiz_type_"):
        parts = data.split("_")
        if len(parts) != 4:
            return
        coin = parts[2].upper()
        atype = parts[3]
        symbol = normalize_symbol(coin)
        if not is_valid_symbol(symbol):
            try:
                await query.edit_message_text("Unknown coin.", reply_markup=None)
            except Exception:
                pass
            return

        if atype in ("5pump", "5drop"):
            if await db.count_alerts() >= _max_alerts():
                try:
                    await query.edit_message_text(f"Alert limit reached ({_max_alerts()}). Remove one first.")
                except Exception:
                    pass
                return
            price = await get_current_price(symbol)
            if not price:
                price = engine.last_prices.get(symbol)
            if not price:
                try:
                    await query.edit_message_text("Could not fetch current price to calculate percentage.")
                except Exception:
                    pass
                return
            condition = "above" if atype == "5pump" else "below"
            target = price * 1.05 if condition == "above" else price * 0.95

            alert_id = await db.add_alert(symbol, target, condition, True)
            ws = context.bot_data["ws"]
            await ws.subscribe(symbol)
            coin_safe = _escape_md(coin)
            try:
                await query.edit_message_text(
                    f"*Alert #{alert_id} added:*\n\n*{coin_safe}* alerts every time it goes "
                    f"{condition} *{format_price(target)}* (now {format_price(price)}).",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        elif atype == "custom":
            context.user_data["awaiting_custom_price"] = coin
            coin_safe = _escape_md(coin)
            try:
                await query.edit_message_text(
                    f"Send the exact target price for *{coin_safe}* (e.g. `70000`).",
                    parse_mode="Markdown"
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Bot factory
# ---------------------------------------------------------------------------

def create_bot(alert_engine, binance_ws) -> Application:
    """Create and configure the Telegram bot application."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set — cannot create bot.")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.bot_data["engine"] = alert_engine
    app.bot_data["ws"] = binance_ws

    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("removeall", cmd_removeall))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("snooze", cmd_snooze))
    app.add_handler(CommandHandler("unsnooze", cmd_unsnooze))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("movers", cmd_movers))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("preset", cmd_preset))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("import", cmd_import))
    app.add_handler(CommandHandler("backup", cmd_backup))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    return app
