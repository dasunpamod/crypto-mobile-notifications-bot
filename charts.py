"""Multi-engine crypto charting and market sentiment indicators.

Provides:
- Option 1 (Live WebApp): get_tradingview_embed_url(), get_tradingview_web_url()
- Option 2 (Technical Analysis): generate_mplfinance_image() with Volume, EMA 20/50 & RSI 14
- Option 3 (TradingView Snapshots): generate_chartimg_image() via chart-img.com API
- Lightweight Cloud Fallback: generate_quickchart_image() with 0 MB RAM overhead
- Dispatcher: generate_chart_image() routing across engines with automatic fallback
- Market Sentiment: get_fear_and_greed()
"""

import datetime
import gc
import io
import logging
import httpx

import config

logger = logging.getLogger(__name__)

_BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
_BINANCE_US_KLINE_URL = "https://api.binance.us/api/v3/klines"
_FNG_URL = "https://api.alternative.me/fng/?limit=1"
_QUICKCHART_URL = "https://quickchart.io/chart"
_CHART_IMG_URL = "https://api.chart-img.com/v2/tradingview/advanced-chart"

# Interval mappings
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

_TV_INTERVAL_MAP = {
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "1d": "D",
    "1w": "W",
}

_CHARTIMG_INTERVAL_MAP = {
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "1d": "1D",
    "1w": "1W",
}


def get_tradingview_embed_url(symbol: str, interval: str = "1h") -> str:
    """Return an official TradingView widget embed URL suitable for Telegram WebApp."""
    sym = symbol.strip().upper()
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    tv_int = _TV_INTERVAL_MAP.get(interval.lower(), "60")
    return (
        f"https://s.tradingview.com/widgetembed/?symbol=BINANCE%3A{sym}"
        f"&interval={tv_int}&theme=dark&style=1"
    )


