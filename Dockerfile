# ============================================================
# AI Multi-Chain Sniper - production image for Railway / Docker
# ============================================================
FROM python:3.11-slim

# Faster, leaner Python
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app

# System deps - build-essential needed for some Python wheels (orjson,
# cryptography on older arches). curl for healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps first (layer cached when only code changes)
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App code
COPY . .

# Persistent volume mount target
RUN mkdir -p /data/logs /data/saved_models

# Drop privileges
RUN useradd --create-home --shell /bin/bash sniper && \
    chown -R sniper:sniper /app /data
USER sniper

# Default to PAPER mode. Override via Railway env vars.
ENV MODE=PAPER \
    ENABLE_REAL_TRADING=false

# Railway / Docker do not provide a TTY; main.py auto-detects this and
# switches to log-only mode + skips the interactive REAL confirmation.
CMD ["python", "-u", "main.py"]
