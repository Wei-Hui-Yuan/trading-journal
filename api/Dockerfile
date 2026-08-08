# ---- Stage 1: builder ----
FROM python:3.12-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# requirements.txt only -- NOT requirements-dev.txt. The test suite and its tree
# stay out of the image that serves traffic.
#
# gunicorn used to be a second, unpinned `pip install` on the end of this line,
# which left the process manager supervising everything else as the one
# dependency no lockfile described. It is a normal pinned entry now.
#
# --no-deps enforces the lock: every transitive dependency is already named and
# pinned in requirements.txt, so pip has nothing left to resolve. Without it a
# pin that is quietly incomplete still builds, by silently fetching the newest
# release of whatever is missing -- which is the failure this file exists to
# prevent. If the build breaks here, the lock is wrong; fix the lock.
RUN pip install --no-cache-dir --user --no-deps -r requirements.txt

# ---- Stage 2: runner ----
FROM python:3.12-slim AS runner

WORKDIR /app

COPY --from=builder /root/.local /root/.local

# Every top-level module, not a hand-listed subset: naming files individually
# meant a new one (auth.py) silently vanished from the image and the container
# died on import. The *.py glob still cannot pull in .env, so secrets stay out.
COPY *.py ./
COPY services/ ./services/

# Worker count is deliberately low and set via WEB_CONCURRENCY (gunicorn reads
# that natively) rather than a baked-in --workers flag, so it can be tuned from
# the Northflank dashboard without a rebuild.
#
# These are *async* uvicorn workers: one process already serves many concurrent
# requests, since the handlers are I/O-bound and yield while awaiting. Extra
# processes buy little here and cost a full interpreter each -- FastAPI,
# SQLAlchemy, asyncpg and cryptography are loaded per worker, which is enough
# to starve a small container.
ENV PYTHONUNBUFFERED=1 \
    PATH=/root/.local/bin:$PATH \
    WEB_CONCURRENCY=2

EXPOSE 8000

CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000", "--timeout", "120", "main:app"]
