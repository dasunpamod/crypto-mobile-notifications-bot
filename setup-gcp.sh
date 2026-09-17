#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
# Crypto Price Alert Bot — One-Click GCP Setup Script
# Run this on your Google Cloud e2-micro VM
# ─────────────────────────────────────────────────────────────────────

set -e

echo "🚀 Setting up Crypto Alert Bot..."

# Install system dependencies
echo "📦 Installing system packages..."
sudo apt update -qq
sudo apt install -y -qq python3-pip python3-venv git > /dev/null 2>&1

# Create project directory
PROJECT_DIR="$HOME/crypto-alerts"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

# Create virtual environment
echo "🐍 Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
echo "Installing Python packages..."
pip install --quiet -r requirements.txt

# Prompt for configuration
echo ""
echo "─────────────────────────────────────────"
echo "  Configuration"
echo "─────────────────────────────────────────"
echo ""
read -p "Telegram Bot Token: " BOT_TOKEN
read -p "Telegram User ID: " USER_ID
read -p "ntfy Topic Name: " NTFY_TOPIC

# Create .env file
cat > .env << EOF
TELEGRAM_BOT_TOKEN=$BOT_TOKEN
TELEGRAM_USER_ID=$USER_ID
NTFY_TOPIC=$NTFY_TOPIC
NTFY_SERVER=https://ntfy.sh
SEND_TELEGRAM_ALERTS=false
EOF
chmod 600 .env

echo "✅ Configuration saved to .env"

# Create systemd service (runs on boot, auto-restarts)
echo "⚙️  Creating systemd service..."
sudo tee /etc/systemd/system/crypto-alerts.service > /dev/null << EOF
[Unit]
Description=Crypto Price Alert Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/venv/bin/python main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

echo ""
echo "─────────────────────────────────────────"
echo "  ✅ Setup Complete!"
echo "─────────────────────────────────────────"
echo ""
echo "  Now upload your Python files to: $PROJECT_DIR"
echo "  (config.py, database.py, prices.py, binance_ws.py, alert_engine.py,"
echo "   telegram_bot.py, notifier.py, charts.py, webhook_server.py, main.py)"
echo ""
echo "  Then start the bot with:"
echo "    sudo systemctl enable crypto-alerts"
echo "    sudo systemctl start crypto-alerts"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status crypto-alerts   # Check status"
echo "    sudo journalctl -u crypto-alerts -f   # View live logs"
echo "    sudo systemctl restart crypto-alerts  # Restart"
echo ""
