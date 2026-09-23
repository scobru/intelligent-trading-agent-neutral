FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=3000 \
    SYNFUTURES_PORT=3100 \
    SYNFUTURES_SERVICE_URL=http://localhost:3100

# Python per l'agente, Node 20 per il microservizio SynFutures (Oyster SDK)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    sqlite3 \
    dos2unix \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# 1. Microservizio SynFutures
WORKDIR /app/synfutures-service
COPY synfutures-service/package*.json synfutures-service/tsconfig.json ./
RUN npm install --include=dev
COPY synfutures-service/src ./src
RUN npm run build

# 2. Dipendenze Python
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 3. Codice
COPY . .
RUN dos2unix ./start.sh && chmod +x ./start.sh

# Stato persistente (SQLite, registro coppie, portafoglio paper)
RUN mkdir -p /app/data

EXPOSE 3000

CMD ["/bin/bash", "./start.sh"]
