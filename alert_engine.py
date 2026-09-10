"""Alert engine: connects real-time prices to alert rules."""

import asyncio
import datetime
import logging

import database as db
import config
from config import PERSISTENT_ALERT_COOLDOWN_SEC
from notifier import send_alert_notification
from prices import format_price, register_live_price

logger = logging.getLogger(__name__)


def _parse_utc_timestamp(value) -> datetime.datetime | None:
    """Parse a SQLite CURRENT_TIMESTAMP value as an aware UTC datetime."""
    if not value:
        return None
    try:
        if isinstance(value, datetime.datetime):
            dt = value
        else:
            text = str(value).strip().replace("Z", "")
            if "T" in text:
                dt = datetime.datetime.fromisoformat(text)
            else:
                dt = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception as e:
        logger.warning(f"Failed to parse timestamp {value!r}: {e}")
        return None



class AlertEngine:
    """Receives price updates, checks alert conditions, and triggers notifications.

    Alert kinds:
      price   — one-shot or repeat threshold (above/below target).
      pct     — % move from base_price, re-armed (re-based) after firing.
      trail   — follows the peak; fires on a pct pullback from the peak.
      move    — % move within window_min minutes from base_price.
      funding — funding-rate threshold (checked by the funding poller).
    Expired and snoozed alerts are skipped; expired rows are pruned lazily.
    """

    def __init__(self):
        self.last_prices: dict[str, float] = {}
        self.last_update_at: dict[str, datetime.datetime] = {}
        self.telegram_bot = None
        self.chat_id: int | None = None
        self.binance_ws = None
        self.mute_until: datetime.datetime | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._stats = {"checks": 0, "triggered": 0, "errors": 0}
        self.started_at = datetime.datetime.now(datetime.timezone.utc)

    def pause_alerts(self, hours: int) -> None:
        self.mute_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
        
    def resume_alerts(self) -> None:
        self.mute_until = None
        
    def is_muted(self) -> bool:
        if self.mute_until:
            if datetime.datetime.now(datetime.timezone.utc) < self.mute_until:
                return True
            else:
                self.mute_until = None
        return False

    def set_telegram(self, bot, chat_id: int) -> None:
        """Set the Telegram bot instance for optional alert delivery."""
        self.telegram_bot = bot
        self.chat_id = chat_id

    def set_binance_ws(self, ws) -> None:
        """Set the Binance WebSocket client for subscription management."""
        self.binance_ws = ws

    def get_stats(self) -> dict:
        return dict(self._stats)

    def _priority(self, is_persistent: bool) -> str:
        return config.ALERT_PRIORITY_REPEAT if is_persistent else config.ALERT_PRIORITY_ONESHOT

    async def _fire(self, alert, price: float, detail: str = "") -> None:
        """Deliver one trigger, log it, and apply one-shot/repeat bookkeeping."""
        alert_id = db.alert_field(alert, "id")
        symbol = db.alert_field(alert, "symbol", "")
        target = db.alert_field(alert, "target", price)
        condition = db.alert_field(alert, "condition", "above")
        is_persistent = bool(db.alert_field(alert, "is_persistent", 0))
        alert_type = db.alert_field(alert, "alert_type", "price") or "price"
        self._stats["triggered"] += 1
        tag = " [REPEAT]" if is_persistent else ""
        logger.info(
            f"Alert #{alert_id}{tag} [{alert_type}] triggered: {symbol} "
            f"{condition} {format_price(target)} (current: {format_price(price)}) {detail}".rstrip()
        )
        await send_alert_notification(
            symbol=symbol, condition=condition, target=target,
            current_price=price, telegram_bot=self.telegram_bot,
            chat_id=self.chat_id, priority=self._priority(is_persistent),
            detail=detail, alert_id=alert_id,
        )
        try:
            await db.log_fired(alert_id, symbol, condition, target, price, detail)
        except Exception as e:
            logger.error(f"Failed to log fired alert #{alert_id}: {e}")
        # Re-arm repeating kinds by re-basing the anchor at the fire price.
        if alert_type in ("pct", "trail", "move"):
            try:
                await db.rebase_alert(alert_id, price)
            except Exception as e:
                logger.error(f"Failed to rebase alert #{alert_id}: {e}")
        elif is_persistent:
            await db.update_last_triggered(alert_id)
        else:
            await db.remove_alert(alert_id)

    async def prune_expired_now(self) -> int:
        """Delete expired alerts. Returns count removed."""
        try:
            return await db.prune_expired()
        except Exception as e:
            logger.error(f"Failed to prune expired alerts: {e}")
            return 0

    async def on_funding_update(self, symbol: str, funding_rate: float) -> None:
        """Evaluate funding-rate alerts for a symbol."""
        try:
            if not (funding_rate == funding_rate and abs(funding_rate) < 1):
                return
            symbol = symbol.upper()
            if config.quiet_now():
                return
            now = datetime.datetime.now(datetime.timezone.utc)
            alerts = await db.get_alerts_for_symbol(symbol)
            for alert in alerts:
                try:
                    alert_type = db.alert_field(alert, "alert_type", "price") or "price"
                    if alert_type != "funding":
                        continue
                    alert_id = db.alert_field(alert, "id")
                    threshold = db.alert_field(alert, "funding_rate") or 0
                    condition = db.alert_field(alert, "condition", "above")
                    snoozed_until = db.alert_field(alert, "snoozed_until")
                    if snoozed_until:
                        until = _parse_utc_timestamp(snoozed_until)
                        if until and now <= until:
                            continue
                    hit = (condition == "above" and funding_rate >= threshold) or (
                        condition == "below" and funding_rate <= threshold)
                    if hit:
                        self._stats["triggered"] += 1
                        logger.info(f"Funding alert #{alert_id} triggered: {symbol} "
                                    f"{condition} {threshold:+.4%} (now {funding_rate:+.4%})")
                        await send_alert_notification(
                            symbol=symbol, condition=condition, target=threshold * 100,
                            current_price=funding_rate * 100, telegram_bot=self.telegram_bot,
                            chat_id=self.chat_id, priority=self._priority(True),
                            detail=f"funding {funding_rate:+.4%}", alert_id=alert_id,
                        )
                        await db.log_fired(alert_id, symbol, condition, threshold * 100,
                                           funding_rate * 100, f"funding {funding_rate:+.4%}")
                        if db.alert_field(alert, "is_persistent", 0):
                            await db.update_last_triggered(alert_id)
                        else:
                            await db.remove_alert(alert_id)
                except Exception as e:
                    self._stats["errors"] += 1
                    logger.error(f"Error processing funding alert {alert}: {e}", exc_info=True)
        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Error in on_funding_update({symbol}): {e}", exc_info=True)

    async def on_price_update(self, symbol: str, price: float) -> None:
        """Called on every price tick from the WebSocket."""
        try:
            if not (price > 0 and price == price and price != float("inf")):
                return
            symbol = symbol.upper()
            register_live_price(symbol, price)
            now = datetime.datetime.now(datetime.timezone.utc)

            if self.is_muted():
                self.last_prices[symbol] = price
                self.last_update_at[symbol] = now
                return

            self.last_prices[symbol] = price
            self.last_update_at[symbol] = now
            self._stats["checks"] += 1

            lock = self._locks.setdefault(symbol, asyncio.Lock())
            async with lock:
                try:
                    pruned = await db.prune_expired()
                    if pruned:
                        logger.info(f"Pruned {pruned} expired alert(s)")
                except Exception as e:
                    logger.error(f"Failed to prune expired alerts: {e}")
                alerts = await db.get_alerts_for_symbol(symbol)
                if not alerts:
                    return
                if config.quiet_now():
                    return  # armed but silent inside quiet hours
                triggered_any = False
                for alert in alerts:
                    try:
                        alert_id = db.alert_field(alert, "id")
                        target = db.alert_field(alert, "target", 0)
                        condition = db.alert_field(alert, "condition", "above")
                        is_persistent = bool(db.alert_field(alert, "is_persistent", 0))
                        last_triggered_at = db.alert_field(alert, "last_triggered_at")
                        alert_type = db.alert_field(alert, "alert_type", "price") or "price"
                        snoozed_until = db.alert_field(alert, "snoozed_until")
                        cooldown = db.alert_field(alert, "cooldown_sec") or PERSISTENT_ALERT_COOLDOWN_SEC
                        try:
                            cooldown = max(60, int(cooldown))
                        except (TypeError, ValueError):
                            cooldown = PERSISTENT_ALERT_COOLDOWN_SEC
                        if condition not in ("above", "below") or alert_type not in (
                            "price", "pct", "trail", "move", "funding",
                        ):
                            continue
                        # Snoozed alerts stay armed but silent. Second-resolution
                        # timestamps: treat the boundary second as still snoozed.
                        if snoozed_until:
                            until = _parse_utc_timestamp(snoozed_until)
                            if until and now <= until:
                                continue
                        if is_persistent and last_triggered_at and alert_type == "price":
                            last_time = _parse_utc_timestamp(last_triggered_at)
                            if last_time and (now - last_time).total_seconds() < cooldown:
                                continue

                        if alert_type == "price":
                            hit = (condition == "above" and price >= target) or (
                                condition == "below" and price <= target
                            )
                            if not hit:
                                continue
                            triggered_any = True
                            await self._fire(alert, price)
                        elif alert_type == "pct":
                            pct = db.alert_field(alert, "pct") or 0
                            base = db.alert_field(alert, "base_price") or 0
                            if not (pct and base):
                                continue
                            move = (price - base) / base * 100
                            if (condition == "above" and move >= pct) or (
                                condition == "below" and move <= -pct
                            ):
                                triggered_any = True
                                await self._fire(alert, price, f"{move:+.2f}% from {format_price(base)}")
                        elif alert_type == "trail":
                            pct = db.alert_field(alert, "pct") or 0
                            peak = db.alert_field(alert, "peak_price") or price
                            if price > peak:
                                try:
                                    await db.touch_peak(alert_id, price)
                                except Exception as e:
                                    logger.error(f"Failed to update peak #{alert_id}: {e}")
                                continue
                            if peak > 0 and pct:
                                drop = (peak - price) / peak * 100
                                if drop >= pct:
                                    triggered_any = True
                                    await self._fire(alert, price, f"{drop:.2f}% below peak {format_price(peak)}")
                        elif alert_type == "move":
                            pct = db.alert_field(alert, "pct") or 0
                            base = db.alert_field(alert, "base_price") or 0
                            window_min = db.alert_field(alert, "window_min") or 0
                            anchor_ts = db.alert_field(alert, "last_triggered_at") or db.alert_field(alert, "created_at")
                            anchor = _parse_utc_timestamp(anchor_ts)
                            if not (pct and base and window_min):
                                continue
                            if anchor and (now - anchor).total_seconds() > window_min * 60:
                                # Window elapsed without triggering — re-anchor.
                                try:
                                    await db.rebase_alert(alert_id, price)
                                except Exception as e:
                                    logger.error(f"Failed to re-anchor #{alert_id}: {e}")
                                continue
                            move = (price - base) / base * 100
                            if abs(move) >= pct:
                                triggered_any = True
                                await self._fire(alert, price, f"{move:+.2f}% in {window_min}m")
                        # funding alerts are evaluated by the funding poller, not ticks.
                    except Exception as e:
                        self._stats["errors"] += 1
                        logger.error(f"Error processing alert {alert}: {e}", exc_info=True)
                if triggered_any and self.binance_ws:
                    try:
                        remaining = await db.get_alerts_for_symbol(symbol)
                        if not remaining:
                            await self.binance_ws.unsubscribe(symbol)
                            logger.info(f"No remaining alerts for {symbol} — unsubscribed")
                    except Exception as e:
                        logger.error(f"Error unsubscribing {symbol}: {e}")
        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Error in on_price_update({symbol}): {e}", exc_info=True)
