"""Entry point — starts Bybit WebSocket, Telegram bot, and alert engine."""

import asyncio
import datetime
import logging
import signal

import config
from config import TELEGRAM_BOT_TOKEN, validate_config
from database import close_db, get_active_symbols, get_watchlist, init_db
from binance_ws import BybitWebSocket
from alert_engine import AlertEngine
from telegram_bot import create_bot, format_price
from prices import close_price_client, get_prices, get_ticker, ticker_price, get_current_price
from notifier import close_notifier, flush_pending, ping_healthcheck
import database as db
import charts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Silence noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

logger = logging.getLogger("crypto-alerts")


def _parse_briefing_time(value: str) -> tuple:
    try:
        h, m = value.split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except ValueError:
        pass
    return 8, 0


async def daily_briefing_task(app) -> None:
    """Sends a daily summary of configured coins at DAILY_BRIEFING_TIME UTC."""
    if not config.TELEGRAM_USER_ID:
        logger.warning("Daily briefing disabled — TELEGRAM_USER_ID not set.")
        return

    symbols = list(config.DAILY_BRIEFING_SYMBOLS)
    hour, minute = _parse_briefing_time(config.DAILY_BRIEFING_TIME)

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)

        sleep_seconds = (target - now).total_seconds()
        logger.info(f"Daily briefing scheduled in {sleep_seconds / 3600:.1f} hours.")

        try:
            await asyncio.sleep(sleep_seconds)

            fng = await charts.get_fear_and_greed()
            prices = await get_prices(symbols)
            lines = ["*Daily Crypto Briefing*\n"]
            if fng:
                lines.append(f"🧠 *Market Sentiment:* {fng['value']}/100 ({fng['classification']})\n")
            for symbol in symbols:
                price = prices.get(symbol)
                coin = symbol.replace("USDT", "")
                if price:
                    lines.append(f"- *{coin}*: {format_price(price)}")
                else:
                    lines.append(f"- *{coin}*: unavailable")

            try:
                await app.bot.send_message(
                    chat_id=config.TELEGRAM_USER_ID,
                    text="\n".join(lines),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"Failed to send daily briefing: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in daily briefing: {e}")
            await asyncio.sleep(60)


async def funding_poller_task(engine) -> None:
    """Every 15 min, evaluate funding-rate alerts for their symbols."""
    while True:
        try:
            await asyncio.sleep(900)
            await engine.prune_expired_now()
            symbols = sorted(await db.get_active_symbols())
            for symbol in symbols:
                try:
                    ticker = await get_ticker(symbol)
                    rate = ticker.get("fundingRate") if ticker else None
                    if rate:
                        await engine.on_funding_update(symbol, float(rate))
                except Exception as e:
                    logger.error(f"Funding poll failed for {symbol}: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Funding poller error: {e}")
            await asyncio.sleep(60)


async def maintenance_task(app) -> None:
    """Hourly: flush queued notifications, prune expired."""
    first = True
    while True:
        try:
            if not first:
                await asyncio.sleep(3600)
            first = False
            bot = app.bot if app else None
            await flush_pending(telegram_bot=bot, chat_id=config.TELEGRAM_USER_ID or None)
            try:
                await db.prune_expired()
            except Exception:
                pass
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Maintenance task error: {e}")
            await asyncio.sleep(300)


async def heartbeat_task() -> None:
    """Pings external uptime monitor (e.g. Healthchecks.io / Uptime Kuma) every 5 minutes."""
    if not (config.HEALTHCHECK_URL or "").strip():
        logger.info("Heartbeat monitor disabled — HEALTHCHECK_URL not set.")
        return

    interval = getattr(config, "HEARTBEAT_INTERVAL_SEC", 300)
    logger.info(f"Heartbeat monitor active — pinging every {interval}s ({interval // 60}m).")

    # Send initial ping immediately on startup so monitor knows service is up
    await ping_healthcheck()

    while True:
        try:
            await asyncio.sleep(interval)
            await ping_healthcheck()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Heartbeat ping error: {e}")
            await asyncio.sleep(30)


