FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# PAPER is the default and the safe fallback. LIVE needs MODE=LIVE, live_enabled: true
# in bots/config.yaml, and $BOTS_STATE_DIR/LIVE_CONFIRMED.txt.
ENV MODE=PAPER \
    BOTS_STATE_DIR=/data \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "bots.main", "run"]
