# Crypto Telegram Alert Bot

A real-time cryptocurrency price alert bot for Telegram. It streams live prices
over a Bybit WebSocket and fires alerts the moment a target is hit.

## Features

- **Real-time WebSocket:** Bybit v5 public stream with auto-reconnect,
  batched resubscribe, stale-stream watchdog, and per-symbol locking so
  rapid ticks can't double-fire.
- **App-like Telegram UI:** Reply keyboard + inline buttons for add, remove,
  price checks (all coins at once), pause/resume, and a paginated alert list.
- **Alert types:** one-shot and repeating price alerts, percentage (`5%`),
  trailing (`trail`), time-window (`move`), and funding-rate alerts.
- **One-tap price board:** `💰 Check Price` / `Prices: All` shows your whole
  watchlist + active-alert coins in a single message — no more pressing one
  coin at a time. Customize with `/watch` or `WATCHLIST_SYMBOLS`.
- **Instant-fire guard:** `/add` rejects targets whose condition is already
  true, so alerts never fire-and-delete on creation.
- **Expiry, snooze, cooldown:** `/add ... 7d`, `/snooze 3 24h`,
  `cooldown=15m` per alert.
- **Daily briefing:** Configurable time/coins (`DAILY_BRIEFING_TIME`,
  `DAILY_BRIEFING_SYMBOLS`), fetched concurrently with a short TTL cache.
- **Pause / quiet hours:** One-tap mute (`/pause [15m|2h]`) and optional
  `QUIET_HOURS_UTC` window where alerts stay armed but silent.
- **Notifications:** ntfy.sh push with automatic Telegram fallback, a retry
  queue (`pending_notifications`) flushed hourly, optional webhook + auth,
  healthcheck pings, and per-type priority.
- **History & health:** `/history` logs every fired alert; `/status` and
  `/health` show connection, reconnects, queue depth, stream freshness.
- **Backups:** `/export` (JSON) and `/backup` (SQLite file) to a friendlier
  `/import`.
- **Fail-closed auth:** The bot answers *nobody* until `TELEGRAM_USER_ID` is set.

## Prerequisites

- Python 3.10+
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Your Telegram User ID (from [@userinfobot](https://t.me/userinfobot)) — required.

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/yourusername/crypto-alerts.git
   cd crypto-alerts
   ```

2. **Create a virtual environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up your environment variables:**
   Copy the example environment file and add your keys:
   ```bash
   cp .env.example .env
   nano .env
   ```
   *(Fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_USER_ID`. `NTFY_TOPIC` is
   optional — without it, alerts fall back to Telegram.)*

5. **Run the bot:**
   ```bash
   python main.py
   ```

## Commands

| Command | Example | Notes |
|---|---|---|
| `/add` | `/add BTC 72500 above [repeat] [7d] [cooldown=15m]` | price or `5%`, `above`/`below` |
| `/add` | `/add BTC,ETH 120000,5000 above` | ladder (1:1 coins/targets) |
| `/add` | `/add BTC trail 5% below` | fires on `5%` pullback from the peak |
| `/add` | `/add BTC move 3% 60m` | fires on `3%` move within 60 minutes |
| `/add` | `/add BTC funding 0.01% above` | fires on funding-rate threshold |
| `/list [coin]` | `/list SOL` | paginated; inline Edit/Snooze/Remove buttons |
| `/edit` | `/edit 3 76000 above` | retarget a price alert |
| `/snooze` | `/snooze 3 24h` | silence one alert (`/unsnooze 3` to undo) |
| `/remove` | `/remove 3` | removes one alert |
| `/removeall` | `/removeall` | asks for confirmation first |
| `/price [coins...]` | `/price` or `/price SOL AVAX` | no args = whole watchlist at once |
| `/movers [n]` | `/movers 10` | top 24h movers |
| `/history [n]` | `/history 5` | recently fired alerts |
| `/status` | `/status` | connection, tracked symbols, engine counters |
| `/health` | `/health` | reconnects, queue depth, stale streams |
| `/pause` / `/resume` | `/pause 15m` | global mute with optional duration |
| `/watch` / `/unwatch` | `/watch SUI` | customize the one-tap price board |
| `/watchlist` | `/watchlist` | show the price board |
| `/preset` | `/preset dip-buy` | one-tap alert bundles |
| `/export` / `/import` | `/export` | JSON backup / restore from a file |
| `/backup` | `/backup` | raw SQLite database file |
| `/help` | `/help` | full examples (also handles `/start`) |

## Notification options (`.env`)

`NTFY_TOPIC`, `NTFY_SERVER`, `NTFY_TOKEN`/`NTFY_USER`/`NTFY_PASSWORD`,
`SEND_TELEGRAM_ALERTS`, `ALERT_PRIORITY_ONESHOT`/`ALERT_PRIORITY_REPEAT`,
`WEBHOOK_URL` (JSON POST on trigger), `HEALTHCHECK_URL` (hourly ping).

## Tests

Stdlib only, no network access (Bybit calls are stubbed):

```bash
python -m unittest discover -s tests -v
```

## Project layout

- `main.py` — startup, config validation, graceful shutdown
- `telegram_bot.py` — commands, wizard, callbacks, `/status`
- `prices.py` — shared Bybit REST client + TTL cache (single price source)
- `binance_ws.py` — Bybit WebSocket client (name kept for compatibility)
- `alert_engine.py` — threshold checks, cooldowns, mute, per-symbol locks
- `notifier.py` — ntfy + Telegram delivery with fallback
- `database.py` — SQLite storage, validation, migrations, index on `symbol`
- `config.py` — env parsing with defensive defaults + `validate_config()`
- `tests/` — offline unit tests

## Easy GCP Deployment

This project includes a `setup-gcp.sh` script specifically designed for the
**Google Cloud Always Free Tier** (e2-micro instance running Ubuntu).

1. Upload the project to your GCP instance.
2. Run `bash setup-gcp.sh`.
3. The script will automatically install Python, create the virtual environment, install packages, prompt you for your API keys, and configure a `systemd` background service so the bot runs 24/7.

## Technologies Used

- `python-telegram-bot` (Telegram UI and commands)
- `websockets` (Bybit v5 live data stream)
- `aiosqlite` (Persistent alert storage with schema migrations)
- `httpx` (Async REST API calls)