def get_tradingview_web_url(symbol: str) -> str:
    """Return a direct link to open the pair on TradingView."""
    sym = symbol.strip().upper()
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{sym}"


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
    [{"time": "14:00", "close": 97.4, "high": 98.0, "low": 96.5, "open": 96.8, "volume": 120.5}, ...]
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
                        vol = float(row[5]) if len(row) > 5 else 0.0
                        candles.append({
                            "time": time_str,
                            "ts": ts_raw,
                            "open": float(row[1]),
                            "high": float(row[2]),
                            "low": float(row[3]),
                            "close": float(row[4]),
                            "volume": vol,
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
                            vol = float(row[5]) if len(row) > 5 else 0.0
                            candles.append({
                                "time": time_str,
                                "ts": ts_raw,
                                "open": float(row[1]),
                                "high": float(row[2]),
                                "low": float(row[3]),
                                "close": float(row[4]),
                                "volume": vol,
                            })
                        except (IndexError, ValueError, TypeError):
                            continue
                    if candles:
                        return candles
    except Exception as e:
        logger.debug(f"Binance.US klines failed for {symbol}: {e}")

    return None


# ---------------------------------------------------------------------------
# Engine 1: Chart-Img (Option 3 - Authentic TradingView Screenshots)
# ---------------------------------------------------------------------------

async def generate_chartimg_image(symbol: str, interval: str = "1h") -> bytes | None:
    """Generate a pixel-perfect TradingView chart screenshot via chart-img.com API."""
    api_key = getattr(config, "CHART_IMG_API_KEY", "")
    if not api_key:
        logger.debug("CHART_IMG_API_KEY not configured.")
        return None

    symbol = symbol.strip().upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"

    tv_int = _CHARTIMG_INTERVAL_MAP.get(interval.lower(), "1h")
    payload = {
        "symbol": f"BINANCE:{symbol}",
        "interval": tv_int,
        "theme": "dark",
        "studies": [
            {"name": "Moving Average Exponential", "inputs": {"length": 20}},
            {"name": "Moving Average Exponential", "inputs": {"length": 50}},
            {"name": "Relative Strength Index", "forceOverlay": False},
        ],
    }

    try:
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=14.0) as client:
            resp = await client.post(_CHART_IMG_URL, json=payload, headers=headers)
            if resp.status_code == 200 and resp.content and resp.headers.get("content-type", "").startswith("image/"):
                return resp.content
            logger.warning(f"chart-img.com returned HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"chart-img.com request failed for {symbol}: {e}")

    return None


# ---------------------------------------------------------------------------
# Engine 2: mplfinance (Option 2 - Institutional TA with Volume, EMA, RSI)
# ---------------------------------------------------------------------------

async def generate_mplfinance_image(symbol: str, interval: str = "1h", limit: int = 38) -> bytes | None:
    """Generate a rich dark-mode technical analysis chart using mplfinance."""
    candles = await fetch_klines(symbol, interval, limit=limit)
    if not candles:
        return None

    try:
        import pandas as pd
        import mplfinance as mpf
        import matplotlib.pyplot as plt

        dates = [
            datetime.datetime.fromtimestamp(c["ts"] / 1000.0, tz=datetime.timezone.utc)
            for c in candles
        ]
        df = pd.DataFrame(
            {
                "Open": [c["open"] for c in candles],
                "High": [c["high"] for c in candles],
                "Low": [c["low"] for c in candles],
                "Close": [c["close"] for c in candles],
                "Volume": [c.get("volume", 0.0) for c in candles],
            },
            index=dates,
        )

        apds = []
        # EMA 20 overlay
        if len(df) >= 5:
            ema20 = df["Close"].ewm(span=min(20, len(df)), adjust=False).mean()
            apds.append(mpf.make_addplot(ema20, color="#2962FF", width=1.3))

        # RSI(14) subplot
        if len(df) >= 14:
            delta = df["Close"].diff()
            gain = delta.where(delta > 0, 0.0)
            loss = -delta.where(delta < 0, 0.0)
            avg_gain = gain.rolling(window=14, min_periods=14).mean()
            avg_loss = loss.rolling(window=14, min_periods=14).mean()
            rs = avg_gain / avg_loss.replace(0, 1e-9)
            rsi = 100.0 - (100.0 / (1.0 + rs))
            apds.append(mpf.make_addplot(rsi, panel=2, color="#AB47BC", width=1.3, ylabel="RSI(14)"))

        coin = symbol.replace("USDT", "")
        latest_price = df["Close"].iloc[-1]
        from prices import format_price
        title = f"{coin} ({interval.upper()}) • {format_price(latest_price)} • EMA 20 + RSI"

        mc = mpf.make_marketcolors(
            up="#26A69A",
            down="#EF5350",
            edge="inherit",
            wick="inherit",
            volume="inherit",
        )
        s = mpf.make_mpf_style(
            base_mpf_style="nightclouds",
            marketcolors=mc,
            figcolor="#131722",
            facecolor="#131722",
            gridcolor="#1e222d",
        )

        buf = io.BytesIO()
        has_volume = bool(df["Volume"].sum() > 0)
        mpf.plot(
            df,
            type="candle",
            style=s,
            volume=has_volume,
            addplot=apds if apds else None,
            title=title,
            returnfig=False,
            savefig=dict(fname=buf, dpi=125, bbox_inches="tight", format="png"),
        )
        plt.close("all")
        gc.collect()

        return buf.getvalue()
    except Exception as e:
        logger.warning(f"mplfinance generation failed for {symbol}: {e}")
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
            gc.collect()
        except Exception:
            pass

    return None


# ---------------------------------------------------------------------------
# Engine 3: QuickChart v3 (Cloud Fallback - 0 MB RAM)
# ---------------------------------------------------------------------------

async def generate_quickchart_image(
    symbol: str, interval: str = "1h", limit: int = 32, chart_type: str = "candle"
) -> bytes | None:
    """Generate a sleek TradingView-styled chart image via QuickChart cloud."""
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
                    "tension": 0.35,
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
                        "padding": {"top": 10, "bottom": 15},
                    }
                },
                "scales": {
                    "x": {
                        "grid": {"color": "rgba(255, 255, 255, 0.05)"},
                        "ticks": {"color": "#9aa0a6", "maxTicksLimit": 7, "maxRotation": 0},
                    },
                    "y": {
                        "grid": {"color": "rgba(255, 255, 255, 0.05)"},
                        "ticks": {"color": "#9aa0a6"},
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
                    "data": [
                        {"x": c.get("ts", 0), "o": c["open"], "h": c["high"], "l": c["low"], "c": c["close"]}
                        for c in candles
                    ],
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
                        "padding": {"top": 10, "bottom": 15},
                    }
                },
                "scales": {
                    "x": {
                        "type": "timeseries",
                        "time": {
                            "unit": time_unit,
                            "displayFormats": {time_unit: display_format},
                        },
                        "grid": {"color": "rgba(255, 255, 255, 0.05)"},
                        "ticks": {"color": "#9aa0a6", "maxTicksLimit": 7},
                    },
                    "y": {
                        "grid": {"color": "rgba(255, 255, 255, 0.05)"},
                        "ticks": {"color": "#9aa0a6"},
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
                    "devicePixelRatio": 2.0,
                }
            )
            if resp.status_code == 200 and resp.content:
                return resp.content
            if chart_type == "candle":
                return await generate_quickchart_image(symbol, interval=interval, limit=limit, chart_type="line")
    except Exception as e:
        logger.warning(f"QuickChart generation error for {symbol}: {e}")
        if chart_type == "candle":
            try:
                return await generate_quickchart_image(symbol, interval=interval, limit=limit, chart_type="line")
            except Exception:
                pass

    return None


