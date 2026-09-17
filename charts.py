"""Chart generation via QuickChart and market sentiment indicators.

Provides:
- generate_chart_image(symbol, interval, limit): generates a sleek dark-mode
  candlestick/line chart using Bybit/Binance kline data, rendered via QuickChart
  with 0 MB RAM overhead on low-memory VMs.
- get_fear_and_greed(): fetches the current Crypto Fear & Greed Index.
"""

import datetime
import logging
import httpx

import config

logger = logging.getLogger(__name__)

_BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
_BINANCE_US_KLINE_URL = "https://api.binance.us/api/v3/klines"
_FNG_URL = "https://api.alternative.me/fng/?limit=1"
_QUICKCHART_URL = "https://quickchart.io/chart"

# Interval mapping
_BYBIT_INTERVAL_MAP = {
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "1d": "D",
    "1w": "W",
}

_BINANCE_INTERVAL_MAP = {
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "1d": "1d",
    "1w": "1w",
}


async def get_fear_and_greed() -> dict | None:
    """Fetch the latest Crypto Fear & Greed Index.

    Returns e.g. {"value": 72, "classification": "Greed"} or None on error.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_FNG_URL)
            if resp.status_code == 200:
                data = resp.json().get("data")
                if data and isinstance(data, list):
                    item = data[0]
                    return {
                        "value": int(item.get("value", 50)),
                        "classification": str(item.get("value_classification", "Neutral")),
                    }
    except Exception as e:
        logger.debug(f"Failed to fetch Fear & Greed index: {e}")
    return None


async def fetch_klines(symbol: str, interval: str = "1h", limit: int = 35) -> list[dict] | None:
    """Fetch historical kline candle data from Bybit linear or Binance.US fallback.

    Returns list of dicts sorted chronologically:
    [{"time": "14:00", "close": 97.4, "high": 98.0, "low": 96.5, "open": 96.8}, ...]
    """
    symbol = symbol.strip().upper()
    interval = interval.lower()
    if interval not in _BYBIT_INTERVAL_MAP:
        interval = "1h"

    limit = max(10, min(limit, 100))

    candles: list[dict] = []

    # 1. Try Bybit Linear
    try:
        bybit_int = _BYBIT_INTERVAL_MAP.get(interval, "60")
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(
                _BYBIT_KLINE_URL,
                params={"category": "linear", "symbol": symbol, "interval": bybit_int, "limit": limit}
            )
            if resp.status_code == 200:
                raw_list = (resp.json().get("result") or {}).get("list") or []
                # Bybit returns most-recent first; reverse to chronological order
                for row in reversed(raw_list):
                    try:
                        ts_raw = int(row[0])
                        ts = ts_raw / 1000.0
                        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
                        time_str = dt.strftime("%d %b" if interval in ("1d", "1w") else "%H:%M")
                        candles.append({
                            "time": time_str,
                            "ts": ts_raw,
                            "open": float(row[1]),
                            "high": float(row[2]),
                            "low": float(row[3]),
                            "close": float(row[4]),
                        })
                    except (IndexError, ValueError, TypeError):
                        continue
                if candles:
                    return candles
    except Exception as e:
        logger.debug(f"Bybit klines failed for {symbol}: {e}")

    # 2. Fallback to Binance.US
    try:
        binance_int = _BINANCE_INTERVAL_MAP.get(interval, "1h")
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(
                _BINANCE_US_KLINE_URL,
                params={"symbol": symbol, "interval": binance_int, "limit": limit}
            )
            if resp.status_code == 200:
                raw_list = resp.json()
                if isinstance(raw_list, list):
                    for row in raw_list:
                        try:
                            ts_raw = int(row[0])
                            ts = ts_raw / 1000.0
                            dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
                            time_str = dt.strftime("%d %b" if interval in ("1d", "1w") else "%H:%M")
                            candles.append({
                                "time": time_str,
                                "ts": ts_raw,
                                "open": float(row[1]),
                                "high": float(row[2]),
                                "low": float(row[3]),
                                "close": float(row[4]),
                            })
                        except (IndexError, ValueError, TypeError):
                            continue
                    if candles:
                        return candles
    except Exception as e:
        logger.debug(f"Binance.US klines failed for {symbol}: {e}")

    return None


async def generate_chart_image(symbol: str, interval: str = "1h", limit: int = 32, chart_type: str = "candle") -> bytes | None:
    """Generate a sleek TradingView-styled chart image (PNG bytes).

    Supports chart_type="candle" (default Japanese candlesticks) and chart_type="line" (sleek gradient area chart).
    """
    candles = await fetch_klines(symbol, interval, limit=limit)
    if not candles:
        return None

    close_prices = [c["close"] for c in candles]
    latest_price = close_prices[-1]
    first_price = close_prices[0]
    pct_change = ((latest_price - first_price) / first_price * 100) if first_price > 0 else 0.0
    period_high = max(c["high"] for c in candles)
    period_low = min(c["low"] for c in candles)
    is_bullish = latest_price >= first_price

    from prices import format_price
    coin = symbol.replace("USDT", "")
    title = f"{coin} ({interval.upper()}) • {format_price(latest_price)} ({pct_change:+.2f}%)   H: {format_price(period_high)}  L: {format_price(period_low)}"

    time_unit = "day" if interval in ("1d", "1w") else "hour"
    display_format = "dd MMM" if interval in ("1d", "1w") else "HH:mm"

    if chart_type == "line":
        line_color = "#00E676" if is_bullish else "#FF5252"
        fill_color = "rgba(0, 230, 118, 0.12)" if is_bullish else "rgba(255, 82, 82, 0.12)"
        chart_config = {
            "type": "line",
            "data": {
                "labels": [c["time"] for c in candles],
                "datasets": [{
                    "label": "Price",
                    "data": close_prices,
                    "borderColor": line_color,
                    "borderWidth": 2.5,
                    "fill": True,
                    "backgroundColor": fill_color,
                    "pointRadius": 0,
                    "tension": 0.35
                }]
            },
            "options": {
                "plugins": {
                    "legend": {"display": False},
                    "title": {
                        "display": True,
                        "text": title,
                        "color": "#FFFFFF",
                        "font": {"size": 14, "weight": "bold"},
                        "padding": {"top": 10, "bottom": 15}
                    }
                },
                "scales": {
                    "x": {
                        "grid": {"color": "rgba(255, 255, 255, 0.05)"},
                        "ticks": {"color": "#9aa0a6", "maxTicksLimit": 7, "maxRotation": 0}
                    },
                    "y": {
                        "grid": {"color": "rgba(255, 255, 255, 0.05)"},
                        "ticks": {"color": "#9aa0a6"}
                    }
                }
            }
        }
    else:
        # Candlestick (TradingView style)
        chart_config = {
            "type": "candlestick",
            "data": {
                "datasets": [{
                    "label": symbol,
                    "data": [{"x": c.get("ts", 0), "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]} for c in candles],
                    "color": {
                        "up": "#26A69A",
                        "down": "#EF5350",
                        "unchanged": "#888888",
                    }
                }]
            },
            "options": {
                "plugins": {
                    "legend": {"display": False},
                    "title": {
                        "display": True,
                        "text": title,
                        "color": "#FFFFFF",
                        "font": {"size": 14, "weight": "bold"},
                        "padding": {"top": 10, "bottom": 15}
                    }
                },
                "scales": {
                    "x": {
                        "type": "timeseries",
                        "time": {
                            "unit": time_unit,
                            "displayFormats": {time_unit: display_format}
                        },
                        "grid": {"color": "rgba(255, 255, 255, 0.05)"},
                        "ticks": {"color": "#9aa0a6", "maxTicksLimit": 7}
                    },
                    "y": {
                        "grid": {"color": "rgba(255, 255, 255, 0.05)"},
                        "ticks": {"color": "#9aa0a6"}
                    }
                }
            }
        }

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                _QUICKCHART_URL,
                json={
                    "version": "3",
                    "chart": chart_config,
                    "width": 720,
                    "height": 400,
                    "backgroundColor": "#131722",
                    "devicePixelRatio": 2.0
                }
            )
            if resp.status_code == 200 and resp.content:
                return resp.content
            # Fallback to line chart if candlestick rendering failed
            if chart_type == "candle":
                return await generate_chart_image(symbol, interval=interval, limit=limit, chart_type="line")
    except Exception as e:
        logger.warning(f"QuickChart generation error for {symbol}: {e}")
        if chart_type == "candle":
            try:
                return await generate_chart_image(symbol, interval=interval, limit=limit, chart_type="line")
            except Exception:
                pass

    return None
