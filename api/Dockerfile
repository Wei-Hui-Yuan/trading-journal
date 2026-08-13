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

# --timeout MUST stay above services/ibkr_client.TOTAL_BUDGET_SECONDS, and
# test_the_budget_is_below_the_worker_timeout reads this line to enforce it.
#
# It was 120 against a 240s budget: the sync was allowed to spend twice as long
# as the process manager would tolerate. The budget already exists to bound the
# handshake -- see the long comment on TOTAL_BUDGET_SECONDS -- and a worker
# ceiling underneath it takes that decision away from the code that reasoned
# about it, cutting a run off at an arbitrary point instead of the planned one.
#
# Under cron that gets worse rather than better. A run killed mid-ingest can
# leave fills promoted into `trades` but never matched into round trips, which
# is the state _symbols_awaiting_match exists to recover from -- and unattended,
# nobody is watching it happen.
#
# 300 matches the browser's own ceiling in src/lib/api.ts, so the 240s budget is
# once again the binding constraint on every path. Raising it is free: gunicorn
# only acts on the timeout when a worker actually goes quiet for that long.
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000", "--timeout", "300", "main:app"]
