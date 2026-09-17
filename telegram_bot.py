"""Telegram bot for managing crypto price alerts."""

import functools
import logging
import datetime
import time

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup,
    InlineKeyboardButton, BotCommand, WebAppInfo, InputMediaPhoto
)
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

import database as db
import config
import charts
from config import TELEGRAM_BOT_TOKEN
from prices import (
    get_current_price, get_prices, get_ticker, get_tickers, get_top_movers,
    cached_price, cached_ticker, ticker_price, ticker_change_24h, format_price,
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


COIN_ICONS: dict[str, str] = {
    "BTC": "🟠",      # Bitcoin Orange
    "ETH": "🔷",      # Ethereum Blue Diamond
    "SOL": "🟣",      # Solana Purple
    "HYPE": "⚡",     # Hyperliquid Flash
    "DOGE": "🐶",     # Dogecoin Dog
    "XRP": "💧",      # Ripple Droplet
    "BNB": "🟡",      # Binance Gold
    "ADA": "🔹",      # Cardano Blue
    "AVAX": "🔺",     # Avalanche Red Triangle
    "LINK": "🔗",     # Chainlink Chain
    "SUI": "💧",      # Sui Droplet
    "PEPE": "🐸",     # Pepe Frog
    "SHIB": "🐕",     # Shiba Inu
    "TON": "💎",      # TON Crystal
    "NEAR": "🌐",     # NEAR Protocol
    "LTC": "🥈",      # Litecoin Silver
    "DOT": "⚪",      # Polkadot Dot
    "MATIC": "🟣",    # Polygon Purple
    "POL": "🟣",      # Polygon
    "TRX": "🔴",      # Tron Red
    "ARB": "🔵",      # Arbitrum Blue
    "OP": "🔴",       # Optimism Red
    "UNI": "🦄",      # Uniswap Unicorn
    "AAVE": "👻",     # Aave Ghost
    "ATOM": "⚛️",     # Cosmos Atom
    "XMR": "🔒",      # Monero Privacy
    "RENDER": "🎨",   # Render Network
    "FET": "🤖",      # AI Superintelligence
    "TAO": "🧠",      # Bittensor Brain
    "INJ": "🥷",      # Injective
    "KAS": "💠",      # Kaspa
    "APT": "🧬",      # Aptos
    "FTM": "👻",      # Fantom
    "ICP": "♾️",      # Internet Computer Infinity
    "WIF": "🧢",      # Dogwifhat Cap
    "BONK": "🐕",     # Bonk Dog
    "FLOKI": "⚔️",     # Floki Viking
}


def coin_icon(symbol: str) -> str:
    """Return dedicated, colorful visual icon for cryptocurrency symbols."""
    coin = (symbol or "").upper().replace("USDT", "").replace("USD", "").strip()
    return COIN_ICONS.get(coin, "💎")


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
        ticker = tickers.get(symbol)
        price = ticker_price(ticker)
        if price is None and engine:
            price = engine.get_fresh_price(symbol, max_age_sec=30)
        if price is None:
            price = cached_price(symbol, max_age_sec=120)
        if price is None:
            lines.append(f"  *{coin}*: unavailable")
            continue
        pct = ticker_change_24h(ticker or cached_ticker(symbol))
        extra = f" ({_fmt_pct(pct)} 24h)" if pct is not None else ""
        lines.append(f"  {coin_icon(symbol)} *{coin}*: {format_price(price)}{extra}")
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


async def _safe_reply_md(target_msg, text: str, reply_markup=None):
    """Safely reply with Markdown, falling back to plain text if Markdown parsing fails."""
    try:
        return await target_msg.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"Markdown reply failed ({e}); falling back to plain text")
        plain = text.replace("*", "").replace("`", "")
        return await target_msg.reply_text(plain, reply_markup=reply_markup)


async def _safe_edit_md(query, text: str, reply_markup=None):
    """Safely edit message text with Markdown, falling back to plain text if Markdown parsing fails."""
    try:
        if reply_markup is not None:
            return await query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        return await query.edit_message_text(text, parse_mode="Markdown")
    except Exception as e:
        err = str(e).lower()
        if "message is not modified" in err:
            return None
        logger.warning(f"Markdown edit failed ({e}); falling back to plain text")
        plain = text.replace("*", "").replace("`", "").replace("_", "")
        try:
            if reply_markup is not None:
                return await query.edit_message_text(plain, reply_markup=reply_markup)
            return await query.edit_message_text(plain)
        except Exception as e2:
            if "message is not modified" not in str(e2).lower():
                logger.error(f"Fallback plain text edit failed: {e2}")
            return None


def get_fast_price(symbol: str, engine=None) -> float | None:
    """Instant price lookup (<0.001ms) from live WebSocket engine or local cache. Never blocks."""
    norm = normalize_symbol(symbol)
    if engine and hasattr(engine, "get_fresh_price"):
        p = engine.get_fresh_price(norm, max_age_sec=60)
        if p is not None:
            return p
    return cached_price(norm, max_age_sec=120)


async def get_fast_or_live_price(symbol: str, engine=None, timeout: float = 1.5) -> float | None:
    """Instant price lookup first, with strict-timeout REST fallback if missing."""
    p = get_fast_price(symbol, engine)
    if p is not None:
        return p
    try:
        norm = normalize_symbol(symbol)
        return await asyncio.wait_for(get_current_price(norm), timeout=timeout)
    except Exception:
        return None


def _get_target_msg(update: Update):
    if update.message:
        return update.message
    if update.callback_query and update.callback_query.message:
        return update.callback_query.message
    return None


async def _reply_text(update: Update, text: str, parse_mode: str = None, reply_markup=None):
    """Safely send text reply to either a direct message or a callback query."""
    msg = _get_target_msg(update)
    if msg:
        if parse_mode == "Markdown":
            return await _safe_reply_md(msg, text, reply_markup=reply_markup)
        return await msg.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)


