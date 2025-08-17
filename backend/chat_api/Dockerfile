# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip wheel --no-cache-dir --no-deps -r requirements.txt -w /wheels

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim
RUN useradd -m -u 10001 appuser
WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

# App code
COPY app ./app
COPY requirements.txt .

ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HOST=0.0.0.0

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:${PORT}/health || exit 1

USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
