FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (explicit list keeps secrets/DB out of the image)
COPY config.py database.py alert_engine.py binance_ws.py notifier.py prices.py telegram_bot.py main.py ./

# Run the alert system
CMD ["python", "main.py"]
