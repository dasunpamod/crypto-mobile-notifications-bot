# Crypto Mobile Notifications & Telegram Alert Bot

A high-speed, real-time cryptocurrency alert system that delivers **instant, loud mobile push notifications directly to your phone via [ntfy](https://ntfy.sh)**, managed seamlessly through an interactive **Telegram Bot**. Powered by Bybit v5 WebSockets for sub-second price reaction.

---

## 🚨 Why Mobile Notifications via ntfy?

Traditional Telegram messages often get lost in noisy chat lists, muted channels, or delayed by battery-saving background restrictions. This bot solves that by combining **ntfy mobile push** with **Telegram controls**:

- 🔊 **Loud Phone Alarms & Siren Sounds:** Trigger distinct, high-urgency ringtones on your mobile device for critical breakout or liquidation targets.
- 📱 **Lock-Screen Banners:** Immediate push notifications delivered to Android and iOS via the free, open-source **ntfy** app.
- 🌙 **Bypass Silent / Do Not Disturb:** Configure high-priority alerts (`max` or `high`) to ring through DND on your phone for emergency market moves.
- 🔒 **Zero Account / Sign-up Required:** Simply pick a private topic name and subscribe in the ntfy app — no email or phone number needed.
- 🛡️ **Dual-Channel Reliability & Fallback:** If ntfy push fails or is unreachable, the system automatically falls back to Telegram direct messages and queues retries.
- 🌐 **Self-Hostable or Free Cloud:** Works out-of-the-box with the public `https://ntfy.sh` server or your own private self-hosted ntfy instance.

---

## 📲 Quick Setup: Mobile Push Notifications (ntfy)

Setting up loud phone notifications takes less than 60 seconds:

1. **Install the ntfy app:**
   - **Android:** [Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy) or [F-Droid](https://f-droid.org/packages/io.heckel.ntfy/)
   - **iOS:** [Apple App Store](https://apps.apple.com/app/ntfy/id1625396347)
   - **Desktop / Web:** Works directly in any browser at [ntfy.sh](https://ntfy.sh)

2. **Subscribe to a private topic:**
   - Open the ntfy app and tap **+** (Subscribe to topic).
   - Enter a unique, hard-to-guess topic name (e.g. `trader_crypto_alerts_987x`).

3. **Configure loud sounds (Optional but Recommended):**
   - In the ntfy app settings for your topic, enable **Override Do Not Disturb** and set a loud ringtone (e.g. Siren / Alarm) so critical price moves wake you up.

4. **Add the topic to your `.env`:**
   ```bash
   NTFY_TOPIC=trader_crypto_alerts_987x
   NTFY_SERVER=https://ntfy.sh
   ALERT_PRIORITY_ONESHOT=high
   ALERT_PRIORITY_REPEAT=default
   ```

---

## ✨ Key Features

- **Sub-second WebSocket Stream:** Direct Bybit v5 public WebSocket feed with auto-reconnect, batched resubscription, stale-stream watchdog, and per-symbol locking to prevent double-firing.
- **Mobile Push (ntfy):** High/max priority alerts, custom sound tags, retry queues (`pending_notifications`), and webhook integrations.
- **App-Like Telegram UI:** Reply keyboard + inline interactive buttons for instant add, remove, pause/resume, and paginated alert listings.
- **Diverse Alert Types:**
  - **Price Targets:** Fixed price thresholds (`above` / `below`).
  - **Percentage Moves:** Relative percent alerts (`BTC 5% above`).
  - **Trailing Alerts:** Trailing stop / pullback alerts (`BTC trail 5% below` tracking from peak).
  - **Time-Window Velocity:** Rapid price pump/dump detection (`BTC move 3% 60m`).
  - **Funding Rates:** Perpetual contract funding rate thresholds (`BTC funding 0.01% above`).
- **One-Tap Price Board:** `💰 Check Price` shows your entire watchlist and active-alert coins in one clean message.
- **Instant-Fire Protection:** Rejects targets whose conditions are already met on creation so alerts never trigger-and-delete instantly.
- **Expiry, Snooze & Cooldowns:** Self-expiring alerts (`7d`), per-alert cooldowns (`cooldown=15m`), and temporary mutes (`/snooze 3 24h`).
- **Daily Market Briefing:** Scheduled daily recap (`DAILY_BRIEFING_TIME`) covering selected coins with 24h performance stats.
- **Quiet Hours & Global Pause:** One-tap silence button (`/pause 1h`) and UTC quiet-hours windows where alerts arm silently without ringing your phone.
- **Fail-Closed Security:** Rejects all unauthorized users — the bot will only respond to your verified `TELEGRAM_USER_ID`.

---

## 🛠️ Installation & Setup

### Prerequisites

- Python 3.10+
- Telegram Bot Token (from [@BotFather](https://t.me/BotFather))
- Your Telegram User ID (from [@userinfobot](https://t.me/userinfobot))
- ntfy app installed on your phone (free from iOS App Store or Google Play)

### 1. Clone the repository
```bash
git clone https://github.com/dasunpamod/crypto-mobile-notifications-bot.git
cd crypto-mobile-notifications-bot
```

### 2. Create virtual environment
```bash
python3 -m venv venv
# On Linux/macOS:
source venv/bin/activate
# On Windows:
.\venv\Scripts\activate
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure environment variables
Copy the template and fill in your keys:
```bash
cp .env.example .env
```

Edit `.env`:
```env
# Required Telegram credentials
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_USER_ID=your_telegram_user_id_here

# Mobile Push Notifications (ntfy)
NTFY_TOPIC=your_private_topic_name_here
NTFY_SERVER=https://ntfy.sh
SEND_TELEGRAM_ALERTS=true

# Notification Priorities
ALERT_PRIORITY_ONESHOT=high
ALERT_PRIORITY_REPEAT=default
```

### 5. Run the bot
```bash
python main.py
```

---

## 💬 Telegram Commands & Controls

| Command | Example | Description |
|---|---|---|
| `/add` | `/add BTC 72500 above [repeat] [7d] [cooldown=15m]` | Fixed target or percentage alert |
| `/add` | `/add BTC,ETH 120000,5000 above` | Multi-coin ladder |
| `/add` | `/add BTC trail 5% below` | Fires on 5% pullback from peak |
| `/add` | `/add BTC move 3% 60m` | Fires on 3% move within 60 minutes |
| `/add` | `/add BTC funding 0.01% above` | Fires on funding rate threshold |
| `/list [coin]` | `/list SOL` | Paginated alert list with inline Edit/Snooze/Delete |
| `/edit` | `/edit 3 76000 above` | Update existing target |
| `/snooze` | `/snooze 3 24h` | Silence an alert (`/unsnooze 3` to re-enable) |
| `/remove` | `/remove 3` | Remove single alert |
| `/removeall` | `/removeall` | Delete all alerts (with confirmation prompt) |
| `/price [coins]` | `/price` or `/price SOL AVAX` | Full watchlist price board |
| `/movers [n]` | `/movers 10` | Top 24h market gainers & losers |
| `/history [n]` | `/history 5` | Log of recently triggered alerts |
| `/watch` / `/unwatch` | `/watch SUI` | Customize one-tap price board coins |
| `/pause` / `/resume` | `/pause 1h` | Global mute with optional duration |
| `/status` / `/health` | `/status` | Connection health, queue depth, watchdog stats |
| `/export` / `/backup` | `/export` | Export alerts as JSON or raw SQLite database |
| `/help` | `/help` | Full interactive guide and example syntax |

---

## 🔔 Notification Architecture & Options

Configure your alert delivery in `.env`:

```env
# Mobile push server (public or self-hosted)
NTFY_SERVER=https://ntfy.sh
NTFY_TOPIC=your_private_topic

# Optional authentication for private ntfy servers
#NTFY_TOKEN=
#NTFY_USER=
#NTFY_PASSWORD=

# Push priorities: max | high | default | low | min
ALERT_PRIORITY_ONESHOT=high
ALERT_PRIORITY_REPEAT=default

# Dual-delivery: also send message directly inside Telegram chat
SEND_TELEGRAM_ALERTS=true

# Outgoing webhook for external home automation / Discord
#WEBHOOK_URL=https://hooks.example.com/alert

# Heartbeat monitor (e.g. Uptime Kuma, healthchecks.io)
#HEALTHCHECK_URL=https://hc-ping.com/your-uuid
```

---

## 🧪 Running Tests

Tests run offline with stdlib `unittest` (no network required):

```bash
python -m unittest discover -s tests -v
```

---

## 🚀 Easy 24/7 Cloud Deployment (GCP Always Free)

Deploy directly on a **Google Cloud Always Free Tier (e2-micro)** Ubuntu VM:

1. Upload the project to your VM.
2. Run the automated installer:
   ```bash
   bash setup-gcp.sh
   ```
3. The script configures Python, installs packages, prompts for configuration, and sets up a resilient `systemd` background service that auto-restarts on reboot.

---

## 📦 Project Structure

- `main.py` — Orchestrator, health monitor, and graceful shutdown.
- `notifier.py` — High-priority ntfy mobile push engine with Telegram fallback and retry queue.
- `telegram_bot.py` — Rich Telegram UI, keyboard controls, command handlers, and wizards.
- `alert_engine.py` — Multi-type condition evaluation, trailing stop tracking, velocity windows.
- `binance_ws.py` — Low-latency Bybit v5 WebSocket client with watchdog and auto-resubscription.
- `prices.py` — Shared Bybit REST client with TTL cache for instant quotes.
- `database.py` — Async SQLite storage, migrations, and index optimization.
- `config.py` — Defensive environment variable validation.
- `tests/` — Offline test suite for bot logic, parsing, and database operations.
