import asyncio
import json
import logging
import random
import time

import websockets

logger = logging.getLogger(__name__)

# Bybit v5 public linear (USDT perps) WebSocket
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"

# Data-quality guardrails
MAX_SYMBOLS = 100          # Bybit allows 10 args/msg; we batch, but cap total
STALE_AFTER_SEC = 300      # warn if no tick for a symbol this long


class BybitWebSocket:
    """Persistent WebSocket connection to Bybit with auto-reconnect.

    Dynamically subscribes/unsubscribes to ticker streams for symbols
    that have active alerts. All mutation of subscription state is
    serialized through an asyncio lock so concurrent bot handlers and
    the reconnect path cannot interleave subscribe frames.
    """

    def __init__(self, on_price_update):
        self.on_price_update = on_price_update
        self.ws = None
        self.subscribed_symbols: set[str] = set()
        self.last_tick_at: dict[str, float] = {}
        self._stale_warned: set[str] = set()
        self._running = False
        self._ping_task = None
        self._watchdog_task = None
        self._lock = asyncio.Lock()
        self.connected = False
        self.reconnects = 0

    async def _send_ping(self) -> None:
        """Bybit requires a manual ping every 20 seconds."""
        while self._running and self.ws:
            try:
                await asyncio.sleep(20)
                if self.ws:
                    await self.ws.send(json.dumps({"req_id": str(int(time.time())), "op": "ping"}))
            except Exception:
                break

    async def _send(self, msg: dict) -> bool:
        ws = self.ws
        if not ws:
            return False
        try:
            await ws.send(json.dumps(msg))
            return True
        except Exception as e:
            logger.error(f"WebSocket send failed: {e}")
            return False

    async def subscribe(self, symbol: str) -> bool:
        """Subscribe to real-time price updates for a symbol."""
        # Bybit expects uppercase, e.g., BTCUSDT
        symbol = symbol.upper()
        async with self._lock:
            if symbol in self.subscribed_symbols:
                return True
            if len(self.subscribed_symbols) >= MAX_SYMBOLS:
                logger.warning(f"Symbol cap reached ({MAX_SYMBOLS}) — ignoring {symbol}")
                return False
            self.subscribed_symbols.add(symbol)
            self._stale_warned.discard(symbol)
            if self.ws:
                ok = await self._send({"op": "subscribe", "args": [f"tickers.{symbol}"]})
                if ok:
                    logger.info(f"Subscribed to Bybit tickers.{symbol}")
                return ok
            return True

    async def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from price updates for a symbol."""
        symbol = symbol.upper()
        async with self._lock:
            if symbol not in self.subscribed_symbols:
                return
            self.subscribed_symbols.discard(symbol)
            self.last_tick_at.pop(symbol, None)
            self._stale_warned.discard(symbol)
            if self.ws:
                ok = await self._send({"op": "unsubscribe", "args": [f"tickers.{symbol}"]})
                if ok:
                    logger.info(f"Unsubscribed from Bybit tickers.{symbol}")

    async def _resubscribe_all(self) -> None:
        """Re-subscribe to all tracked symbols after reconnect (10/batch)."""
        async with self._lock:
            symbols = sorted(self.subscribed_symbols)
            ws = self.ws
        if not symbols or not ws:
            return
        success_count = 0
        for i in range(0, len(symbols), 10):
            batch = [f"tickers.{s}" for s in symbols[i:i + 10]]
            try:
                await ws.send(json.dumps({"op": "subscribe", "args": batch}))
                success_count += len(batch)
            except Exception as e:
                logger.error(f"Failed to resubscribe batch: {e}")
                continue
        logger.info(f"Resubscribed to {success_count}/{len(symbols)} stream(s)")

    async def _watchdog(self) -> None:
        """Warn and auto-resubscribe for symbols that stop receiving ticks."""
        try:
            while self._running:
                await asyncio.sleep(60)
                now = time.monotonic()
                async with self._lock:
                    symbols = list(self.subscribed_symbols)
                for symbol in symbols:
                    last = self.last_tick_at.get(symbol)
                    stale = last is None or (now - last) > STALE_AFTER_SEC
                    if stale and symbol not in self._stale_warned:
                        self._stale_warned.add(symbol)
                        age = "never" if last is None else f"{now - last:.0f}s ago"
                        logger.warning(
                            f"No Bybit ticks for {symbol} (last: {age}) — attempting re-subscribe."
                        )
                        if self.ws:
                            try:
                                await self._send({"op": "subscribe", "args": [f"tickers.{symbol}"]})
                            except Exception:
                                pass
                    elif not stale:
                        self._stale_warned.discard(symbol)
        except asyncio.CancelledError:
            pass

    async def run(self) -> None:
        """Main loop: connect, listen, auto-reconnect with backoff + jitter."""
        self._running = True
        backoff = 1
        self._watchdog_task = asyncio.create_task(self._watchdog())

        while self._running:
            try:
                async with websockets.connect(
                    BYBIT_WS_URL, ping_interval=None, close_timeout=5,
                    max_size=2 ** 20,
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    backoff = 1
                    logger.info("Connected to Bybit WebSocket")
                    self._ping_task = asyncio.create_task(self._send_ping())
                    await self._resubscribe_all()

                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(message)
                            if not isinstance(data, dict):
                                continue
                            if data.get("op") == "pong" or "topic" not in data:
                                continue
                            topic = data.get("topic", "")
                            payload = data.get("data") or {}
                            if not topic.startswith("tickers."):
                                continue
                            symbol = topic.split(".")[1].upper() if "." in topic else ""
                            if not symbol or not isinstance(payload, dict):
                                continue
                            price_str = payload.get("lastPrice")
                            if not price_str:
                                continue
                            try:
                                price = float(price_str)
                            except (TypeError, ValueError):
                                continue
                            if not (price > 0 and price == price and price != float("inf")):
                                continue
                            async with self._lock:
                                if symbol in self.subscribed_symbols:
                                    self.last_tick_at[symbol] = time.monotonic()
                                    self._stale_warned.discard(symbol)
                                else:
                                    continue
                            try:
                                await self.on_price_update(symbol, price)
                            except Exception as e:
                                logger.error(f"on_price_update failed for {symbol}: {e}")
                        except (json.JSONDecodeError, KeyError, ValueError):
                            continue
                        except Exception as e:
                            logger.error(f"Error handling WS message: {e}")

            except Exception as e:
                # websockets 15+ raises ConnectionClosed under websockets.exceptions;
                # top-level alias may not exist, so treat all as reconnectable.
                self.reconnects += 1
                logger.warning(f"WebSocket disconnected ({e}), reconnecting in {backoff}s...")

            self.ws = None
            self.connected = False
            if self._ping_task:
                self._ping_task.cancel()
                try:
                    await self._ping_task
                except asyncio.CancelledError:
                    pass
                self._ping_task = None

            if self._running:
                jitter = random.uniform(0, backoff * 0.25)
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, 60)

        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

    def stop(self) -> None:
        """Signal the WebSocket loop to stop."""
        self._running = False
        if self.ws:
            try:
                asyncio.create_task(self.ws.close())
            except Exception:
                pass
