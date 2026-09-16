"""Shared price fetching via Bybit REST API with multi-exchange fallbacks.

Single source of truth for "current price" lookups used by the Telegram bot
(commands, wizards, daily briefing) and for validating that a symbol exists.
A shared httpx client + short TTL cache keeps this fast.

If Bybit REST is geo-blocked (e.g., HTTP 403 on US cloud VMs), it seamlessly
falls back to Binance.US, Gate.io, and live WebSocket price caches.
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
MAX_CACHE_ENTRIES = 500


def _prune_cache() -> None:
    """Keep memory bounded by evicting oldest entries when cache grows too large."""
    if len(_cache) > MAX_CACHE_ENTRIES:
        sorted_keys = sorted(_cache.keys(), key=lambda k: _cache[k][1])
        for k in sorted_keys[: len(sorted_keys) // 5]:
            _cache.pop(k, None)


def format_price(price: float) -> str:
    """Format a price dynamically based on magnitude (handles sub-cent coins cleanly)."""
    try:
        price = float(price)
    except (TypeError, ValueError):
        return "N/A"
    if price != price or price == float("inf") or price <= 0:
        return "N/A"
    if price < 0.00001:
        return f"${price:.8f}"
    elif price < 0.001:
        return f"${price:.6f}"
    elif price < 1:
        return f"${price:.4f}"
    else:
        return f"${price:,.2f}"


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
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Accept": "application/json",
                }
                _client = httpx.AsyncClient(
                    headers=headers,
                    timeout=httpx.Timeout(8.0, connect=5.0),
                    follow_redirects=True,
                )
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


def register_live_price(symbol: str, price: float, pct_change: float | str | None = None) -> None:
    """Seed or update cache from live WebSocket ticks (never geo-blocked)."""
    symbol = (symbol or "").strip().upper()
    if not symbol or not (price > 0):
        return
    now = time.monotonic()
    existing = _cache.get(symbol)
    if pct_change is not None:
        try:
            pct = str(float(pct_change))
        except (TypeError, ValueError):
            pct = existing[0].get("price24hPcnt") if existing else None
    else:
        pct = existing[0].get("price24hPcnt") if existing else None

    ticker = {
        "symbol": symbol,
        "lastPrice": str(price),
        "price24hPcnt": pct,
    }
    _prune_cache()
    _cache[symbol] = (ticker, now)


async def _fetch_from_bybit(client: httpx.AsyncClient, symbol: str) -> dict | None:
    try:
        resp = await client.get(
            _BYBIT_TICKERS_URL, params={"category": "linear", "symbol": symbol}
        )
        if resp.status_code == 200:
            data = resp.json()
            items = (data.get("result") or {}).get("list") or []
            if items and isinstance(items[0], dict):
                return dict(items[0])
        elif resp.status_code == 403:
            logger.debug(f"Bybit 403 for {symbol} (geo-restricted); falling back to secondary APIs")
    except Exception as e:
        logger.debug(f"Bybit ticker error for {symbol}: {e}")
    return None


async def _fetch_from_binance_us(client: httpx.AsyncClient, symbol: str) -> dict | None:
    try:
        resp = await client.get(
            f"https://api.binance.us/api/v3/ticker/24hr?symbol={symbol}"
        )
        if resp.status_code == 200:
            data = resp.json()
            price = float(data.get("lastPrice") or 0)
            pct = float(data.get("priceChangePercent") or 0) / 100.0
            if price > 0:
                return {
                    "symbol": symbol,
                    "lastPrice": str(price),
                    "price24hPcnt": str(pct),
                }
    except Exception as e:
        logger.debug(f"Binance.US ticker error for {symbol}: {e}")
    return None


async def _fetch_from_gateio(client: httpx.AsyncClient, symbol: str) -> dict | None:
    try:
        pair = symbol.replace("USDT", "_USDT") if "USDT" in symbol else f"{symbol}_USDT"
        resp = await client.get(
            f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={pair}"
        )
        if resp.status_code == 200:
            items = resp.json()
            if items and isinstance(items, list):
                item = items[0]
                price = float(item.get("last") or 0)
                pct = float(item.get("change_percentage") or 0) / 100.0
                if price > 0:
                    return {
                        "symbol": symbol,
                        "lastPrice": str(price),
                        "price24hPcnt": str(pct),
                    }
    except Exception as e:
        logger.debug(f"Gate.io ticker error for {symbol}: {e}")
    return None


async def _fetch_from_coinbase(client: httpx.AsyncClient, symbol: str) -> dict | None:
    """Fallback to Coinbase Pro API (US-compliant, rock-solid for SOL, BTC, ETH on US cloud VMs)."""
    try:
        base = symbol.replace("USDT", "").replace("USDC", "").replace("USD", "")
        if not base:
            return None
        resp = await client.get(
            f"https://api.exchange.coinbase.com/products/{base}-USD/ticker"
        )
        if resp.status_code == 200:
            data = resp.json()
            price = float(data.get("price") or 0)
            if price > 0:
                return {
                    "symbol": symbol,
                    "lastPrice": str(price),
                    "price24hPcnt": None,
                }
    except Exception as e:
        logger.debug(f"Coinbase ticker error for {symbol}: {e}")
    return None


async def get_ticker(symbol: str):
    """Fetch ticker dict for a symbol (cached). Falls back to Binance.US/Gate.io/Coinbase if Bybit is geo-blocked."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return None
    now = time.monotonic()
    ttl = _ttl()
    entry = _cache.get(symbol)
    if entry and (now - entry[1]) < ttl:
        return entry[0]

    client = await _get_client()

    # 1. Try Bybit Linear
    ticker = await _fetch_from_bybit(client, symbol)

    # 2. Fallback to Binance.US (works on US servers)
    if not ticker:
        ticker = await _fetch_from_binance_us(client, symbol)

    # 3. Fallback to Gate.io (for coins not on Binance.US)
    if not ticker:
        ticker = await _fetch_from_gateio(client, symbol)

    # 4. Fallback to Coinbase (US-compliant, rock-solid for SOL, BTC, ETH)
    if not ticker:
        ticker = await _fetch_from_coinbase(client, symbol)

    if ticker:
        _prune_cache()
        _cache[symbol] = (ticker, now)
        return ticker

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


