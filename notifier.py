"""Notification delivery via ntfy.sh and optionally Telegram."""

import logging

import httpx

import config
from config import NTFY_SERVER, NTFY_TOPIC, SEND_TELEGRAM_ALERTS
import database as db

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

_NTFY_PRIORITY = {"max": 5, "high": 4, "default": 3, "low": 2, "min": 1}


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
    return _client


async def close_notifier() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


def _escape_markdown(text: str) -> str:
    """Escape Telegram Markdown (legacy) special chars in user-derived text."""
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


async def send_ntfy(
    title: str,
    message: str,
    tags: str | None = None,
    priority: str = "high",
) -> bool:
    """Send a push notification via ntfy.sh. Returns True on success."""
    if not NTFY_TOPIC:
        logger.warning("NTFY_TOPIC not set — skipping ntfy notification")
        return False
    prio = _NTFY_PRIORITY.get((priority or "high").lower(), 4)

    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": prio,
    }
    if tags:
        payload["tags"] = [tags]

    headers = {}
    if config.NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {config.NTFY_TOKEN}"
    auth = None
    if config.NTFY_USER and config.NTFY_PASSWORD and not config.NTFY_TOKEN:
        auth = (config.NTFY_USER, config.NTFY_PASSWORD)

    try:
        client = await _get_client()
        response = await client.post(NTFY_SERVER, json=payload, headers=headers or None, auth=auth)
        response.raise_for_status()
        logger.info(f"ntfy notification sent: {title}")
        return True
    except Exception as e:
        logger.error(f"Failed to send ntfy notification: {e}")
        return False


async def flush_pending(telegram_bot=None, chat_id: int | None = None) -> int:
    """Retry queued notifications. Returns count delivered."""
    try:
        pending = await db.pop_pending_notifications(20)
    except Exception as e:
        logger.error(f"Failed to read pending queue: {e}")
        return 0
    delivered = 0
    for nid, title, message, tags, priority, symbol, _attempts in pending:
        ok = await send_ntfy(title, message, tags=tags or None, priority=priority or "high")
        if ok:
            try:
                await db.ack_notification(nid)
            except Exception:
                pass
            delivered += 1
        else:
            try:
                await db.bump_notification(nid)
            except Exception:
                pass
            # ntfy down → Telegram fallback so nothing is silently lost.
            if telegram_bot and chat_id and symbol:
                try:
                    await telegram_bot.send_message(
                        chat_id=chat_id,
                        text=f"*{_escape_markdown(symbol.replace('USDT', ''))}* queued alert:\n{message}",
                        parse_mode="Markdown",
                    )
                    await db.ack_notification(nid)
                    delivered += 1
                except Exception as e:
                    logger.error(f"Failed Telegram fallback for queued #{nid}: {e}")
    if delivered:
        logger.info(f"Flushed {delivered} queued notification(s)")
    return delivered


async def post_webhook(payload: dict) -> bool:
    """POST a JSON payload to WEBHOOK_URL. Returns True on success."""
    url = (config.WEBHOOK_URL or "").strip()
    if not url:
        return False
    try:
        client = await _get_client()
        resp = await client.post(url, json=payload, timeout=10.0)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Webhook POST failed: {e}")
        return False


async def ping_healthcheck() -> None:
    url = (config.HEALTHCHECK_URL or "").strip()
    if not url:
        return
    try:
        client = await _get_client()
        await client.get(url, timeout=10.0)
    except Exception as e:
        logger.warning(f"Healthcheck ping failed: {e}")


async def send_alert_notification(
    symbol: str,
    condition: str,
    target: float,
    current_price: float,
    telegram_bot=None,
    chat_id: int | None = None,
    priority: str = "high",
    detail: str = "",
    alert_id: int | None = None,
) -> None:
    """Send an alert notification via ntfy (and optionally Telegram)."""
    coin = symbol.replace("USDT", "")
    coin_safe = _escape_markdown(coin)
    direction = "above" if condition == "above" else "below"
    emoji = "\U0001f4c8" if condition == "above" else "\U0001f4c9"
    tag = "chart_with_upwards_trend" if condition == "above" else "chart_with_downwards_trend"

    title = f"{emoji} {coin} Alert Triggered"
    message = (
        f"{coin} crossed {direction} ${target:,.2f}\n"
        f"Current price: ${current_price:,.2f}"
    )
    if detail:
        message += f"\n{detail}"

    ntfy_ok = await send_ntfy(title, message, tags=tag, priority=priority)
    if not ntfy_ok:
        # Queue for retry — flushed hourly and on startup.
        try:
            await db.queue_notification(title, message, tags=tag, priority=priority, symbol=symbol)
        except Exception as e:
            logger.error(f"Failed to queue notification: {e}")

    # Outgoing webhook (Home Assistant / Discord bridging).
    try:
        await post_webhook({
            "alert_id": alert_id, "symbol": symbol, "condition": condition,
            "target": target, "price": current_price, "detail": detail or "",
        })
    except Exception:
        pass

    # Optionally also send via Telegram
    if SEND_TELEGRAM_ALERTS and telegram_bot and chat_id:
        try:
            await telegram_bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{emoji} *{coin_safe} Alert Triggered*\n\n"
                    f"{coin_safe} crossed {direction} `${target:,.2f}`\n"
                    f"Current price: `${current_price:,.2f}`"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")
    elif not ntfy_ok and telegram_bot and chat_id:
        # Fallback: ntfy failed/misconfigured — still deliver via Telegram
        # so alerts are never silently dropped.
        try:
            await telegram_bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{emoji} *{coin_safe} Alert Triggered*\n\n"
                    f"{coin_safe} crossed {direction} `${target:,.2f}`\n"
                    f"Current price: `${current_price:,.2f}`"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram fallback alert: {e}")

