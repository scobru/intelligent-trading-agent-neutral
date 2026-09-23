#!/bin/bash
set -u

echo "========================================================"
echo "Starting Neutral Agent (Base delta-neutral) on Docker"
echo "========================================================"

# Il funding cambia lentamente: un ciclo ogni 30 minuti basta
INTERVAL="${TRADING_INTERVAL:-1800}"

# 1. Microservizio SynFutures: serve sempre, anche in paper, perche' il
#    funding si legge da li'. Senza SYNFUTURES_PRIVATE_KEY non firma nulla.
echo "[1/3] Starting SynFutures microservice on port 3100..."
(
    cd /app/synfutures-service
    export SYNFUTURES_PORT=3100
    export SYNFUTURES_PRIVATE_KEY="${SYNFUTURES_PRIVATE_KEY:-${PRIVATE_KEY:-}}"
    if [ "${PAPER_TRADING:-false}" = "true" ] || [ "${DRY_RUN:-true}" = "true" ]; then
        # in paper/dry-run il servizio resta in sola lettura
        export SYNFUTURES_PRIVATE_KEY=""
    fi
    if [ -d dist ]; then node dist/index.js; else npx ts-node src/index.ts; fi
) &

for i in $(seq 1 30); do
    curl -s http://localhost:3100/health > /dev/null 2>&1 && break
    sleep 1
done

echo "[2/3] Starting Web Dashboard on port ${PORT:-3000}..."
python dashboard.py &

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "📱 Starting Telegram Bot listener..."
    python telegram_bot.py &
fi

echo "[3/3] Starting neutral loop (interval: ${INTERVAL}s)..."
if [ "${PAPER_TRADING:-false}" = "true" ]; then
    echo "📝 PAPER attivo: portafoglio virtuale."
elif [ "${DRY_RUN:-true}" = "true" ]; then
    echo "🧪 DRY-RUN attivo: nessuna transazione verrà firmata."
fi
echo ""

while true; do
    echo "⏰ [$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running neutral cycle..."
    python main.py
    echo "💤 Sleeping for ${INTERVAL} seconds until next cycle..."
    sleep "${INTERVAL}"
done