def cached_price(symbol: str, max_age_sec: float | None = None) -> float | None:
    """Return the price from cache without any network I/O if within TTL."""
    entry = _cache.get((symbol or "").upper())
    if not entry:
        return None
    age = time.monotonic() - entry[1]
    limit = max_age_sec if max_age_sec is not None else _ttl()
    if age <= limit:
        return ticker_price(entry[0])
    return None


async def get_current_price(symbol: str) -> float | None:
    """Fetch the current last price for a symbol (e.g. 'BTCUSDT').

    Returns None when the symbol is unknown or all requests fail.
    Results are cached for a few seconds.
    """
    return ticker_price(await get_ticker(symbol))


async def symbol_exists(symbol: str) -> bool:
    """Check whether a symbol is tradeable (cached)."""
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
    client = await _get_client()

    # 1. Try Bybit
    try:
        resp = await client.get(_BYBIT_TICKERS_URL, params={"category": "linear"})
        if resp.status_code == 200:
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
            if rows:
                rows.sort(key=lambda r: abs(r[2]), reverse=True)
                return rows[: max(1, min(limit, 25))]
    except Exception as e:
        logger.debug(f"Bybit top movers error: {e}")

    # 2. Fallback to Binance.US
    try:
        resp = await client.get("https://api.binance.us/api/v3/ticker/24hr")
        if resp.status_code == 200:
            items = resp.json()
            rows = []
            for item in items:
                try:
                    symbol = str(item.get("symbol") or "").upper()
                    if not symbol.endswith("USDT"):
                        continue
                    price = float(item.get("lastPrice") or 0)
                    pct = float(item.get("priceChangePercent") or 0) / 100.0
                except (TypeError, ValueError):
                    continue
                if price > 0:
                    rows.append((symbol, price, pct))
            if rows:
                rows.sort(key=lambda r: abs(r[2]), reverse=True)
                return rows[: max(1, min(limit, 25))]
    except Exception as e:
        logger.debug(f"Binance.US top movers error: {e}")

    return []
