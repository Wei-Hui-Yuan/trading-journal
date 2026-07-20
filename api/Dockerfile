# ---- Stage 1: builder ----
FROM python:3.12-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --user -r requirements.txt \
    && pip install --no-cache-dir --user gunicorn

# ---- Stage 2: runner ----
FROM python:3.12-slim AS runner

WORKDIR /app

COPY --from=builder /root/.local /root/.local

COPY main.py .
COPY services/ ./services/

ENV PYTHONUNBUFFERED=1 \
    PATH=/root/.local/bin:$PATH

EXPOSE 8000

CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000", "--workers", "4", "main:app"]
