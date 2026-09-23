FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HAFAS_RADAR_SCRIPT=/app/microservice/hafas_radar.js \
    NODE_BINARY=node

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY microservice/package.json microservice/package-lock.json /app/microservice/
RUN cd /app/microservice && npm ci --omit=dev --no-audit --no-fund

COPY backend /app/backend
COPY microservice/*.js /app/microservice/
COPY data/borders_cache.geojson /app/data/borders_cache.geojson

WORKDIR /app/backend
EXPOSE 8000

CMD ["python", "main.py"]