# ---------------------------------------------------------------------------
# Master Dispatcher with Automatic Fallbacks
# ---------------------------------------------------------------------------

async def generate_chart_image(
    symbol: str, interval: str = "1h", limit: int = 35, chart_type: str | None = None
) -> bytes | None:
    """Generate a chart image using the requested style/engine with automatic fallback.

    Supported chart_type:
    - "tv" / "chartimg": Option 3 Authentic TradingView snapshot with EMA & RSI
    - "ta" / "mpl" / "tech": Option 2 Institutional Technical Analysis via mplfinance
    - "candle" / "quick": Option 2/QuickChart clean candlestick
    - "line" / "area": Option 2/QuickChart sleek glowing gradient line
    """
    mode = (chart_type or getattr(config, "DEFAULT_CHART_ENGINE", "chartimg")).lower()

    # 1. TradingView Snapshot
    if mode in ("tv", "chartimg", "snap", "tradingview"):
        img = await generate_chartimg_image(symbol, interval=interval)
        if img:
            return img
        # Fallback to mplfinance, then quickchart
        img = await generate_mplfinance_image(symbol, interval=interval, limit=limit)
        if img:
            return img
        return await generate_quickchart_image(symbol, interval=interval, limit=limit, chart_type="candle")

    # 2. Institutional Technical Analysis (mplfinance)
    if mode in ("ta", "mpl", "tech", "technical"):
        img = await generate_mplfinance_image(symbol, interval=interval, limit=limit)
        if img:
            return img
        # Fallback to chartimg, then quickchart
        img = await generate_chartimg_image(symbol, interval=interval)
        if img:
            return img
        return await generate_quickchart_image(symbol, interval=interval, limit=limit, chart_type="candle")

    # 3. Line / Area
    if mode in ("line", "area"):
        return await generate_quickchart_image(symbol, interval=interval, limit=limit, chart_type="line")

    # 4. Clean Candlestick
    if mode in ("candle", "candles", "candlestick", "quick"):
        return await generate_quickchart_image(symbol, interval=interval, limit=limit, chart_type="candle")

    # Default fallback chain
    img = await generate_chartimg_image(symbol, interval=interval)
    if img:
        return img
    img = await generate_mplfinance_image(symbol, interval=interval, limit=limit)
    if img:
        return img
    return await generate_quickchart_image(symbol, interval=interval, limit=limit, chart_type="candle")

