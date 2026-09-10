"""Shared price fetching via Bybit REST API.

Single source of truth for "current price" lookups used by the Telegram bot
(commands, wizards, daily briefing) and for validating that a symbol exists.
A shared httpx client + short TTL cache keeps this fast and avoids hammering
the API when the briefing or list view fetches several coins at once.
"""

import asyncio
import logging
import time

import httpx

import config

logger = logging.getLogger(__name__)

_BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()
_cache: dict = {}  # symbol -> (ticker_dict, monotonic_ts)


def _ttl() -> int:
    try:
        return max(2, int(config.PRICE_CACHE_TTL_SEC))
    except Exception:
        return 10


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=5.0))
    return _client


async def close_price_client() -> None:
    """Close the shared HTTP client (called on shutdown)."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


async def get_ticker(symbol: str):
    """Fetch the full Bybit ticker dict for a symbol (cached). None if unknown."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return None
    now = time.monotonic()
    ttl = _ttl()
    entry = _cache.get(symbol)
    if entry and (now - entry[1]) < ttl:
        return entry[0]
    try:
        client = await _get_client()
        resp = await client.get(
            _BYBIT_TICKERS_URL, params={"category": "linear", "symbol": symbol}
        )
        if resp.status_code != 200:
            logger.warning(f"Bybit tickers HTTP {resp.status_code} for {symbol}")
            return entry[0] if entry else None
        data = resp.json()
        items = (data.get("result") or {}).get("list") or []
        if not items or not isinstance(items[0], dict):
            return None  # unknown symbol
        ticker = dict(items[0])
        _cache[symbol] = (ticker, now)
        return ticker
    except Exception as e:
        logger.warning(f"Failed to fetch ticker for {symbol}: {e}")
        return entry[0] if entry else None


def cached_ticker(symbol: str):
    """Return the last known ticker dict without any network I/O."""
    entry = _cache.get((symbol or "").upper())
    return entry[0] if entry else None


def ticker_price(ticker) -> float | None:
    try:
        price = float((ticker or {}).get("lastPrice") or 0)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def ticker_change_24h(ticker) -> float | None:
    """Return 24h change as a fraction (0.05 = +5%), or None."""
    try:
        raw = (ticker or {}).get("price24hPcnt")
        if raw is None:
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def cached_price(symbol: str) -> float | None:
    """Return the last known price from cache without any network I/O."""
    entry = _cache.get((symbol or "").upper())
    if not entry:
        return None
    if (time.monotonic() - entry[1]) < _ttl():
        return ticker_price(entry[0])
    return ticker_price(entry[0])


async def get_current_price(symbol: str) -> float | None:
    """Fetch the current last price for a symbol (e.g. 'BTCUSDT').

    Returns None when the symbol is unknown or the request fails.
    Results are cached for a few seconds.
    """
    return ticker_price(await get_ticker(symbol))


async def symbol_exists(symbol: str) -> bool:
    """Check whether a symbol is tradeable on Bybit (cached)."""
    return await get_current_price(symbol) is not None


async def get_prices(symbols) -> dict:
    """Fetch several symbols concurrently. Returns {symbol: price | None}."""
    symbols = [s.strip().upper() for s in symbols if s and s.strip()]
    if not symbols:
        return {}
    results = await asyncio.gather(*(get_current_price(s) for s in symbols))
    return dict(zip(symbols, results))


async def get_tickers(symbols) -> dict:
    """Fetch several full tickers concurrently. Returns {symbol: ticker | None}."""
    symbols = [s.strip().upper() for s in symbols if s and s.strip()]
    if not symbols:
        return {}
    results = await asyncio.gather(*(get_ticker(s) for s in symbols))
    return dict(zip(symbols, results))


async def get_top_movers(limit: int = 10) -> list:
    """Return top linear tickers by absolute 24h change: [(symbol, price, pct)]."""
    try:
        client = await _get_client()
        resp = await client.get(_BYBIT_TICKERS_URL, params={"category": "linear"})
        if resp.status_code != 200:
            return []
        items = (resp.json().get("result") or {}).get("list") or []
        rows = []
        for item in items:
            try:
                symbol = str(item.get("symbol") or "").upper()
                if not symbol.endswith("USDT"):
                    continue
                price = float(item.get("lastPrice") or 0)
                pct = float(item.get("price24hPcnt") or 0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                rows.append((symbol, price, pct))
        rows.sort(key=lambda r: abs(r[2]), reverse=True)
        return rows[: max(1, min(limit, 25))]
    except Exception as e:
        logger.warning(f"Failed to fetch top movers: {e}")
        return []