async def main() -> None:
    # ── Validate configuration ───────────────────────────────────────────
    problems = validate_config()
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is required. See .env.example")
        raise SystemExit(1)
    for p in problems:
        logger.warning(p)
    if not config.NTFY_TOPIC and not config.SEND_TELEGRAM_ALERTS:
        logger.warning(
            "Neither NTFY_TOPIC nor SEND_TELEGRAM_ALERTS is set — "
            "alerts will fall back to Telegram delivery only."
        )

    # ── Initialize database ──────────────────────────────────────────────
    await init_db()
    logger.info("Database initialized")

    # ── Create alert engine ──────────────────────────────────────────────
    engine = AlertEngine()

    # ── Create Bybit WebSocket client ──────────────────────────────────
    ws = BybitWebSocket(on_price_update=engine.on_price_update)
    engine.set_binance_ws(ws)

    # ── Create Telegram bot ──────────────────────────────────────────────
    app = create_bot(engine, ws)
    engine.set_telegram(app.bot, config.TELEGRAM_USER_ID)

    # ── Load existing alerts + watchlist and subscribe to their symbols ──
    alert_symbols = await get_active_symbols()
    watchlist_symbols = list(config.WATCHLIST_SYMBOLS or ("BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT"))
    try:
        watchlist_symbols.extend(await get_watchlist())
    except Exception:
        pass
    all_symbols = sorted(set(alert_symbols) | set(watchlist_symbols))
    for symbol in all_symbols:
        await ws.subscribe(symbol)
    logger.info(f"Subscribed to {len(all_symbols)} symbol(s) ({len(alert_symbols)} alerts, {len(watchlist_symbols)} watchlist)")

    # ── Start Telegram bot (polling in background) ───────────────────────
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot started — send /help to your bot")

    # ── Start Daily Briefing + maintenance/funding/heartbeat tasks ───────
    briefing_task = asyncio.create_task(daily_briefing_task(app))
    maintenance_task_handle = asyncio.create_task(maintenance_task(app))
    funding_task = asyncio.create_task(funding_poller_task(engine))
    heartbeat_task_handle = asyncio.create_task(heartbeat_task())

    # ── Start Webhook receiver if enabled ────────────────────────────────
    webhook_task = None
    if config.WEBHOOK_ENABLED:
        from webhook_server import run_webhook_server
        webhook_task = asyncio.create_task(run_webhook_server(telegram_bot=app.bot))
        logger.info(f"Webhook receiver started on port {config.WEBHOOK_PORT or 8080}")

    # ── Graceful shutdown on SIGINT/SIGTERM ───────────────────────────────
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received...")
        ws.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # ── Run WebSocket (main loop) ────────────────────────────────────────
    logger.info("Starting Bybit WebSocket — listening for prices...")

    ws_task = asyncio.create_task(ws.run())
    stop_task = asyncio.create_task(stop_event.wait())

    try:
        # Wait for either the WS to stop or a shutdown signal
        done, pending = await asyncio.wait(
            [ws_task, stop_task], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down...")
    finally:
        ws.stop()
        ws_task.cancel()
        briefing_task.cancel()
        maintenance_task_handle.cancel()
        funding_task.cancel()
        heartbeat_task_handle.cancel()
        bg_tasks = [ws_task, briefing_task, maintenance_task_handle, funding_task, heartbeat_task_handle]
        if webhook_task:
            from webhook_server import stop_webhook_server
            stop_webhook_server()
            webhook_task.cancel()
            bg_tasks.append(webhook_task)
        for bg in bg_tasks:
            try:
                await bg
            except asyncio.CancelledError:
                pass
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.warning(f"Error during bot shutdown: {e}")
        await close_price_client()
        await close_notifier()
        await close_db()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
