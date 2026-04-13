# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Bağımlılıkları önce kopyala — layer cache için
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Build stage'den sadece kurulu paketleri al
COPY --from=builder /install /usr/local

# Uygulama kodunu kopyala
COPY . .

# Veri klasörü — SQLite DB buraya mount edilir
RUN mkdir -p /app/data

# Ortam değişkenleri (Railway veya docker-compose'dan gelir, burada sadece default)
ENV DATABASE_URL="sqlite+aiosqlite:////app/data/deutschmeister.db" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