async def _reply_doc(update: Update, context: ContextTypes.DEFAULT_TYPE, document, caption: str = ""):
    """Safely send document reply to chat_id or target message."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id:
        return await context.bot.send_document(chat_id=chat_id, document=document, caption=caption)
    msg = _get_target_msg(update)
    if msg and hasattr(msg, "reply_document"):
        return await msg.reply_document(document=document, caption=caption)


async def _should_keep_subscribed(symbol: str) -> bool:
    """Check if a symbol should remain subscribed to the WebSocket feed."""
    try:
        if await db.is_watched(symbol):
            return True
        watchlist_cfg = getattr(config, "WATCHLIST_SYMBOLS", ()) or ()
        if symbol in watchlist_cfg:
            return True
        remaining = await db.get_alerts_for_symbol(symbol)
        return bool(remaining)
    except Exception:
        return False


async def _resolve_symbol(coin_or_symbol: str, engine=None) -> tuple | None:
    """Normalize + verify a symbol. Returns (symbol, price)."""
    symbol = normalize_symbol(coin_or_symbol)
    if not is_valid_symbol(symbol):
        return None
    price = await get_current_price(symbol)
    if price is None and engine is not None:
        price = engine.get_fresh_price(symbol, max_age_sec=30)
    return symbol, price


# Re-exported for backwards compatibility (main.py imports it from here).
__all__ = ["format_price", "get_current_price", "create_bot"]

# Alias kept so `from telegram_bot import get_current_price` still works
# (single source of truth lives in prices.py).


def get_main_keyboard(engine=None):
    """Create the clean 4-button persistent main menu keyboard."""
    keyboard = [
        [KeyboardButton("⚡ Dashboard"), KeyboardButton("➕ Set Alert")],
        [KeyboardButton("💰 Prices"), KeyboardButton("📋 My Alerts")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def _render_dashboard(engine) -> tuple[str, InlineKeyboardMarkup]:
    """Generate text and inline navigation buttons for the Master Command Center."""
    watch = await _effective_watchlist()
    alert_count = await db.count_alerts()
    symbols = list(watch[:4])

    status_str = "⏸️ *ALERTS PAUSED*" if (engine and engine.is_muted()) else "🟢 *ONLINE (Active)*"
    stamp = config.now_local().strftime("%H:%M:%S")

    lines = [
        "⚡ *CRYPTO COMMAND CENTER*",
        f"Status: {status_str}  •  `{stamp}`",
        f"Active Alerts: *{alert_count}*  •  Watchlist: *{len(watch)}*",
        "",
        "📊 *Market Snapshot*",
    ]

    for sym in symbols:
        coin = _escape_md(sym.replace("USDT", ""))
        price = get_fast_price(sym, engine)
        ticker = cached_ticker(sym)
        pct = ticker_change_24h(ticker)
        pct_str = f" ({_fmt_pct(pct)})" if pct is not None else ""
        lines.append(f"  • {coin_icon(sym)} *{coin}*: {format_price(price) if price else 'loading...'}{pct_str}")

    lines.append("")
    lines.append("👇 *Tap an action below to manage or navigate:*")

    pause_label = "▶️ Resume Alerts" if (engine and engine.is_muted()) else "⏸️ Pause Alerts"
    pause_cb = "pause_resume" if (engine and engine.is_muted()) else "hub_pause"

    kb = [
        [
            InlineKeyboardButton("➕ Set Alert", callback_data="wiz_start"),
            InlineKeyboardButton("📋 My Alerts", callback_data="hub_alerts"),
        ],
        [
            InlineKeyboardButton("📊 Top Movers", callback_data="hub_movers"),
            InlineKeyboardButton("📈 Charts & TA", callback_data="hub_charts"),
        ],
        [
            InlineKeyboardButton("👁️ Watchlist", callback_data="hub_watch"),
            InlineKeyboardButton("📐 Grid Wizard", callback_data="hub_grid"),
        ],
        [
            InlineKeyboardButton(pause_label, callback_data=pause_cb),
            InlineKeyboardButton("🛠️ Tools & Status", callback_data="hub_tools"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh Dashboard", callback_data="hub_refresh"),
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _show_coin_card(target, symbol: str, engine=None) -> None:
    """Render a dedicated zero-dead-end Coin Action Card."""
    symbol = normalize_symbol(symbol)
    coin = symbol.replace("USDT", "")
    coin_safe = _escape_md(coin)
    price = get_fast_price(symbol, engine)
    ticker = cached_ticker(symbol)
    if price is None:
        try:
            price = await asyncio.wait_for(get_current_price(symbol), timeout=1.5)
            ticker = cached_ticker(symbol)
        except Exception:
            pass

    pct = ticker_change_24h(ticker)
    pct_str = f" ({_fmt_pct(pct)} 24h)" if pct is not None else ""

    watched = await db.is_watched(symbol)
    watch_btn_label = "👁️ Unwatch" if watched else "👁️ +Watch"

    alerts = await db.get_alerts_for_symbol(symbol)
    alert_count = len(alerts)

    tv_embed_url = charts.get_tradingview_embed_url(symbol, interval="1h")

    lines = [
        f"{coin_icon(coin)} *{coin_safe} / USDT*",
        f"💰 Price: *{format_price(price) if price else 'N/A'}*{pct_str}",
        f"🔔 Active Alerts: *{alert_count}*",
    ]
    if ticker:
        high = ticker.get("highPrice") or ticker.get("h")
        low = ticker.get("lowPrice") or ticker.get("l")
        if high and low:
            try:
                lines.append(f"📈 24h Range: ${float(low):,.2f} — ${float(high):,.2f}")
            except Exception:
                pass

    lines.append("\n_Select an action below:_")

    kb = [
        [
            InlineKeyboardButton("📈 View Chart", callback_data=f"chart_{symbol}_1h_tv"),
            InlineKeyboardButton("➕ Quick Alert", callback_data=f"wiz_coin_{coin}"),
        ],
        [
            InlineKeyboardButton("🚨 Siren Alert", callback_data=f"wiz_type_{coin}_siren"),
            InlineKeyboardButton("📐 Grid Range", callback_data=f"wiz_grid_{coin}"),
        ],
        [
            InlineKeyboardButton(watch_btn_label, callback_data=f"watch_toggle_{coin}"),
            InlineKeyboardButton("🚀 Live TV Chart", web_app=WebAppInfo(url=tv_embed_url)),
        ],
        [
            InlineKeyboardButton("🔙 Back to Dashboard", callback_data="hub_main"),
        ],
    ]
    markup = InlineKeyboardMarkup(kb)
    if hasattr(target, "reply_text"):
        await _safe_reply_md(target, "\n".join(lines), reply_markup=markup)
    else:
        await _safe_edit_md(target, "\n".join(lines), reply_markup=markup)


async def _render_movers_deck() -> tuple[str, InlineKeyboardMarkup]:
    """Generate the interactive Top Movers deck with direct coin cards."""
    rows = await get_top_movers(8)
    if not rows:
        return "Could not fetch top movers right now.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Hub", callback_data="hub_main")]])

    lines = ["📊 *Top 24h Market Movers*\n_Tap any coin to open its action card & charts:_\n"]
    kb = []
    current_row = []
    for symbol, price, pct in rows:
        coin = symbol.replace("USDT", "")
        sign = "🟢 +" if pct >= 0 else "🔴 "
        pct_display = f"{float(pct)*100:+.1f}%"
        btn_label = f"{sign}{coin} {pct_display}"
        current_row.append(InlineKeyboardButton(btn_label, callback_data=f"coin_card_{coin}"))
        if len(current_row) == 2:
            kb.append(current_row)
            current_row = []
    if current_row:
        kb.append(current_row)

    kb.append([
        InlineKeyboardButton("🔄 Refresh Movers", callback_data="hub_movers"),
        InlineKeyboardButton("🔙 Back to Hub", callback_data="hub_main"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _render_watch_deck(engine=None) -> tuple[str, InlineKeyboardMarkup]:
    """Generate the interactive Watchlist deck."""
    watch = await _effective_watchlist()
    lines = ["👁️ *Watchlist Deck*\n_Tap any coin to view charts, set alerts, or manage:_\n"]
    kb = []
    current_row = []
    for sym in watch:
        coin = sym.replace("USDT", "")
        p = get_fast_price(sym, engine)
        ticker = cached_ticker(sym)
        pct = ticker_change_24h(ticker)
        pct_str = f" ({_fmt_pct(pct)})" if pct is not None else ""
        lines.append(f"  • {coin_icon(sym)} *{_escape_md(coin)}*: {format_price(p) if p else 'N/A'}{pct_str}")
        current_row.append(InlineKeyboardButton(f"{coin_icon(coin)} {coin}", callback_data=f"coin_card_{coin}"))
        if len(current_row) == 3:
            kb.append(current_row)
            current_row = []
    if current_row:
        kb.append(current_row)

    kb.append([
        InlineKeyboardButton("➕ Add Coin", callback_data="watch_add_wiz"),
        InlineKeyboardButton("🗑️ Remove Coin", callback_data="watch_del_wiz"),
    ])
    kb.append([
        InlineKeyboardButton("🔄 Refresh Watchlist", callback_data="hub_watch"),
        InlineKeyboardButton("🔙 Back to Hub", callback_data="hub_main"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _render_pause_deck(engine=None) -> tuple[str, InlineKeyboardMarkup]:
    """Generate the interactive Pause control deck."""
    is_paused = engine.is_muted() if engine else False
    status_text = "⏸️ *Alerts are currently PAUSED.*" if is_paused else "🟢 *Alerts are currently ACTIVE.*"
    lines = [
        "⏸️ *Pause / Resume Alert Control*",
        status_text,
        "",
        "Choose a duration to silence all notifications without deleting alerts:",
    ]
    kb = [
        [
            InlineKeyboardButton("⏸️ 15m", callback_data="pause_dur_15m"),
            InlineKeyboardButton("⏸️ 1h", callback_data="pause_dur_1h"),
            InlineKeyboardButton("⏸️ 4h", callback_data="pause_dur_4h"),
            InlineKeyboardButton("⏸️ 24h", callback_data="pause_dur_24h"),
        ],
        [
            InlineKeyboardButton("▶️ Resume Alerts Now", callback_data="pause_resume"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Hub", callback_data="hub_main"),
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _render_tools_deck(engine=None, ws=None) -> tuple[str, InlineKeyboardMarkup]:
    """Generate the Tools and System Health deck."""
    stats = engine.get_stats() if engine and hasattr(engine, "get_stats") else {}
    ws_connected = getattr(ws, "connected", False) if ws else False
    lines = [
        "🛠️ *Tools & Diagnostics Hub*",
        f"• WebSocket: {'🟢 Connected' if ws_connected else '🔴 Disconnected'}",
        f"• Price Checks: {stats.get('checks', 0)}  |  Triggered: {stats.get('triggered', 0)}",
        f"• Queued Notifications: {await db.pending_count()}",
        "",
        "Quick Actions:",
    ]
    kb = [
        [
            InlineKeyboardButton("📜 Fired History", callback_data="hub_history"),
            InlineKeyboardButton("🩺 Watchdog Health", callback_data="hub_health"),
        ],
        [
            InlineKeyboardButton("💾 Backup Database", callback_data="hub_backup"),
            InlineKeyboardButton("📤 Export JSON", callback_data="hub_export"),
        ],
        [
            InlineKeyboardButton("🔄 Update from GitHub", callback_data="hub_update"),
        ],
        [
            InlineKeyboardButton("🔙 Back to Hub", callback_data="hub_main"),
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _render_charts_hub() -> tuple[str, InlineKeyboardMarkup]:
    """Generate the Charts and TA hub."""
    lines = [
        "📈 *Charts & Technical Analysis Hub*",
        "Select a coin to generate a chart, or open the Live TradingView WebApp:",
    ]
    kb = [
        [
            InlineKeyboardButton(f"{coin_icon('BTC')} BTC Chart", callback_data="chart_BTCUSDT_1h_tv"),
            InlineKeyboardButton(f"{coin_icon('ETH')} ETH Chart", callback_data="chart_ETHUSDT_1h_tv"),
        ],
        [
            InlineKeyboardButton(f"{coin_icon('SOL')} SOL Chart", callback_data="chart_SOLUSDT_1h_tv"),
            InlineKeyboardButton(f"{coin_icon('HYPE')} HYPE Chart", callback_data="chart_HYPEUSDT_1h_tv"),
        ],
        [
            InlineKeyboardButton(f"{coin_icon('DOGE')} DOGE Chart", callback_data="chart_DOGEUSDT_1h_tv"),
            InlineKeyboardButton(f"{coin_icon('XRP')} XRP Chart", callback_data="chart_XRPUSDT_1h_tv"),
        ],
        [
            InlineKeyboardButton("🚀 Open Live TV Chart (BTC)", web_app=WebAppInfo(url=charts.get_tradingview_embed_url("BTCUSDT"))),
        ],
        [
            InlineKeyboardButton("🔙 Back to Hub", callback_data="hub_main"),
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _render_wiz_start() -> tuple[str, InlineKeyboardMarkup]:
    """Generate Alert Wizard: Step 1 Coin Selection."""
    lines = [
        "➕ *Step 1: Select a Coin for your Alert*",
        "Choose one of the popular coins below, or type any coin ticker (e.g. `DOGE`):",
    ]
    kb = [
        [InlineKeyboardButton(f"{coin_icon('BTC')} BTC", callback_data="wiz_coin_BTC"), InlineKeyboardButton(f"{coin_icon('ETH')} ETH", callback_data="wiz_coin_ETH")],
        [InlineKeyboardButton(f"{coin_icon('SOL')} SOL", callback_data="wiz_coin_SOL"), InlineKeyboardButton(f"{coin_icon('HYPE')} HYPE", callback_data="wiz_coin_HYPE")],
        [InlineKeyboardButton(f"{coin_icon('DOGE')} DOGE", callback_data="wiz_coin_DOGE"), InlineKeyboardButton(f"{coin_icon('XRP')} XRP", callback_data="wiz_coin_XRP")],
        [InlineKeyboardButton("🔙 Back to Hub", callback_data="hub_main")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _render_wiz_coin(coin: str, engine=None) -> tuple[str, InlineKeyboardMarkup]:
    """Generate Alert Wizard: Step 2 Alert Type / 1-Tap Percentages."""
    symbol = normalize_symbol(coin)
    coin_safe = _escape_md(coin.upper())
    price = get_fast_price(symbol, engine)
    if price is None:
        try:
            price = await asyncio.wait_for(get_current_price(symbol), timeout=1.5)
        except Exception:
            price = None

    lines = [
        f"🎯 *Set Alert for {coin_icon(coin)} {coin_safe}*",
        f"Current Price: *{format_price(price) if price else 'N/A'}*",
        "",
        "Choose an alert type or 1-tap percentage:",
    ]
    kb = [
        [
            InlineKeyboardButton("🎯 Custom Price", callback_data=f"wiz_type_{coin}_custom"),
            InlineKeyboardButton("🚨 Emergency Siren", callback_data=f"wiz_type_{coin}_siren"),
        ],
        [
            InlineKeyboardButton("📈 +2% Quick", callback_data=f"wiz_add_pct_{coin}_2"),
            InlineKeyboardButton("📈 +5% Quick", callback_data=f"wiz_add_pct_{coin}_5"),
        ],
        [
            InlineKeyboardButton("📉 -2% Quick", callback_data=f"wiz_add_pct_{coin}_-2"),
            InlineKeyboardButton("📉 -5% Quick", callback_data=f"wiz_add_pct_{coin}_-5"),
        ],
        [
            InlineKeyboardButton("🪢 Trailing Stop", callback_data=f"wiz_trail_{coin}"),
            InlineKeyboardButton("⚡ Volatility Move", callback_data=f"wiz_move_{coin}"),
        ],
        [
            InlineKeyboardButton("📐 Grid Range", callback_data=f"wiz_grid_{coin}"),
            InlineKeyboardButton("🔙 Back to Coins", callback_data="wiz_start"),
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _render_wiz_trail(coin: str, engine=None) -> tuple[str, InlineKeyboardMarkup]:
    """Generate Trailing Stop wizard presets."""
    lines = [
        f"🪢 *{coin_icon(coin)} {coin.upper()} Trailing Stop Alert*",
        "Alerts when price pulls back by X% from its highest peak:",
    ]
    kb = [
        [InlineKeyboardButton("Trail 2%", callback_data=f"wiz_add_trail_{coin}_2"), InlineKeyboardButton("Trail 3%", callback_data=f"wiz_add_trail_{coin}_3")],
        [InlineKeyboardButton("Trail 5%", callback_data=f"wiz_add_trail_{coin}_5"), InlineKeyboardButton("Trail 10%", callback_data=f"wiz_add_trail_{coin}_10")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"wiz_coin_{coin}")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _render_wiz_move(coin: str, engine=None) -> tuple[str, InlineKeyboardMarkup]:
    """Generate Volatility Move wizard presets."""
    lines = [
        f"⚡ *{coin_icon(coin)} {coin.upper()} Volatility Move Alert*",
        "Alerts on rapid price surges or drops within a time window:",
    ]
    kb = [
        [InlineKeyboardButton("3% in 15m", callback_data=f"wiz_add_move_{coin}_3_15"), InlineKeyboardButton("5% in 1h", callback_data=f"wiz_add_move_{coin}_5_60")],
        [InlineKeyboardButton("7% in 4h", callback_data=f"wiz_add_move_{coin}_7_240"), InlineKeyboardButton("10% in 24h", callback_data=f"wiz_add_move_{coin}_10_1440")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"wiz_coin_{coin}")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _render_wiz_grid(coin: str, engine=None) -> tuple[str, InlineKeyboardMarkup]:
    """Generate Grid Range wizard presets."""
    symbol = normalize_symbol(coin)
    price = get_fast_price(symbol, engine)
    if price is None:
        try:
            price = await asyncio.wait_for(get_current_price(symbol), timeout=1.5)
        except Exception:
            price = None

    lines = [
        f"📐 *{coin_icon(coin)} {coin.upper()} Price Grid Setup*",
        f"Current Price: *{format_price(price) if price else 'N/A'}*",
        "",
        "Instantly deploy laddered alerts above and below market:",
    ]
    kb = [
        [
            InlineKeyboardButton("±2% Range (5 levels)", callback_data=f"grid_preset_{coin}_2_5"),
            InlineKeyboardButton("±5% Range (5 levels)", callback_data=f"grid_preset_{coin}_5_5"),
        ],
        [
            InlineKeyboardButton("±10% Range (7 levels)", callback_data=f"grid_preset_{coin}_10_7"),
            InlineKeyboardButton("±20% Range (9 levels)", callback_data=f"grid_preset_{coin}_20_9"),
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data=f"wiz_coin_{coin}"),
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _render_alert_editor(alert_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """Generate the rich interactive Alert Editor deck."""
    alert = await db.get_alert(alert_id)
    if not alert:
        return None
    symbol = db.alert_field(alert, "symbol", "")
    target = db.alert_field(alert, "target", 0)
    condition = db.alert_field(alert, "condition", "above")
    is_persistent = bool(db.alert_field(alert, "is_persistent", 0))
    is_urgent = bool(db.alert_field(alert, "is_urgent", 0))
    atype = db.alert_field(alert, "alert_type", "price") or "price"
    snoozed = db.alert_field(alert, "snoozed_until")
    coin = _escape_md(symbol.replace("USDT", ""))

    target_str = format_price(target)
    if atype == "trail":
        pct = db.alert_field(alert, "pct", 0)
        target_str = f"Trailing Stop (-{pct:g}% from peak)"
    elif atype == "move":
        pct = db.alert_field(alert, "pct", 0)
        win = db.alert_field(alert, "window_min", 15)
        target_str = f"Move Alert (±{pct:g}% in {win}m)"
    elif atype == "funding":
        target_str = "Funding Rate alert"

    lines = [
        f"⚙️ *Manage Alert #{alert_id}*",
        f"• Coin: {coin_icon(symbol)} *{coin}*",
        f"• Target: *{condition.upper()}* `{target_str}`" if atype == "price" else f"• Type: *{target_str}*",
        f"• Mode: {'🔁 Repeat' if is_persistent else '🎯 Once'}",
        f"• Siren: {'🚨 ENABLED (Loud)' if is_urgent else '🔕 Standard'}",
    ]
    if snoozed:
        lines.append(f"• Snooze: *till {_fmt_ts(snoozed)}*")

    lines.append("\n_Tap an action to adjust this alert:_")

    siren_btn = InlineKeyboardButton(
        "🔕 Turn Off Siren" if is_urgent else "🚨 Turn On Siren",
        callback_data=f"toggle_urgent_{alert_id}"
    )
    repeat_btn = InlineKeyboardButton(
        "🎯 Make Once" if is_persistent else "🔁 Make Repeat",
        callback_data=f"toggle_repeat_{alert_id}"
    )
    flip_btn = InlineKeyboardButton(
        f"🔄 Flip to {'BELOW' if condition == 'above' else 'ABOVE'}",
        callback_data=f"edit_flip_{alert_id}"
    )

    kb = []
    if atype == "price":
        kb.append([
            InlineKeyboardButton("➕ +1%", callback_data=f"edittgt_{alert_id}_1"),
            InlineKeyboardButton("➕ +5%", callback_data=f"edittgt_{alert_id}_5"),
            InlineKeyboardButton("➖ -1%", callback_data=f"edittgt_{alert_id}_-1"),
            InlineKeyboardButton("➖ -5%", callback_data=f"edittgt_{alert_id}_-5"),
        ])
        kb.append([
            flip_btn,
            InlineKeyboardButton("✏️ Custom Price", callback_data=f"edit_custom_{alert_id}"),
        ])
    kb.extend([
        [
            siren_btn,
            repeat_btn,
        ],
        [
            InlineKeyboardButton("🔕 Snooze 1h", callback_data=f"snooze_{alert_id}_1h"),
            InlineKeyboardButton("4h", callback_data=f"snooze_{alert_id}_4h"),
            InlineKeyboardButton("24h", callback_data=f"snooze_{alert_id}_24h"),
        ],
        [
            InlineKeyboardButton("❌ Delete Alert", callback_data=f"remove_{alert_id}"),
            InlineKeyboardButton("📋 Back to List", callback_data="hub_alerts"),
        ],
    ])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


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
        kb = [
            [
                InlineKeyboardButton("➕ Set Alert", callback_data="wiz_start"),
                InlineKeyboardButton("🔙 Hub", callback_data="hub_main"),
            ]
        ]
        return hint + "\n\nTap Set Alert to create one.", InlineKeyboardMarkup(kb)

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
        is_urgent = bool(db.alert_field(alert, "is_urgent", 0))
        flags = []
        if is_urgent:
            flags.append("🚨 URGENT")
        if is_persistent or atype in ("pct", "trail", "move"):
            flags.append("repeat")
        if snoozed:
            flags.append(f"snoozed till {_fmt_ts(snoozed)}")
        if expires:
            flags.append(f"expires {_fmt_ts(expires)}")
        flag_txt = f" ({', '.join(flags)})" if flags else " (once)"
        lines.append(f"  #{alert_id}{flag_txt} {coin_icon(symbol)} *{coin}* {desc}")
        keyboard.append([
            InlineKeyboardButton(f"⚙️ Edit #{alert_id}", callback_data=f"edit_{alert_id}"),
            InlineKeyboardButton(f"❌ Remove #{alert_id}", callback_data=f"remove_{alert_id}"),
        ])

    lines.append("")
    symbols = sorted({a[1] for a in chunk})
    for symbol in symbols:
        price = get_fast_price(symbol, engine)
        if price is not None:
            coin = _escape_md(symbol.replace("USDT", ""))
            pct = ticker_change_24h(cached_ticker(symbol))
            extra = f" ({_fmt_pct(pct)})" if pct is not None else ""
            lines.append(f"  {coin_icon(symbol)} {coin}: {format_price(price)}{extra}")

    nav = []
    tag = f"|{filt}" if filt else ""
    if page > 0:
        nav.append(InlineKeyboardButton("< Prev", callback_data=f"list_{page - 1}{tag}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next >", callback_data=f"list_{page + 1}{tag}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([
        InlineKeyboardButton("➕ Set Alert", callback_data="wiz_start"),
        InlineKeyboardButton("Refresh List", callback_data=f"refresh_list_{page}{tag}"),
        InlineKeyboardButton("🔙 Hub", callback_data="hub_main"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def _create_one_alert(symbol, current_price, condition, arg, is_trail,
                            is_move, is_funding, is_persistent, expires_at,
                            cooldown_sec, window_min=None, single_coin_multi=False,
                            is_urgent: bool = False):
    """Create a single alert row. Returns (id, description)."""
    if is_trail:
        pct = float(arg.rstrip("%"))
        if not (0 < pct <= 50):
            raise ValueError
        aid = await db.add_alert(symbol, current_price, "below", True,
                                 alert_type="trail", pct=pct, base_price=current_price,
                                 peak_price=current_price, expires_at=expires_at,
                                 cooldown_sec=cooldown_sec, is_urgent=is_urgent)
        return aid, f"trail {pct:g}%"
    if is_move:
        pct = float(arg.rstrip("%"))
        if not (0 < pct <= 50) or not window_min or not (1 <= window_min <= 1440):
            raise ValueError
        if condition == "auto":
            condition = "above"
        aid = await db.add_alert(symbol, current_price, condition, True,
                                 alert_type="move", pct=pct, window_min=window_min,
                                 base_price=current_price, peak_price=current_price,
                                 expires_at=expires_at, cooldown_sec=cooldown_sec,
                                 is_urgent=is_urgent)
        return aid, f"{pct:g}% in {window_min}m"
    if is_funding:
        pct = float(arg.rstrip("%"))
        if not (0 < abs(pct) <= 5):
            raise ValueError
        if condition == "auto":
            condition = "above"
        aid = await db.add_alert(symbol, current_price, condition, True,
                                 alert_type="funding", funding_rate=pct / 100,
                                 expires_at=expires_at, cooldown_sec=cooldown_sec,
                                 is_urgent=is_urgent)
        return aid, f"funding {pct:g}%"
    if arg.endswith("%"):
        percent = float(arg.rstrip("%"))
        if not (0 < abs(percent) <= 1000):
            raise ValueError
        if condition == "auto":
            condition = "above" if percent > 0 else "below"
        percent = abs(percent)
        target = current_price * (1 + percent / 100) if condition == "above" else current_price * (1 - percent / 100)
        if current_price is not None:
            if (condition == "above" and current_price >= target) or (
                    condition == "below" and current_price <= target):
                raise RuntimeError("already-hit")
        aid = await db.add_alert(symbol, target, condition, is_persistent,
                                 alert_type="price", expires_at=expires_at,
                                 cooldown_sec=cooldown_sec, is_urgent=is_urgent)
        return aid, f"{condition} {format_price(target)}"
    target = float(arg.replace(",", ""))
    if not (0 < target <= MAX_PRICE_VALUE):
        raise ValueError
    if current_price is not None:
        if condition == "auto" or single_coin_multi:
            if target > current_price:
                condition = "above"
            elif target < current_price:
                condition = "below"
            else:
                raise RuntimeError("already-hit")
        elif (condition == "above" and current_price >= target) or (
                condition == "below" and current_price <= target):
            raise RuntimeError("already-hit")
    else:
        if condition == "auto":
            condition = "above"
    aid = await db.add_alert(symbol, target, condition, is_persistent,
                             alert_type="price", expires_at=expires_at,
                             cooldown_sec=cooldown_sec, is_urgent=is_urgent)
    return aid, f"{condition} {format_price(target)}"


@authorized
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add — price, %, ladder, trail, move, funding. See /help for examples."""
    usage = (
        "Usage:\n"
        "`/add BTC 72500 above` [urgent] [repeat] [7d] [cooldown=15m]\n"
        "`/add ETH 5% up repeat`\n"
        "`/add BTC 76000,75000,79000` (auto-detects above/below)\n"
        "`/add BTC,ETH 80000,4000 above` (ladder)\n"
        "`/add BTC trail 5% below`\n"
        "`/add BTC move 3% 60m`\n"
        "`/add BTC funding 0.01% above`\n"
        "_Add `urgent` or `siren` to sound full siren on phone._"
    )

    if not _check_rate_limit(update.effective_user.id):
        await update.message.reply_text("Slow down — try again in a second.")
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(usage, parse_mode="Markdown")
        return
    max_alerts = _max_alerts()
    raw_coins = args[0]
    kind = args[1].lower()
    is_trail = kind == "trail"
    is_move = kind == "move"
    is_funding = kind == "funding"

    if (is_trail or is_move or is_funding) and len(args) < 3:
        await update.message.reply_text(usage, parse_mode="Markdown")
        return

    window_min = None
    single_coin_multi = False

    if is_trail or is_move or is_funding:
        price_arg = kind
        raw_rest = [a.lower() for a in args[2:]]
        condition = "below" if is_trail else "above"
        rest = []
        for token in raw_rest:
            if token in ("above", "below", "up", "down"):
                condition = "above" if token in ("above", "up") else "below"
            else:
                rest.append(token)
        if is_move:
            for token in raw_rest:
                t = token.rstrip("mM")
                if t.isdigit():
                    window_min = int(t)
                    break
        coins = parse_symbols(raw_coins)
        targets_raw = []
        ladder = False
    else:
        # Collect price tokens (handles spaces after commas e.g. ["76000,", "75000,", "79000"])
        price_tokens = []
        raw_rest = []
        in_prices = True
        for token in args[1:]:
            clean = token.rstrip(",").replace(",", "").replace(".", "").replace("+", "").replace("-", "")
            if in_prices:
                if clean.isdigit() or token.rstrip(",").endswith("%") or token.endswith(","):
                    price_tokens.append(token)
                    if not token.endswith(",") and not (len(price_tokens) > 1 and price_tokens[-2].endswith(",")):
                        in_prices = False
                else:
                    in_prices = False
                    raw_rest.append(token.lower())
            else:
                raw_rest.append(token.lower())

        price_arg = "".join(price_tokens) if price_tokens else args[1]
        condition = "auto"
        rest = []
        for token in raw_rest:
            if token in ("above", "below", "up", "down") and condition == "auto":
                condition = "above" if token in ("above", "up") else "below"
            else:
                rest.append(token)

        coins = parse_symbols(raw_coins)
        targets_raw = [t.strip() for t in price_arg.split(",") if t.strip()]
        ladder = len(targets_raw) > 1
        if ladder and len(coins) == 1 and len(targets_raw) > 1:
            coins = coins * len(targets_raw)
            single_coin_multi = True
        elif ladder and len(coins) != len(targets_raw):
            await update.message.reply_text("Ladder needs same count: `/add BTC,ETH 80000,4000 above`.", parse_mode="Markdown")
            return

    if not coins or (not targets_raw and not is_trail and not is_move and not is_funding):
        await update.message.reply_text(usage, parse_mode="Markdown")
        return

    is_persistent = "repeat" in rest
    is_urgent = any(t in rest for t in ("urgent", "siren"))
    expires_at = None
    for token in rest:
        if token in ("repeat", "urgent", "siren") or token.startswith("cooldown="):
            continue
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

    if len(targets_raw) > 10 or len(coins) > 10:
        await update.message.reply_text("Max 10 alerts per /add.")
        return
    num_new = len(targets_raw) if ladder else len(coins)
    if await db.count_alerts() + num_new > max_alerts:
        await update.message.reply_text(f"Alert limit reached ({max_alerts}). Remove one first.")
        return

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
        if current_price is None and engine is not None:
            current_price = engine.get_fresh_price(symbol, max_age_sec=30)

        if is_trail or is_move or is_funding:
            arg = ""
            for token in args[2:]:
                if token.endswith("%"):
                    arg = token
                    break
            if not arg and len(args) > 2:
                arg = args[2]
        else:
            arg = targets_raw[i] if ladder else price_arg

        try:
            aid, desc = await _create_one_alert(
                symbol, current_price, condition, arg, is_trail,
                is_move, is_funding, is_persistent, expires_at,
                cooldown_sec, window_min, single_coin_multi=single_coin_multi,
                is_urgent=is_urgent)
            await ws.subscribe(symbol)
            created.append((aid, symbol, desc, current_price))
        except RuntimeError:
            if not single_coin_multi:
                await update.message.reply_text(
                    f"Already true for {_escape_md(symbol.replace('USDT', ''))} "
                    f"(now {format_price(current_price)}).", parse_mode="Markdown")
                return
        except ValueError:
            await update.message.reply_text(f"Invalid value `{_escape_md(arg)}`. See /help.", parse_mode="Markdown")
            return

    if not created:
        await update.message.reply_text("Target equals the current price — no alerts added.")
        return

    lines = []
    for aid, symbol, desc, now_price in created:
        coin = _escape_md(symbol.replace("USDT", ""))
        lines.append(f"#{aid} *{coin}* {desc} (now {format_price(now_price)})")
    tag = ""
    if is_urgent:
        tag += " [🚨 URGENT]"
    if is_persistent or is_trail or is_move:
        tag += " [repeat]"
    if expires_at:
        tag += f" [expires {_fmt_ts(expires_at)}]"
    await update.message.reply_text(f"Alert(s) created{tag}:\n" + "\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_urgent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/urgent [args] or /siren [args] — create emergency siren alert with priority:5 and siren sound."""
    if not context.args:
        kb = [
            [InlineKeyboardButton("🚨 BTC Siren", callback_data="addwiz_type_BTC_siren"),
             InlineKeyboardButton("🚨 ETH Siren", callback_data="addwiz_type_ETH_siren")],
            [InlineKeyboardButton("🚨 SOL Siren", callback_data="addwiz_type_SOL_siren"),
             InlineKeyboardButton("🚨 HYPE Siren", callback_data="addwiz_type_HYPE_siren")],
        ]
        await update.message.reply_text(
            "🚨 *Emergency Siren Alert*\n\n"
            "Plays a loud siren on mobile (pierces DND & silent mode via Ntfy priority 5) and displays critical alert banner.\n\n"
            "Select a coin below to configure, or type:\n"
            "`/urgent BTC 68000 below`\n"
            "`/urgent SOL 125 above repeat`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    args = list(context.args)
    if not any(t in [a.lower() for a in args] for t in ("urgent", "siren")):
        args.append("urgent")
    context.args = args
    await cmd_add(update, context)


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

    if not await _should_keep_subscribed(symbol):
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
        if not await _should_keep_subscribed(symbol):
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
        if price is None and engine:
            price = engine.get_fresh_price(symbol, max_age_sec=30)
        if price is None:
            price = cached_price(symbol, max_age_sec=120)
        coin = _escape_md(symbol.replace("USDT", ""))
        if price:
            extra = ""
            pct = ticker_change_24h(ticker or cached_ticker(symbol))
            if pct is not None:
                extra = f" ({_fmt_pct(pct)} 24h)"
            await update.message.reply_text(f"{coin_icon(symbol)} *{coin}*: {format_price(price)}{extra}", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"Could not fetch price for {coin} (unknown symbol or API issue).")
        return
    tickers = await get_tickers(symbols)
    lines = ["*Prices*\n"]
    for symbol in symbols:
        coin = _escape_md(symbol.replace("USDT", ""))
        ticker = tickers.get(symbol)
        price = ticker_price(ticker)
        if price is None and engine:
            price = engine.get_fresh_price(symbol, max_age_sec=30)
        if price is None:
            price = cached_price(symbol, max_age_sec=120)
        if price is None:
            lines.append(f"  {coin_icon(symbol)} *{coin}*: unavailable")
            continue
        pct = ticker_change_24h(tickers.get(symbol) or cached_ticker(symbol))
        extra = f" ({_fmt_pct(pct)} 24h)" if pct is not None else ""
        lines.append(f"  {coin_icon(symbol)} *{coin}*: {format_price(price)}{extra}")
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
            ticker = tickers.get(s)
            p = ticker_price(ticker)
            if p is None and engine:
                p = engine.get_fresh_price(s, max_age_sec=30)
            if p is None:
                p = cached_price(s, max_age_sec=120)
            extra = ""
            pct = ticker_change_24h(tickers.get(s) or cached_ticker(s))
            if pct is not None:
                extra = f" ({_fmt_pct(pct)})"
            lines.append(f"  {_escape_md(s.replace('USDT', ''))}: {format_price(p) if p else 'no data yet'}{extra}")
        if len(symbols) > 10:
            lines.append(f"  ...and {len(symbols) - 10} more")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@authorized
async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dashboard, /menu, /hub — open interactive command center."""
    engine = context.bot_data["engine"]
    text, markup = await _render_dashboard(engine)
    if update.message:
        await update.message.reply_text(
            text, parse_mode="Markdown", reply_markup=markup
        )
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode="Markdown", reply_markup=markup
            )
        except Exception:
            pass


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — welcome user, initialize 4-button persistent reply keyboard, and open dashboard."""
    engine = context.bot_data["engine"]
    if update.message:
        await update.message.reply_text(
            "⚡ *Welcome to Crypto Alerts Command Center!*",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(engine),
        )
    await cmd_dashboard(update, context)


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — show available commands."""
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
        "`/urgent BTC 68000 below` — loud siren alert (priority 5)\n"
        "`/add BTC 72500 above` [repeat] [urgent] [7d] [cooldown=15m]\n"
        "`/add ETH 5% up repeat`\n"
        "`/add BTC 76000,75000,79000` (multi)\n"
        "`/add BTC,ETH 80000,4000 above` (ladder)\n"
        "`/grid BTC 70000 80000 5` — automated price grid\n"
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
        "`/update` — pull from git & restart (owner only)\n"
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
    if len(args) == 1:
        try:
            aid = int(args[0])
            ed_res = await _render_alert_editor(aid)
            if ed_res:
                txt, markup = ed_res
                await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=markup)
                return
            else:
                await update.message.reply_text(f"Alert #{aid} not found.")
                return
        except ValueError:
            pass
    if len(args) < 2:
        await update.message.reply_text("Usage: `/edit <id>` or `/edit <id> <price> [above|below]`", parse_mode="Markdown")
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


def _build_chart_ui(symbol: str, interval: str, chart_type: str) -> tuple[str, InlineKeyboardMarkup]:
    """Build standardized 3-row interactive keyboard and caption for charts."""
    coin = symbol.replace("USDT", "")
    tv_embed_url = charts.get_tradingview_embed_url(symbol, interval=interval)
    tv_web_url = charts.get_tradingview_web_url(symbol)

    valid_intervals = ("15m", "1h", "4h", "1d")
    row1 = [
        InlineKeyboardButton(
            f"• {intv} •" if intv == interval else intv,
            callback_data=f"chart_{symbol}_{intv}_{chart_type}",
        )
        for intv in valid_intervals
    ]

    row2 = [
        InlineKeyboardButton(f"{'• 📸 TV •' if chart_type in ('tv', 'chartimg') else '📸 TV Snap'}", callback_data=f"chart_{symbol}_{interval}_tv"),
        InlineKeyboardButton(f"{'• 📊 TA •' if chart_type in ('ta', 'tech', 'mpl') else '📊 Tech TA'}", callback_data=f"chart_{symbol}_{interval}_ta"),
        InlineKeyboardButton(f"{'• 🕯️ •' if chart_type == 'candle' else '🕯️ Clean'}", callback_data=f"chart_{symbol}_{interval}_candle"),
        InlineKeyboardButton(f"{'• 📈 •' if chart_type == 'line' else '📈 Line'}", callback_data=f"chart_{symbol}_{interval}_line"),
    ]

    row3 = [
        InlineKeyboardButton("🚀 Open Live TV Chart", web_app=WebAppInfo(url=tv_embed_url)),
        InlineKeyboardButton("🌐 TV.com", url=tv_web_url),
    ]

    has_tv_key = bool(getattr(config, "CHART_IMG_API_KEY", ""))
    style_labels = {
        "tv": "📸 TradingView Pro" if has_tv_key else "📊 Tech TA (No TV API Key)",
        "chartimg": "📸 TradingView Pro" if has_tv_key else "📊 Tech TA (No TV API Key)",
        "ta": "📊 Technical Analysis (EMA+RSI)",
        "tech": "📊 Technical Analysis (EMA+RSI)",
        "mpl": "📊 Technical Analysis (EMA+RSI)",
        "candle": "🕯️ Clean Candlesticks",
        "line": "📈 Sleek Area Line",
    }
    style_label = style_labels.get(chart_type, "📸 TradingView Pro")
    caption = f"📊 *{_escape_md(coin)}* ({interval.upper()}) • {style_label}"
    return caption, InlineKeyboardMarkup([row1, row2, row3])


@authorized
async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chart <symbol> [interval] [style] — generate a dark-mode price chart."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/chart <symbol> [interval] [style]`\n\n"
            "Supported intervals: `15m`, `30m`, `1h` (default), `2h`, `4h`, `1d`, `1w`\n"
            "Supported styles: `tv` (TradingView snapshot), `ta` (EMA + RSI), `candle`, `line`\n\n"
            "Examples:\n"
            "• `/chart BTC 4h`\n"
            "• `/chart ETH 1h ta`\n"
            "• `/chart SOL line`",
            parse_mode="Markdown"
        )
        return

    raw_coin = args[0]
    symbol = normalize_symbol(raw_coin)
    if not is_valid_symbol(symbol):
        await update.message.reply_text(f"Invalid symbol `{_escape_md(raw_coin)}`.", parse_mode="Markdown")
        return

    interval = "1h"
    chart_type = getattr(config, "DEFAULT_CHART_ENGINE", "tv")
    valid_intervals = ("15m", "30m", "1h", "2h", "4h", "1d", "1w")
    valid_types = {
        "tv": "tv", "snap": "tv", "tradingview": "tv", "chartimg": "tv",
        "ta": "ta", "tech": "ta", "mpl": "ta", "technical": "ta",
        "candle": "candle", "candles": "candle", "candlestick": "candle", "clean": "candle",
        "line": "line", "area": "line"
    }
    for arg in [a.lower() for a in args[1:]]:
        if arg in valid_intervals:
            interval = arg
        elif arg in valid_types:
            chart_type = valid_types[arg]

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    except Exception:
        pass

    chart_png = await charts.generate_chart_image(symbol, interval=interval, chart_type=chart_type)
    if not chart_png:
        await update.message.reply_text(
            f"Failed to generate chart for `{_escape_md(symbol)}`. Please try again in a moment.",
            parse_mode="Markdown"
        )
        return

    caption, markup = _build_chart_ui(symbol, interval, chart_type)
    await update.message.reply_photo(
        photo=chart_png,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=markup,
    )



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
        await _reply_text(update, "No fired alerts yet.")
        return
    lines = ["*Recently fired*\n"]
    for aid, symbol, condition, target, price, detail, fired_at in rows:
        coin = _escape_md((symbol or "").replace("USDT", ""))
        extra = f" {_escape_md(detail)}" if detail else ""
        lines.append(
            f"  #{aid} *{coin}* {condition} {format_price(target)}"
            f" @ {format_price(price)}{extra} — {_fmt_ts(fired_at)}")
    await _reply_text(update, "\n".join(lines), parse_mode="Markdown")


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
    await _reply_text(update, "\n".join(lines), parse_mode="Markdown")


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
        made.append(f"#{aid} {symbol.replace('USDT', '')} {direction} {format_price(target)}")
    if not made:
        await update.message.reply_text("Could not fetch prices for the preset.")
        return
    await update.message.reply_text(f"Preset *{name}* created:\n" + "\n".join(made), parse_mode="Markdown")


@authorized
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export — send alerts + watchlist as a JSON backup file."""
    if update.effective_chat and update.effective_chat.type != "private":
        await _reply_text(update, "This command can only be used in a private chat with the bot.")
        return
    import io
    import json
    alerts = await db.get_all_alerts()
    cols = ["id", "symbol", "target", "condition", "created_at", "is_persistent",
            "last_triggered_at", "alert_type", "expires_at", "snoozed_until",
            "cooldown_sec", "pct", "window_min", "base_price", "peak_price", "funding_rate", "is_urgent"]
    data = {"alerts": [dict(zip(cols, list(a) + [None] * (len(cols) - len(a)))) for a in alerts]}
    try:
        data["watchlist"] = await db.get_watchlist()
    except Exception:
        data["watchlist"] = []
    buf = io.BytesIO(json.dumps(data, indent=2, default=str).encode("utf-8"))
    buf.name = "alerts-backup.json"
    await _reply_doc(update, context, document=buf, caption=f"Backup: {len(alerts)} alert(s).")


@authorized
async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/import — reply to a JSON backup file with /import to restore."""
    doc = None
    if update.message and update.message.reply_to_message and update.message.reply_to_message.document:
        doc = update.message.reply_to_message.document
    if not doc:
        await _reply_text(update, "Reply to your backup JSON file with `/import`.", parse_mode="Markdown")
        return
    try:
        f = await doc.get_file()
        raw = await f.download_as_bytearray()
    except Exception as e:
        await _reply_text(update, f"Could not download file: {e}")
        return
    import json
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except Exception:
        await _reply_text(update, "That file is not valid JSON.")
        return
    items = data.get("alerts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        await _reply_text(update, "Backup has no `alerts` list.")
        return
    ws = context.bot_data["ws"]
    engine = context.bot_data["engine"]
    made, skipped = 0, 0
    curr_alerts = await db.count_alerts()
    max_cap = _max_alerts()
    if curr_alerts >= max_cap:
        await _reply_text(update, f"Alert limit reached ({max_cap}). Delete some alerts before importing.")
        return
    for item in items[:100]:
        try:
            if curr_alerts + made >= max_cap:
                skipped += 1
                continue
            symbol = normalize_symbol(str(item.get("symbol", "")))
            target = float(item.get("target", 0))
            condition = str(item.get("condition", "above")).lower()
            if not is_valid_symbol(symbol) or condition not in ("above", "below"):
                skipped += 1
                continue
            price = await get_current_price(symbol)
            if price is None and engine:
                price = engine.get_fresh_price(symbol, max_age_sec=30)
            if price is None:
                price = target
            atype = str(item.get("alert_type", "price") or "price")
            if atype not in ("price", "pct", "trail", "move", "funding"):
                atype = "price"
            is_urgent = bool(item.get("is_urgent", 0))
            await db.add_alert(
                symbol, target, condition, bool(item.get("is_persistent", 0)),
                alert_type=atype, expires_at=item.get("expires_at"),
                cooldown_sec=item.get("cooldown_sec"), pct=item.get("pct"),
                window_min=item.get("window_min"),
                base_price=item.get("base_price") or price,
                peak_price=item.get("peak_price") or price,
                funding_rate=item.get("funding_rate"),
                is_urgent=is_urgent)
            await ws.subscribe(symbol)
            made += 1
        except Exception:
            skipped += 1
    for sym in (data.get("watchlist") or [])[:25]:
        try:
            if is_valid_symbol(sym):
                await db.add_watch(sym)
        except Exception:
            pass
    await _reply_text(update, f"Imported {made} alert(s){f', skipped {skipped}' if skipped else ''}.")


@authorized
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backup — send the raw SQLite database file."""
    if update.effective_chat and update.effective_chat.type != "private":
        await _reply_text(update, "This command can only be used in a private chat with the bot.")
        return
    import os as _os
    if not _os.path.exists(db.DB_PATH):
        await _reply_text(update, "No database file yet.")
        return
    try:
        await db.checkpoint_wal()
        with open(db.DB_PATH, "rb") as f:
            await _reply_doc(update, context, document=f, caption="Database backup.")
    except Exception as e:
        await _reply_text(update, f"Backup failed: {e}")


@authorized
async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/update — pull latest version from GitHub and restart service (owner only)."""
    import asyncio
    import os
    import subprocess
    import sys

    if not update.effective_user or update.effective_user.id != config.TELEGRAM_USER_ID:
        await _reply_text(update, "Unauthorized: only the bot owner can update.")
        return

    msg = await _reply_text(update, "🔄 Checking for updates from GitHub...")
    repo_dir = os.path.dirname(os.path.abspath(__file__))

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "pull", "origin", "main",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        out_str = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()

        if proc.returncode != 0:
            await msg.edit_text(
                f"❌ Git pull failed (exit code {proc.returncode}):\n```\n{_escape_md(out_str[:400])}\n```",
                parse_mode="Markdown"
            )
            return

        log_proc = await asyncio.create_subprocess_exec(
            "git", "log", "-1", "--pretty=format:%h - %s",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        log_out, _ = await log_proc.communicate()
        commit_info = log_out.decode(errors="replace").strip()

        if "Already up to date" in out_str:
            await msg.edit_text(
                f"✅ Bot is already up to date.\n\n*Current commit:*\n`{_escape_md(commit_info)}`",
                parse_mode="Markdown"
            )
            return

        await msg.edit_text(
            f"🚀 *Update pulled successfully!*\n\n"
            f"*New commit:*\n`{_escape_md(commit_info)}`\n\n"
            f"🔄 Restarting service now...",
            parse_mode="Markdown"
        )

        await asyncio.sleep(1.0)
        try:
            subprocess.Popen(["sudo", "systemctl", "restart", "crypto-alerts"])
        except Exception:
            subprocess.Popen([sys.executable] + sys.argv, cwd=repo_dir)
            sys.exit(0)

    except Exception as e:
        await msg.edit_text(f"❌ Update failed: `{_escape_md(str(e))}`", parse_mode="Markdown")


@authorized
async def cmd_grid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/grid <coin> <low> <high> <count> [repeat] [expiry] — create an automated price alert grid."""
    usage = (
        "Usage:\n"
        "`/grid <coin> <low> <high> <count> [repeat] [expiry]`\n\n"
        "Example:\n"
        "`/grid BTC 70000 80000 5`\n"
        "`/grid SOL 90 120 4 repeat 7d`"
    )
    if not _check_rate_limit(update.effective_user.id):
        await update.message.reply_text("Slow down — try again in a second.")
        return

    args = context.args or []
    if len(args) < 4:
        await update.message.reply_text(usage, parse_mode="Markdown")
        return

    raw_coin = args[0]
    symbol = normalize_symbol(raw_coin)
    if not is_valid_symbol(symbol):
        await update.message.reply_text(f"Invalid symbol `{_escape_md(raw_coin)}`.", parse_mode="Markdown")
        return

    try:
        low = float(args[1].replace(",", ""))
        high = float(args[2].replace(",", ""))
        count = int(args[3])
    except ValueError:
        await update.message.reply_text("Invalid numbers. Use: `/grid BTC 70000 80000 5`.", parse_mode="Markdown")
        return

    if low <= 0 or high <= 0 or low >= high or high > MAX_PRICE_VALUE:
        await update.message.reply_text(f"Error: `<low>` must be positive, less than `<high>`, and high must not exceed {format_price(MAX_PRICE_VALUE)}.")
        return

    if not (2 <= count <= 10):
        await update.message.reply_text("Count must be between 2 and 10.")
        return

    rest = [a.lower() for a in args[4:]]
    is_persistent = "repeat" in rest
    expires_at = None
    for token in rest:
        parsed = _parse_expiry(token)
        if parsed and parsed != "invalid":
            expires_at = parsed

    max_alerts = _max_alerts()
    if await db.count_alerts() + count > max_alerts:
        await update.message.reply_text(f"Grid needs {count} alerts, but limit is {max_alerts}. Remove some first.")
        return

    engine = context.bot_data["engine"]
    resolved = await _resolve_symbol(symbol, engine)
    if resolved is None:
        await update.message.reply_text(f"Invalid coin `{_escape_md(symbol)}`.", parse_mode="Markdown")
        return
    symbol, current_price = resolved
    if current_price is None and engine is not None:
        current_price = engine.get_fresh_price(symbol, max_age_sec=30)

    step = (high - low) / (count - 1)
    grid_prices = [low + i * step for i in range(count)]

    ws = context.bot_data["ws"]
    created = []
    for price_target in grid_prices:
        if current_price is not None:
            condition = "above" if price_target > current_price else "below"
            if price_target == current_price:
                continue
        else:
            condition = "above"

        aid = await db.add_alert(symbol, price_target, condition, is_persistent,
                                 alert_type="price", expires_at=expires_at)
        created.append((aid, price_target, condition))

    await ws.subscribe(symbol)
    coin = _escape_md(symbol.replace("USDT", ""))
    now_str = f" (now {format_price(current_price)})" if current_price else ""
    tag = " [repeat]" if is_persistent else ""
    if expires_at:
        tag += f" [expires {_fmt_ts(expires_at)}]"

    lines = [f"🌐 *Grid created for {coin}*{now_str}{tag}:"]
    for aid, target, condition in created:
        lines.append(f"  • #{aid} {condition} *{format_price(target)}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


_MENU_LABELS = (
    "Dashboard", "⚡ Dashboard", "My Alerts", "📋 My Alerts", "List Alerts",
    "Prices", "💰 Prices", "Check Price", "Add Alert", "➕ Set Alert", "Set Alert",
    "Remove Alert", "Pause", "Resume", "Pause Alerts", "Resume Alerts",
    "Help", "Movers", "📊 Movers", "History", "📜 History", "Watchlist", "👁️ Watchlist",
    "Tools", "🛠️ Tools"
)


@authorized
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel — cancel any active interactive prompts or wizards."""
    active = False
    for key in ("editing_alert_target", "awaiting_watch_coin", "awaiting_custom_price", "is_urgent"):
        if context.user_data.pop(key, None) is not None:
            active = True
    msg = "Action cancelled." if active else "No active action to cancel."
    await _reply_text(update, msg)


# ---------------------------------------------------------------------------
# Interactive UI Handlers
# ---------------------------------------------------------------------------

@authorized
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text from interactive buttons or wizard states."""
    engine = context.bot_data["engine"]
    text = (update.message.text or "").strip()

    # State 1: Editing target price from Alert Editor deck
    editing_id = context.user_data.get("editing_alert_target")
    if editing_id:
        if any(label in text for label in _MENU_LABELS) or text.startswith("/"):
            context.user_data.pop("editing_alert_target", None)
        else:
            context.user_data.pop("editing_alert_target", None)
            try:
                val = float(text.replace(",", "").strip())
                if not (0 < val <= MAX_PRICE_VALUE):
                    await update.message.reply_text("Price is out of range.")
                    return
                alert = await db.get_alert(editing_id)
                if not alert:
                    await update.message.reply_text(f"Alert #{editing_id} not found.")
                    return
                atype = db.alert_field(alert, "alert_type", "price") or "price"
                if atype != "price":
                    await update.message.reply_text("Target editing is only supported for price alerts.")
                    return
                condition = db.alert_field(alert, "condition", "above")
                await db.set_target(editing_id, val, condition)
                ed_res = await _render_alert_editor(editing_id)
                if ed_res:
                    txt, kb = ed_res
                    await update.message.reply_text(
                        f"✅ Alert #{editing_id} target updated to *{format_price(val)}*.\n\n" + txt,
                        parse_mode="Markdown",
                        reply_markup=kb,
                    )
                else:
                    await update.message.reply_text(f"Alert #{editing_id} target updated to {format_price(val)}.")
                return
            except ValueError:
                await update.message.reply_text("Invalid price format. Edit cancelled.")
                return

    # State 2: Adding a custom coin to Watchlist
    if context.user_data.get("awaiting_watch_coin"):
        if any(label in text for label in _MENU_LABELS) or text.startswith("/"):
            context.user_data.pop("awaiting_watch_coin", None)
        else:
            context.user_data.pop("awaiting_watch_coin", None)
            sym = normalize_symbol(text)
            if is_valid_symbol(sym):
                p = await get_current_price(sym)
                if p is None and engine:
                    p = engine.get_fresh_price(sym, max_age_sec=30)
                if p is not None:
                    await db.add_watch(sym)
                    ws = context.bot_data.get("ws")
                    if ws:
                        await ws.subscribe(sym)
                    coin_clean = sym.replace("USDT", "")
                    await update.message.reply_text(f"✅ Added *{coin_clean}* to watchlist!", parse_mode="Markdown")
                    txt, kb = await _render_watch_deck(engine)
                    await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=kb)
                    return
            await update.message.reply_text(f"Could not find valid coin `{_escape_md(text)}`.", parse_mode="Markdown")
            return

    # State 3: Custom Price Wizard
    awaiting_coin = context.user_data.get("awaiting_custom_price")
    if awaiting_coin:
        if any(label in text for label in _MENU_LABELS) or text.startswith("/"):
            context.user_data.pop("awaiting_custom_price", None)  # Cancel wizard
            context.user_data.pop("is_urgent", None)
        else:
            # Support single or multiple comma- or space-separated prices
            raw_parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
            if len(raw_parts) == 1 and " " in raw_parts[0]:
                raw_parts = raw_parts[0].split()

            targets = []
            for p in raw_parts:
                try:
                    val = float(p.replace(",", "").strip())
                    if not (0 < val <= MAX_PRICE_VALUE):
                        await update.message.reply_text(f"Price {p} is out of range. Enter prices like `76000, 75000`.")
                        return
                    targets.append(val)
                except ValueError:
                    await update.message.reply_text(f"'{p}' is not a valid number. Enter prices like `76000, 75000, 79000`.")
                    return

            if not targets:
                await update.message.reply_text("Please enter a valid price, or tap a menu button to cancel.")
                return

            symbol = normalize_symbol(awaiting_coin)
            resolved = await _resolve_symbol(symbol, engine)
            if resolved is None:
                context.user_data.pop("awaiting_custom_price", None)
                context.user_data.pop("is_urgent", None)
                await update.message.reply_text(f"Invalid coin `{_escape_md(awaiting_coin)}`. Wizard cancelled.", parse_mode="Markdown")
                return
            symbol, price = resolved
            if price is None and engine is not None:
                price = engine.get_fresh_price(symbol, max_age_sec=30)

            if await db.count_alerts() + len(targets) > _max_alerts():
                context.user_data.pop("awaiting_custom_price", None)
                context.user_data.pop("is_urgent", None)
                await update.message.reply_text(f"Alert limit reached ({_max_alerts()}). Remove one first with /list.")
                return

            ws = context.bot_data["ws"]
            added = []
            is_urgent = bool(context.user_data.pop("is_urgent", False))
            for target in targets:
                if price is not None:
                    condition = "above" if target > price else "below"
                    if target == price:
                        continue
                else:
                    condition = "above" if target > 1000 else "below"

                alert_id = await db.add_alert(symbol, target, condition, False, is_urgent=is_urgent)
                added.append((alert_id, target, condition))

            await ws.subscribe(symbol)
            context.user_data.pop("awaiting_custom_price", None)
            coin = _escape_md(awaiting_coin)

            if not added:
                await update.message.reply_text("Target equals the current price — no alert added.")
                return

            urgent_badge = " [🚨 URGENT]" if is_urgent else ""
            if len(added) == 1:
                aid, target, condition = added[0]
                prefix = "🚨 *Emergency Siren Alert*" if is_urgent else "✅ Alert"
                if price is not None:
                    await _safe_reply_md(
                        update.message,
                        f"{prefix} #{aid} added: *{coin}* {condition} *{format_price(target)}* "
                        f"(now {format_price(price)}){urgent_badge}.",
                    )
                else:
                    await _safe_reply_md(
                        update.message,
                        f"{prefix} #{aid} added: *{coin}* {condition} *{format_price(target)}*{urgent_badge}.",
                    )
            else:
                now_str = f" (now {format_price(price)})" if price is not None else ""
                header = (
                    f"🚨 *Added {len(added)} Emergency Siren alerts for {coin}*{now_str}:"
                    if is_urgent
                    else f"✅ Added *{len(added)}* alerts for *{coin}*{now_str}:"
                )
                lines = [header]
                for aid, target, condition in added:
                    lines.append(f"  • #{aid} {condition} *{format_price(target)}*{urgent_badge}")
                await _safe_reply_md(update.message, "\n".join(lines))
            return

    # Normal Main Menu
    if text in ("⚡ Dashboard", "Dashboard", "Home", "Menu", "⚡"):
        text_dash, kb_dash = await _render_dashboard(engine)
        await update.message.reply_text(text_dash, parse_mode="Markdown", reply_markup=kb_dash)
        return
    elif text in ("➕ Set Alert", "Set Alert", "➕ Add Alert", "Add Alert"):
        txt_wiz, kb_wiz = await _render_wiz_start()
        await update.message.reply_text(txt_wiz, parse_mode="Markdown", reply_markup=kb_wiz)
        return
    elif text in ("📋 My Alerts", "📋 List Alerts", "My Alerts", "List Alerts"):
        await cmd_list(update, context)
        return
    elif text in ("💰 Prices", "💰 Check Price", "Check Price", "Prices: All", "Prices"):
        await _send_all_prices(update.message, engine)
        return
    elif text in ("📊 Movers", "Movers", "Top Movers"):
        txt_m, kb_m = await _render_movers_deck()
        await update.message.reply_text(txt_m, parse_mode="Markdown", reply_markup=kb_m)
        return
    elif text in ("⏸️ Pause", "⏸️ Pause Alerts", "Pause Alerts", "Pause"):
        txt_p, kb_p = await _render_pause_deck(engine)
        await update.message.reply_text(txt_p, parse_mode="Markdown", reply_markup=kb_p)
        return
    elif text in ("▶️ Resume", "▶️ Resume Alerts", "Resume Alerts", "Resume"):
        engine.resume_alerts()
        await update.message.reply_text("Alerts resumed.", reply_markup=get_main_keyboard(engine))
        return
    elif text in ("📜 History", "History"):
        await cmd_history(update, context)
        return
    elif text in ("Watchlist", "👁️ Watchlist"):
        txt_w, kb_w = await _render_watch_deck(engine)
        await update.message.reply_text(txt_w, parse_mode="Markdown", reply_markup=kb_w)
        return
    elif text in ("❓ Help", "Help"):
        await cmd_help(update, context)
        return
    elif text in ("❌ Remove Alert", "Remove Alert"):
        await update.message.reply_text("Tap *My Alerts* to see inline delete buttons for each alert.", parse_mode="Markdown")
        return

    # Check for bare coin symbol (e.g. "SOL", "BTC", "ETH", "DOGE") -> open Action Card!
    clean_sym = text.strip().lstrip("$").upper().replace("/", "").replace("-", "")
    if clean_sym and " " not in clean_sym and len(clean_sym) <= 12 and not text.startswith("/"):
        normalized = normalize_symbol(clean_sym)
        if is_valid_symbol(normalized):
            p = await get_current_price(normalized)
            if p is None and engine:
                p = engine.get_fresh_price(normalized, max_age_sec=30)
            if p is None:
                p = cached_price(normalized, max_age_sec=120)
            if p is not None:
                await _show_coin_card(update.message, normalized, engine)
                return

    if text and not text.startswith("/"):
        await update.message.reply_text(
            "Tap a menu button below, or type any coin name (e.g. `SOL`, `BTC`, `DOGE`) to open its action card.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard(engine)
        )


@authorized
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline button clicks."""
    query = update.callback_query
    if not query:
        return

    # Track whether query.answer() has been called to prevent double-answering.
    # Use a list so _dispatch_callback can mutate it (avoids monkey-patching
    # the frozen CallbackQuery object which newer python-telegram-bot forbids).
    answered = [False]

    async def _safe_answer(*args, **kwargs):
        """Call query.answer() at most once, swallowing errors."""
        if answered[0]:
            return
        answered[0] = True
        try:
            await query.answer(*args, **kwargs)
        except Exception:
            pass

    try:
        await _dispatch_callback(query, update, context, _safe_answer)
    except Exception as e:
        logger.error(f"Callback dispatch error for data={query.data!r}: {e}", exc_info=True)
        try:
            await _safe_edit_md(
                query,
                f"⚠️ *An error occurred:* `{_escape_md(str(e)[:150])}`\n\nTap below to return:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Return to Dashboard", callback_data="hub_main")]])
            )
        except Exception:
            pass
    finally:
        if not answered[0]:
            try:
                await query.answer()
            except Exception:
                pass


async def _dispatch_callback(query, update: Update, context: ContextTypes.DEFAULT_TYPE,
                              answer=None) -> None:
    data = query.data or ""
    engine = context.bot_data["engine"]

    # Fallback: if no safe answer callback provided, use query.answer directly
    if answer is None:
        answer = query.answer

    if data == "hub_main":
        await answer()
        txt, kb = await _render_dashboard(engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "hub_refresh":
        await answer("⚡ Dashboard updated!")
        txt, kb = await _render_dashboard(engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "hub_alerts":
        await answer()
        txt, kb = await get_list_text_and_markup(engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "hub_movers":
        await answer()
        txt, kb = await _render_movers_deck()
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "hub_watch":
        await answer()
        txt, kb = await _render_watch_deck(engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "hub_pause":
        await answer()
        txt, kb = await _render_pause_deck(engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "hub_tools":
        await answer()
        ws = context.bot_data.get("ws")
        txt, kb = await _render_tools_deck(engine, ws)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "hub_charts":
        await answer()
        txt, kb = await _render_charts_hub()
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "hub_grid":
        await answer()
        txt, kb = await _render_wiz_start()
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "hub_history":
        await answer()
        await cmd_history(update, context)
        return

    if data == "hub_health":
        await answer()
        await cmd_health(update, context)
        return

    if data == "hub_backup":
        await answer()
        await cmd_backup(update, context)
        return

    if data == "hub_export":
        await answer()
        await cmd_export(update, context)
        return

    if data == "hub_update":
        await answer()
        await cmd_update(update, context)
        return

    if data.startswith("coin_card_"):
        await answer()
        coin = data[len("coin_card_"):]
        await _show_coin_card(query, coin, engine)
        return

    if data.startswith("watch_toggle_"):
        coin = data[len("watch_toggle_"):]
        sym = normalize_symbol(coin)
        is_now = await db.toggle_watch(sym)
        ws = context.bot_data.get("ws")
        if ws and is_now:
            await ws.subscribe(sym)
        await answer(f"{'Added to' if is_now else 'Removed from'} watchlist!")
        await _show_coin_card(query, coin, engine)
        return

    if data == "watch_add_wiz":
        await answer()
        context.user_data["awaiting_watch_coin"] = True
        kb = [
            [InlineKeyboardButton(f"{coin_icon('DOGE')} +DOGE", callback_data="watch_add_quick_DOGE"), InlineKeyboardButton(f"{coin_icon('XRP')} +XRP", callback_data="watch_add_quick_XRP")],
            [InlineKeyboardButton(f"{coin_icon('PEPE')} +PEPE", callback_data="watch_add_quick_PEPE"), InlineKeyboardButton(f"{coin_icon('ADA')} +ADA", callback_data="watch_add_quick_ADA")],
            [InlineKeyboardButton(f"{coin_icon('AVAX')} +AVAX", callback_data="watch_add_quick_AVAX"), InlineKeyboardButton(f"{coin_icon('LINK')} +LINK", callback_data="watch_add_quick_LINK")],
            [InlineKeyboardButton("🔙 Back to Watchlist", callback_data="hub_watch")],
        ]
        await _safe_edit_md(
            query,
            "➕ *Add to Watchlist*\n\nTap a popular coin below or type any ticker in chat (e.g. `NEAR`):",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("watch_add_quick_"):
        coin = data[len("watch_add_quick_"):]
        sym = normalize_symbol(coin)
        await db.add_watch(sym)
        ws = context.bot_data.get("ws")
        if ws:
            await ws.subscribe(sym)
        await answer(f"Added {coin} to watchlist!")
        txt, kb = await _render_watch_deck(engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "watch_del_wiz":
        await answer()
        watch = await _effective_watchlist()
        kb = [[InlineKeyboardButton(f"❌ Remove {coin_icon(s)} {s.replace('USDT', '')}", callback_data=f"watch_remove_{s.replace('USDT', '')}")] for s in watch]
        kb.append([InlineKeyboardButton("🔙 Back to Watchlist", callback_data="hub_watch")])
        await _safe_edit_md(
            query,
            "🗑️ *Select a coin to remove from watchlist:*",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("watch_remove_"):
        coin = data[len("watch_remove_"):]
        sym = normalize_symbol(coin)
        await db.remove_watch(sym)
        await answer(f"Removed {coin} from watchlist.")
        txt, kb = await _render_watch_deck(engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data.startswith("pause_dur_"):
        dur = data[len("pause_dur_"):]
        secs = _parse_duration(dur) or 3600
        engine.pause_alerts(secs / 3600)
        await answer(f"Alerts paused for {dur}!")
        txt, kb = await _render_pause_deck(engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "pause_resume":
        engine.resume_alerts()
        await answer("Alerts resumed! 🔔")
        txt, kb = await _render_dashboard(engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "wiz_start":
        await answer()
        txt, kb = await _render_wiz_start()
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data.startswith("wiz_coin_"):
        await answer()
        coin = data[len("wiz_coin_"):]
        txt, kb = await _render_wiz_coin(coin, engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data.startswith("wiz_trail_"):
        await answer()
        coin = data[len("wiz_trail_"):]
        txt, kb = await _render_wiz_trail(coin, engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data.startswith("wiz_move_"):
        await answer()
        coin = data[len("wiz_move_"):]
        txt, kb = await _render_wiz_move(coin, engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data.startswith("wiz_grid_"):
        await answer()
        coin = data[len("wiz_grid_"):]
        txt, kb = await _render_wiz_grid(coin, engine)
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data.startswith("wiz_add_pct_"):
        if await db.count_alerts() >= _max_alerts():
            await answer(f"Alert limit reached ({_max_alerts()}).", show_alert=True)
            return
        parts = data.split("_")
        coin = parts[3].upper()
        pct = float(parts[4])
        sym = normalize_symbol(coin)
        price = await get_fast_or_live_price(sym, engine)
        if not price:
            await answer("Could not get current price.", show_alert=True)
            return
        cond = "above" if pct > 0 else "below"
        tgt = price * (1 + pct / 100)
        aid = await db.add_alert(sym, tgt, cond, True, alert_type="price")
        ws = context.bot_data.get("ws")
        if ws:
            await ws.subscribe(sym)
        await answer(f"✅ Alert #{aid} created!")
        kb = [
            [InlineKeyboardButton("➕ Set Another Alert", callback_data="wiz_start"),
             InlineKeyboardButton("📋 View Alerts", callback_data="hub_alerts")],
            [InlineKeyboardButton("⚡ Dashboard", callback_data="hub_main")],
        ]
        await _safe_edit_md(
            query,
            f"✅ *Alert #{aid} created for {_escape_md(coin)}!*\n\n"
            f"• Target: *{cond.upper()} {format_price(tgt)}* ({pct:+.0f}%)\n"
            f"• Mode: Repeat\n"
            f"• Current Price: {format_price(price)}",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("wiz_add_trail_"):
        if await db.count_alerts() >= _max_alerts():
            await answer(f"Alert limit reached ({_max_alerts()}).", show_alert=True)
            return
        parts = data.split("_")
        coin = parts[3].upper()
        pct = float(parts[4])
        sym = normalize_symbol(coin)
        price = await get_fast_or_live_price(sym, engine)
        if not price:
            await answer("Could not get current price.", show_alert=True)
            return
        aid = await db.add_alert(sym, price, "below", True, alert_type="trail", pct=pct, base_price=price, peak_price=price)
        ws = context.bot_data.get("ws")
        if ws:
            await ws.subscribe(sym)
        await answer(f"✅ Trailing Stop #{aid} created!")
        kb = [
            [InlineKeyboardButton("📋 View Alerts", callback_data="hub_alerts"),
             InlineKeyboardButton("⚡ Dashboard", callback_data="hub_main")],
        ]
        await _safe_edit_md(
            query,
            f"✅ *Trailing Stop #{aid} created for {_escape_md(coin)}!*\n\n"
            f"• Pullback: *{pct}% from peak*\n"
            f"• Current Price: {format_price(price)}",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("wiz_add_move_"):
        if await db.count_alerts() >= _max_alerts():
            await answer(f"Alert limit reached ({_max_alerts()}).", show_alert=True)
            return
        parts = data.split("_")
        coin = parts[3].upper()
        pct = float(parts[4])
        win = int(parts[5])
        sym = normalize_symbol(coin)
        price = await get_fast_or_live_price(sym, engine)
        if not price:
            await answer("Could not get current price.", show_alert=True)
            return
        aid = await db.add_alert(sym, price, "above", True, alert_type="move", pct=pct, window_min=win, base_price=price, peak_price=price)
        ws = context.bot_data.get("ws")
        if ws:
            await ws.subscribe(sym)
        await answer(f"✅ Move Alert #{aid} created!")
        kb = [
            [InlineKeyboardButton("📋 View Alerts", callback_data="hub_alerts"),
             InlineKeyboardButton("⚡ Dashboard", callback_data="hub_main")],
        ]
        await _safe_edit_md(
            query,
            f"✅ *Move Alert #{aid} created for {_escape_md(coin)}!*\n\n"
            f"• Trigger: *{pct}% move within {win}m*\n"
            f"• Current Price: {format_price(price)}",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("grid_preset_"):
        parts = data.split("_")
        coin = parts[2].upper()
        spread = float(parts[3])
        levels = int(parts[4])
        if await db.count_alerts() + levels > _max_alerts():
            await answer(f"Not enough room (limit {_max_alerts()}).", show_alert=True)
            return
        sym = normalize_symbol(coin)
        price = await get_fast_or_live_price(sym, engine)
        if not price:
            await answer("Could not get current price.", show_alert=True)
            return
        low = price * (1 - spread / 100)
        high = price * (1 + spread / 100)
        step = (high - low) / (levels - 1)
        grid_prices = [low + i * step for i in range(levels)]
        ws = context.bot_data.get("ws")
        added_ids = []
        for p_tgt in grid_prices:
            cond = "above" if p_tgt > price else "below"
            if abs(p_tgt - price) / price < 0.0001:
                continue
            aid = await db.add_alert(sym, p_tgt, cond, True, alert_type="price")
            added_ids.append(aid)
        if ws:
            await ws.subscribe(sym)
        await answer(f"✅ Grid deployed with {len(added_ids)} alerts!")
        kb = [
            [InlineKeyboardButton("📋 View Alerts", callback_data="hub_alerts"),
             InlineKeyboardButton("⚡ Dashboard", callback_data="hub_main")],
        ]
        await _safe_edit_md(
            query,
            f"🌐 *Grid deployed for {_escape_md(coin)}!*\n\n"
            f"• Deployed {len(added_ids)} alerts between *{format_price(low)}* and *{format_price(high)}*\n"
            f"• Spread: ±{spread:g}%\n"
            f"• Current Price: {format_price(price)}",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data == "prices_all":
        await _send_all_prices(query, engine, context=context)
        return

    if data.startswith("quick_add_"):
        if await db.count_alerts() >= _max_alerts():
            await answer(f"Alert limit reached ({_max_alerts()}).", show_alert=True)
            return
        # quick_add_<symbol>_<condition>_<target>
        parts = data.split("_")
        if len(parts) >= 5:
            sym, cond, tgt_str = parts[2], parts[3], parts[4]
            try:
                tgt = float(tgt_str)
                aid = await db.add_alert(sym, tgt, cond, False, alert_type="price")
                ws = context.bot_data["ws"]
                await ws.subscribe(sym)
                coin = _escape_md(sym.replace("USDT", ""))
                await answer(f"✅ Added alert #{aid}: {coin} {cond} {format_price(tgt)}")
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
            except Exception as e:
                await answer(f"Error: {e}")
        return

    if data.startswith("quick_snooze_"):
        # quick_snooze_<alert_id>_<duration>
        parts = data.split("_")
        if len(parts) >= 4:
            aid_str, dur_str = parts[2], parts[3]
            try:
                aid = int(aid_str)
                dur = _parse_duration(dur_str) or 7200
                until = db.iso_in(seconds=dur)
                await db.set_snooze(aid, until)
                await answer(f"🔕 Alert #{aid} snoozed for {dur_str}")
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
            except Exception as e:
                await answer(f"Error: {e}")
        return

    if data.startswith("quick_del_"):
        # quick_del_<alert_id>
        parts = data.split("_")
        if len(parts) >= 3:
            try:
                aid = int(parts[2])
                alert = await db.get_alert(aid)
                if alert:
                    sym = alert[1]
                    await db.remove_alert(aid)
                    if not await _should_keep_subscribed(sym):
                        ws = context.bot_data["ws"]
                        await ws.unsubscribe(sym)
                    await answer(f"❌ Alert #{aid} removed")
                else:
                    await answer(f"Alert #{aid} already removed")
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
            except Exception as e:
                await answer(f"Error: {e}")
        return

    if data.startswith("chart_"):
        # chart_<symbol> or chart_<symbol>_<interval> or chart_<symbol>_<interval>_<type>
        parts = data.split("_")
        if len(parts) >= 2:
            sym = normalize_symbol(parts[1])
            interval = parts[2].lower() if len(parts) >= 3 else "1h"
            chart_type = parts[3].lower() if len(parts) >= 4 else getattr(config, "DEFAULT_CHART_ENGINE", "tv")
            valid_intervals = ("15m", "30m", "1h", "2h", "4h", "1d", "1w")
            if interval not in valid_intervals:
                interval = "1h"
            if chart_type in ("tv", "chartimg") and not getattr(config, "CHART_IMG_API_KEY", ""):
                await answer("⚠️ CHART_IMG_API_KEY not set in .env — showing Tech TA.", show_alert=True)
            else:
                await answer("Updating chart...")
            chart_png = await charts.generate_chart_image(sym, interval=interval, chart_type=chart_type)
            if chart_png:
                caption, markup = _build_chart_ui(sym, interval, chart_type)
                # If editing an existing chart message with photo, update in-place!
                if query.message and getattr(query.message, "photo", None):
                    try:
                        import io
                        await query.edit_message_media(
                            media=InputMediaPhoto(media=io.BytesIO(chart_png), caption=caption, parse_mode="Markdown"),
                            reply_markup=markup,
                        )
                        return
                    except Exception as e:
                        logger.debug(f"edit_message_media failed ({e}), falling back to send_photo")

                try:
                    await context.bot.send_photo(
                        chat_id=query.message.chat_id,
                        photo=chart_png,
                        caption=caption,
                        parse_mode="Markdown",
                        reply_markup=markup,
                    )
                except Exception as e:
                    logger.error(f"Error sending chart via callback: {e}")
            else:
                await answer(f"Failed to generate chart for {sym}.", show_alert=True)
        return

    if data.startswith("list_"):
        await answer()
        try:
            rest = data[len("list_"):]
            page_s, _, filt = rest.partition("|")
            page = int(page_s) if page_s else 0
        except ValueError:
            page, filt = 0, None
        text, markup = await get_list_text_and_markup(engine, page=page, filt=filt or None)
        await _safe_edit_md(query, text, reply_markup=markup)
        return

    if data.startswith("refresh_list"):
        await answer("List refreshed!")
        rest = data[len("refresh_list"):].lstrip("_")
        page_s, _, filt = rest.partition("|")
        try:
            page = int(page_s) if page_s else 0
        except ValueError:
            page = 0
        text, markup = await get_list_text_and_markup(engine, page=page, filt=filt or None)
        stamp = f"\n_Refreshed: {datetime.datetime.now().strftime('%H:%M:%S')}_"
        await _safe_edit_md(query, text + stamp, reply_markup=markup)
        return

    if data.startswith("edit_custom_"):
        try:
            alert_id = int(data.split("_")[2])
            alert = await db.get_alert(alert_id)
            if not alert:
                await answer("Alert not found.", show_alert=True)
                return
            atype = db.alert_field(alert, "alert_type", "price") or "price"
            if atype != "price":
                await answer("Custom price is only supported for price alerts.", show_alert=True)
                return
            context.user_data["editing_alert_target"] = alert_id
            await _safe_edit_md(
                query,
                f"✏️ *Editing Target Price for Alert #{alert_id}*\n\n"
                f"Please reply with the new target price in chat (e.g. `74500` or `152.5`):",
            )
        except Exception as e:
            await answer(f"Error: {e}")
        return

    if data.startswith("edittgt_"):
        parts = data.split("_")
        if len(parts) >= 3:
            try:
                aid = int(parts[1])
                delta = float(parts[2])
                alert = await db.get_alert(aid)
                if alert:
                    atype = db.alert_field(alert, "alert_type", "price") or "price"
                    if atype != "price":
                        await answer("Target adjustment is only supported for price alerts.", show_alert=True)
                        return
                    cur_tgt = db.alert_field(alert, "target", 0)
                    cond = db.alert_field(alert, "condition", "above")
                    new_tgt = cur_tgt * (1 + delta / 100)
                    await db.set_target(aid, new_tgt, cond)
                    await answer(f"Target set to {format_price(new_tgt)} ({delta:+.0f}%)")
                    res = await _render_alert_editor(aid)
                    if res:
                        await _safe_edit_md(query, res[0], reply_markup=res[1])
            except Exception as e:
                await answer(f"Error: {e}")
        return

    if data.startswith("edit_flip_"):
        try:
            aid = int(data.split("_")[2])
            alert = await db.get_alert(aid)
            if alert:
                atype = db.alert_field(alert, "alert_type", "price") or "price"
                if atype != "price":
                    await answer("Condition flip is only supported for price alerts.", show_alert=True)
                    return
                cur_tgt = db.alert_field(alert, "target", 0)
                cond = db.alert_field(alert, "condition", "above")
                new_cond = "below" if cond == "above" else "above"
                await db.set_target(aid, cur_tgt, new_cond)
                await answer(f"Condition flipped to {new_cond.upper()}!")
                res = await _render_alert_editor(aid)
                if res:
                    await _safe_edit_md(query, res[0], reply_markup=res[1])
        except Exception as e:
            await answer(f"Error: {e}")
        return

    if data.startswith("toggle_repeat_"):
        try:
            aid = int(data.split("_")[2])
            new_val = await db.toggle_persistent(aid)
            await answer(f"Repeat {'ENABLED' if new_val else 'DISABLED'}")
            res = await _render_alert_editor(aid)
            if res:
                await _safe_edit_md(query, res[0], reply_markup=res[1])
        except Exception as e:
            await answer(f"Error: {e}")
        return

    if data.startswith("toggle_urgent_"):
        try:
            alert_id = int(data.split("_")[2])
            new_state = await db.toggle_urgent(alert_id)
            if new_state is None:
                await answer("Alert not found.", show_alert=True)
                return
            status_str = "🚨 Emergency Siren ENABLED" if new_state else "🔕 Siren DISABLED"
            await answer(status_str)
            res = await _render_alert_editor(alert_id)
            if res:
                await _safe_edit_md(query, res[0], reply_markup=res[1])
        except Exception as e:
            await answer(f"Error: {e}")
        return

    if data.startswith("snooze_"):
        parts = data.split("_")
        if len(parts) == 3:
            try:
                alert_id = int(parts[1])
                hours = int(parts[2].rstrip("h"))
                until = db.iso_in(hours=hours)
                await db.set_snooze(alert_id, until)
                await answer(f"🔕 Snoozed for {hours}h!")
                res = await _render_alert_editor(alert_id)
                if res:
                    await _safe_edit_md(query, res[0], reply_markup=res[1])
                return
            except ValueError:
                pass

    if data.startswith("edit_"):
        try:
            alert_id = int(data.split("_")[1])
        except (ValueError, IndexError):
            return
        await answer()
        res = await _render_alert_editor(alert_id)
        if not res:
            await _safe_edit_md(query, f"Alert #{alert_id} not found.", reply_markup=None)
            return
        txt, kb = res
        await _safe_edit_md(query, txt, reply_markup=kb)
        return

    if data == "removeall_confirm":
        try:
            symbols = await db.get_active_symbols()
            count = await db.remove_all_alerts()
            ws = context.bot_data["ws"]
            for symbol in symbols:
                if not await _should_keep_subscribed(symbol):
                    await ws.unsubscribe(symbol)
            await _safe_edit_md(query, f"Removed all {count} alert(s).", reply_markup=None)
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
                if not await _should_keep_subscribed(symbol):
                    ws = context.bot_data["ws"]
                    await ws.unsubscribe(symbol)
                text, markup = await get_list_text_and_markup(engine)
                prefix = f"Alert #{alert_id} removed.\n\n"
                await _safe_edit_md(query, prefix + text, reply_markup=markup)
            else:
                await _safe_edit_md(query, f"Alert #{alert_id} already removed.", reply_markup=None)
        except Exception as e:
            logger.error(f"Error handling remove: {e}")
        return

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
            [InlineKeyboardButton("Custom Price(s)", callback_data=f"addwiz_type_{coin}_custom")],
            [InlineKeyboardButton("🚨 Emergency Siren Alert", callback_data=f"addwiz_type_{coin}_siren")]
        ]
        try:
            await query.edit_message_text(
                f"Selected *{coin_safe}*. What kind of alert?",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb),
            )
        except Exception:
            pass

    elif data.startswith("addwiz_type_") or data.startswith("wiz_type_"):
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
            if not price and engine is not None:
                price = engine.get_fresh_price(symbol, max_age_sec=30)
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
        elif atype == "siren":
            context.user_data["awaiting_custom_price"] = coin
            context.user_data["is_urgent"] = True
            coin_safe = _escape_md(coin)
            try:
                await query.edit_message_text(
                    f"🚨 *Set Emergency Siren Alert for {coin_safe}:*\n\n"
                    f"Send target price(s) (e.g. `68000` or `125`):\n"
                    f"• Loud phone siren piercing Do Not Disturb / silent mode\n"
                    f"• Marked with [🚨 URGENT] badge and critical alert banner\n\n"
                    f"_Prices are automatically set to above/below based on market price._",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        elif atype in ("custom", "target"):
            context.user_data["awaiting_custom_price"] = coin
            context.user_data.pop("is_urgent", None)
            coin_safe = _escape_md(coin)
            try:
                await query.edit_message_text(
                    f"Send target price(s) for *{coin_safe}*:\n\n"
                    f"• Single price: `70000`\n"
                    f"• Multiple prices: `76000, 75000, 79000`\n\n"
                    f"_Prices are automatically set to above/below based on market price._",
                    parse_mode="Markdown"
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Bot factory
# ---------------------------------------------------------------------------

async def _post_init(application: Application) -> None:
    """Register command suggestions in Telegram UI autocomplete."""
    commands = [
        BotCommand("start", "Open Interactive Command Center"),
        BotCommand("dashboard", "Open Command Center Dashboard"),
        BotCommand("menu", "Interactive quick menu"),
        BotCommand("help", "Show help & full command list"),
        BotCommand("add", "Add a price alert (e.g. /add BTC 70000)"),
        BotCommand("urgent", "Create emergency siren alert (loud sound + max priority)"),
        BotCommand("siren", "Alias for /urgent"),
        BotCommand("list", "View and manage active alerts"),
        BotCommand("price", "Check current crypto prices"),
        BotCommand("movers", "Top 24h market gainers & losers"),
        BotCommand("chart", "Generate dark-mode price chart (e.g. /chart BTC 1h)"),
        BotCommand("pause", "Temporarily silence all alerts"),
        BotCommand("resume", "Resume alerts immediately"),
        BotCommand("snooze", "Snooze an alert (e.g. /snooze 1 2h)"),
        BotCommand("unsnooze", "Unsnooze an alert (e.g. /unsnooze 1)"),
        BotCommand("edit", "Change target price of an alert"),
        BotCommand("remove", "Delete an alert by ID"),
        BotCommand("history", "Recently fired alert history"),
        BotCommand("watchlist", "View watchlist prices"),
        BotCommand("watch", "Add coin to watchlist"),
        BotCommand("unwatch", "Remove coin from watchlist"),
        BotCommand("preset", "Set dip-buy or breakout bundles"),
        BotCommand("status", "System & alert engine status"),
        BotCommand("health", "Diagnostics & watchdog health"),
        BotCommand("grid", "Create automated price alert grid"),
        BotCommand("update", "Pull updates & restart bot (owner only)"),
        BotCommand("export", "Export alerts as JSON backup"),
        BotCommand("import", "Import alerts from JSON backup"),
        BotCommand("backup", "Download SQLite database file"),
        BotCommand("cancel", "Cancel current prompt or wizard"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Failed to set bot commands: {e}")


def create_bot(alert_engine, binance_ws) -> Application:
    """Create and configure the Telegram bot application."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set — cannot create bot.")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()
    app.bot_data["engine"] = alert_engine
    app.bot_data["ws"] = binance_ws

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("menu", cmd_dashboard))
    app.add_handler(CommandHandler("hub", cmd_dashboard))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("urgent", cmd_urgent))
    app.add_handler(CommandHandler("siren", cmd_urgent))
    app.add_handler(CommandHandler("grid", cmd_grid))
    app.add_handler(CommandHandler("update", cmd_update))
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
    app.add_handler(CommandHandler("chart", cmd_chart))
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

    async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(f"Telegram handler error: {context.error}", exc_info=context.error)

    app.add_error_handler(_on_error)

    return app
