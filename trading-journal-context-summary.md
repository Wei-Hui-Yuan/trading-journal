# Trading Journal — System Context Summary

Written for an external AI agent picking up this project cold. Verified against
live system state as of 2026-07-21 (not just git history — endpoints were
curled, tests were run, migrations confirmed applied).

## What this is

A personal trading journal for one user (Huiyuan, Singapore, trades US
equities on Interactive Brokers). Backend: FastAPI + async SQLAlchemy 2.0 on
Python 3.12. Frontend: Next.js 14 App Router + TanStack Query v5. Database:
Supabase Postgres. The user is not a professional software engineer — explain
tradeoffs, don't assume familiarity with deployment tooling.

## Repositories and deployment topology

Two directories, two Vercel/Northflank projects, one Supabase project:

- `C:\Users\ianwe\trading-journal` — Next.js frontend, deployed to **Vercel**
- `C:\Users\ianwe\trading-journal\api` — FastAPI backend, deployed to
  **Northflank** at `https://p01--host--5zs8snzvhrhl.code.run`
  (note the trailing `l`, not `i` — this host name has been mistyped before)
- **Supabase** Postgres, project ref `zqrmekexzlhhmviabjya`, region
  `ap-southeast-1`. Connected via the **transaction-mode pooler** (PgBouncer,
  port 6543), NOT the direct connection (port 5432).

GitHub: `Wei-Hui-Yuan/Trading-Journal-`, single `main` branch, no CI gate —
pushes to `main` are what trigger both Vercel and Northflank redeploys.

## Critical connection facts (things that have actually broken)

1. **`statement_cache_size=0` is mandatory** in the asyncpg connect_args
   whenever the app talks to the pooler on port 6543. PgBouncer in
   transaction mode does not support asyncpg's prepared-statement cache;
   omitting this causes cryptic failures. See `api/main.py`, `engine =
   create_async_engine(...)`.

2. **A single missing/wrong character in `DATABASE_URL`'s username silently
   breaks everything and does NOT surface as an auth error to the frontend.**
   This happened for real: the project ref was entered as
   `zqrmekexzlhmviabjya` (19 chars, missing one `h`) instead of
   `zqrmekexzlhhmviabjya` (20 chars). Supabase's pooler rejects this with
   `(ENOTFOUND) tenant/user ... not found` — a *connection* error, not a
   password error, raised before credentials are even checked. This existed
   in both the local `.env` and Northflank's env vars simultaneously.

3. **A 500 error used to reach the browser with NO
   `Access-Control-Allow-Origin` header**, because Starlette builds its
   default 500 response outside the CORS middleware stack. The browser then
   refuses to expose the response to JS at all, and axios reports a bare
   "Network Error" — indistinguishable from the API being completely down or
   DNS being broken. This is now fixed (see Fix Log below) but the general
   principle holds for any future middleware/exception-handler work: **error
   responses generated outside `CORSMiddleware`'s wrapping will not carry
   CORS headers unless something explicit puts them there.**

4. **`NEXT_PUBLIC_*` env vars are baked into the Next.js bundle at BUILD
   time**, not read at runtime. If `NEXT_PUBLIC_API_URL` is wrong on Vercel,
   the fix requires a full redeploy, not just an env var change taking
   effect on next request. Default fallback in `src/lib/api.ts` is
   `http://localhost:8000` if the env var is unset — meaning a production
   build with no env var configured will silently try to call localhost.

5. **Deploy ordering matters.** The frontend and backend are two independent
   deploy pipelines (Vercel, Northflank) triggered by the same `git push`.
   If the frontend redeploys with code that calls a new backend endpoint
   (e.g. `/api/round-trips`) before Northflank finishes building, there is a
   window where the feature 404s or network-errors. No coordination exists
   between the two; whichever finishes first wins the race.

6. **gunicorn worker count**: `WEB_CONCURRENCY` env var, read natively by
   gunicorn (`gunicorn/config.py` default is `int(os.environ.get(
   "WEB_CONCURRENCY", 1))`). Currently set to `2` in the Dockerfile. This was
   previously hardcoded to 4 processes and caused ~20x latency (9-15s for a
   bare 404) on Northflank's small container — each uvicorn worker loads a
   full interpreter with FastAPI/SQLAlchemy/asyncpg/cryptography, and since
   handlers are async/I/O-bound, one process already serves many concurrent
   requests. Do not raise worker count to "fix" slowness without checking
   container memory first.

7. **gunicorn `--timeout 120`** is required in the CMD. IBKR sync calls can
   run long (multiple Flex API round-trips with retry/backoff), and
   gunicorn's default 30s worker timeout was killing the request mid-sync,
   producing `[CRITICAL] WORKER TIMEOUT` in Northflank logs.

## Auth architecture

- **Clerk** handles both frontend session management and backend JWT
  verification. Same Clerk instance across dev/prod (`vast-grackle-22`,
  visible in the publishable key `pk_test_...` — currently a `test` instance,
  not `live`).
- Frontend: `@clerk/nextjs@6.39.6` (pinned below 7.x because 7.x requires
  Next 15+, and the user has an explicit standing instruction: **do not
  upgrade Next.js mid-feature-build**). `src/middleware.ts` uses
  `clerkMiddleware()` + `auth.protect()`, public routes are only
  `/sign-in(.*)` and `/sign-up(.*)` — every other route including `/` and
  `/journal` requires a session.
- `src/lib/api.ts` attaches the session JWT via `window.Clerk.session.getToken()`
  (documented Clerk escape hatch for use outside React components) as
  `Authorization: Bearer <token>` in an axios request interceptor. Has an
  explicit 5-second timeout on token retrieval — axios's own `timeout` option
  does NOT cover time spent inside a request interceptor, so without this a
  hung Clerk call would leave requests pending forever with no visible error.
- Backend: `api/auth.py`, `verify_clerk_token` dependency. Verifies against
  Clerk's JWKS endpoint. Hardened against three forgery classes (confirmed by
  actually attempting them): wrong signing key with correct `kid`, `alg:none`,
  and RS256→HS256 key-confusion. `ALGORITHMS = ["RS256"]` is pinned explicitly
  — this is what blocks the last two.
- `CLERK_JWKS_URL` env var: **if unset, every protected route returns 500**,
  deliberately — the code never falls back to "unverified access." Accepts a
  bare Clerk origin and normalizes it by appending `/.well-known/jwks.json`
  (a bare origin was previously being pasted directly, which returns HTTP 200
  with an empty body and `application/json` content-type — `raise_for_status()`
  passed, then `.json()` threw, and the resulting error message wrongly said
  "could not reach" a host that had, in fact, responded).
- `HTTPBearer(auto_error=False)` — deliberately not `True`, which would
  return a generic 403 "Not authenticated" instead of the app's own 401 with
  a specific message.
- Every protected endpoint uses `dependencies=[Depends(verify_clerk_token)]`.
  `GET /health` is the one deliberately unauthenticated route.

## Database schema (Supabase Postgres, 12 migrations applied)

Core tables:

- **`trades`** — the immutable execution ledger. One row per broker fill or
  hand-logged trade. Never mutated by the matching engine, only appended to.
  Key columns: `ticker`, `direction` (BUY/SELL), `quantity NUMERIC(18,8)`
  (NOT integer — fractional shares are normal on this account and rounding
  previously destroyed sub-share positions), `actual_entry`, `planned_entry`,
  `stop_loss`, `actual_stop_loss` (added migration 012 — separate from
  `stop_loss` so a mid-trade stop adjustment is visible instead of
  overwriting the original plan), `target`, `risk_percent`, `risk_amount`
  (migration 012), `conviction SMALLINT 1-5 CHECK constraint` (migration
  012), `emotional_state TEXT` (migration 012), `thesis TEXT` (migration
  011 — why the trade was taken, written at entry), `strategy_id FK`,
  `ibkr_exec_id` (unique, nullable — dedup key for broker syncs).

- **`positions`** — closed round trips only, produced by the FIFO matching
  engine (`api/services/matching_engine.py`). A position exists ONLY once a
  ticker returns to flat (zero net quantity). **An open/unsold trade has NO
  row here at all** — this was the root cause of a real bug where a manually
  logged trade "disappeared": every original UI surface read from
  `positions`, so an unsold buy was invisible everywhere. Columns:
  `entry_price`/`exit_price` (quantity-weighted averages across all fills in
  the round trip), `realized_pnl`, `review_status` (`pending`/`reviewed`),
  `review_went_well`/`review_went_wrong`/`review_lessons` (migration 011,
  three separate columns — a single combined notes box reliably collapses
  into only recording what went wrong), `exit_reason`, `ideal_entry/stop/
  target` and `revised_entry/stop/target` (migration 012 — two DELIBERATELY
  SEPARATE families: `ideal_*` is hindsight-optimal levels for THIS trade
  (scores plan quality), `revised_*` is the corrected rule for the NEXT
  instance of this setup (feeds strategy refinement); conflating them was
  explicitly rejected during design).

- **`position_fills`** (migration 009) — join table recording which
  individual `trades` rows composed a `positions` round trip, with a `role`
  column (`OPEN`/`CLOSE`, constants `ROLE_OPEN`/`ROLE_CLOSE` in
  `matching_engine.py`). This is what makes execution drill-down possible:
  a position's `entry_price` is a weighted average and this table has the
  individual fills behind it.

- **`strategies`** — the playbook. `name` UNIQUE, `method`/`entry_criteria`/
  `exit_criteria` text fields, feeds the strategy dropdown in both manual
  trade logging and the journal's plan editor.

- **`ibkr_executions`** (migration 005) — staging table for broker sync,
  keyed by IBKR's `transaction_id` (UNIQUE), used with
  `pg_insert(...).on_conflict_do_nothing()` for idempotency. Postgres treats
  NULLs as distinct in unique indexes — relevant if any dedup key can be
  null.

Full migration list: `001_create_positions.sql` through
`012_trade_plan_and_review_depth.sql`, all applied directly against
Supabase (no migration runner/framework — SQL files are run by hand via
asyncpg, tracked in `api/migrations/`).

## The matching engine (`api/services/matching_engine.py`)

FIFO lot-queue matching using `deque`. Key design decisions:

- **Round trips are flat-to-flat aggregations**, not individual fill pairs.
  Scaling in with 2 buy orders and scaling out with 2 sell orders produces
  ONE `positions` row with 4 linked `position_fills`, not 4 separate
  positions. This was a real point of user confusion — CRWD showed as 4
  "trades" before the `/api/round-trips` endpoint was built, even though
  the underlying data was already correctly grouped.
- Going flat is checked BEFORE a leftover fill opens a new lot — this
  matters for the case where an oversell flips a position from long to
  short: it correctly closes the long round trip and starts a fresh short
  one, rather than merging them.
- `realized_pnl` is **summed from individual legs, never recomputed from
  weighted averages** — this avoids a subtle rounding-accumulation bug.
- All quantity/price math uses `Decimal`, never float — share counts
  multiply into money and float error compounds.
- `PRICE_PRECISION = Decimal("0.0001")`.

## R-multiple / analytics (`api/services/analytics.py`)

This is a pre-existing, fully-built module that was recently discovered to
be "starved" rather than broken:

- `compute_r_multiple(trade: ReviewedTrade) -> Optional[float]` — long:
  `(exit - entry) / (entry - stop)`; short: `(entry - exit) / (stop -
  entry)`. **Returns `None`, never `0.0`, when unscoreable** (no exit, no
  stop, or stop on the wrong side / at entry giving non-positive risk).
  This distinction is load-bearing: conflating "no risk was defined" with
  "the trade broke even" would corrupt every aggregate built on top
  (expectancy, average R, the R-distribution buckets).
- `compute_slippage`, `compute_expectancy`, `_r_distribution`,
  `compute_advanced_metrics` all already existed and work correctly.
- The problem was purely that only 1 of 35 `trades` rows had a `stop_loss`
  value populated, because there was no UI to enter one. As of migration
  012 + the round-trip journal UI, this input finally exists. Verified live:
  adding a stop to the CRWD position scored it at R = -0.9738 and the
  advanced-analytics endpoint picked it up automatically with zero changes
  to the analytics module itself.
- `load_reviewed_trades()` joins `positions` (what happened — entry, exit,
  review) to `trades` (what was intended — via `positions.open_trade_id`,
  reading `stop_loss`/`planned_entry` off the OPENING execution). This
  matters: the plan cannot live on `positions` because an open trade has no
  position row yet, and it cannot live per-execution on every fill because a
  scale-in would then have multiple, potentially contradictory stops with no
  way to say which one was "the" plan. Exactly one execution — the opening
  one — owns the plan.

## `GET /api/round-trips` — the newest, most important endpoint

Added in commit `9b89aa7`. This is the endpoint the Journal page actually
uses (NOT `/api/trades`, which returns raw unmatched executions and predates
this work). It:

- Returns one row per trade idea. Closed trades come from `positions` +
  their `position_fills`. Open trades are reconstructed by grouping
  unmatched `trades` rows per ticker (net signed quantity determines
  direction; zero-net-but-unmatched groups are skipped as a data oddity
  rather than rendered as a phantom zero-size position).
- Each row carries `r_multiple` and `planned_r_multiple`, computed on every
  read via `_score_r()` (a thin wrapper delegating to
  `services.analytics.compute_r_multiple` — deliberately not reimplemented,
  so the journal and the analytics tab can never disagree on a sign
  convention). **R is never stored** — recomputed from current entry/exit/
  stop every time, so correcting a stop after the fact cannot leave a stale
  R value behind.
- Every row embeds its `fills: PositionFillOut[]` and the full plan +
  review field set, so the frontend does one fetch per journal page load
  rather than N+1 drill-down calls.
- Verified against live data: 35 raw executions collapse to 16 round trips
  (3 open + 13 closed at the time of testing).

## Frontend structure

- `src/app/page.tsx` — dashboard (KPI strip, day-of-week heatmap, trade
  inbox queue). Reads `useDashboardStats()`.
- `src/app/journal/page.tsx` — the master journal, renders
  `<TradeLedger />`. This is the densest page in the app by design.
- `src/components/TradeLedger.tsx` — reads `useRoundTrips()`
  (`/api/round-trips`), renders one expandable row per round trip. Each
  expanded row has THREE sections with TWO independent Save buttons:
  - **The Plan** (saves via `useAnnotateTrade()` → `PATCH /api/trades/
    {plan_trade_id}`): strategy, conviction, planned/actual entry,
    planned/actual stop, target, risk %/$, emotional state, thesis.
  - **Executions** (read-only): the fills table.
  - **The Review** (saves via `useReviewPosition()` → `PATCH /api/
    positions/{position_id}/review`, only rendered for closed trades):
    exit reason, grade, ideal levels, revised levels, the three
    post-mortem text questions.
  - Two buttons instead of one because the plan and review write to two
    different backend resources (`trades` vs `positions`); a single button
    spanning both could not report a partial failure honestly.
- `src/hooks/useTradeInbox.ts` — all TanStack Query hooks + `queryKeys`
  object. `useAnnotateTrade` and `useReviewPosition` both invalidate
  `queryKeys.roundTrips` and `queryKeys.advancedMetrics` on success, so
  editing a stop in the journal immediately refreshes both the journal
  itself and the analytics tab.
- `src/lib/api.ts` — axios client, `baseURL = ${NEXT_PUBLIC_API_URL ||
  'http://localhost:8000'}/api`. Response interceptor normalizes FastAPI's
  `detail` field into `error.message`, and — as of the CORS fix — gives a
  named, specific message (`Could not reach <baseURL>...`) when a request
  fails with NO `response` object at all (the CORS-stripped-error / DNS-down
  / server-actually-down case that used to render as a generic axios
  "Network Error").

## Known standing user instructions (do not violate without asking)

- **Do not upgrade Next.js mid-feature-build.** It was deliberately upgraded
  once, to exactly `14.2.35`, to close CVEs — a targeted, isolated task, not
  a general policy of staying current. Assume any future Next.js version
  bump needs explicit sign-off.
- The user rotated the Supabase DB password once already after pasting it in
  plaintext in a chat session. If a database credential ever needs to be
  shared for debugging, do not have the user paste the raw value in chat —
  ask them to check it in the Supabase/Northflank dashboard directly, or
  paste only a redacted/partial form.

## Things NOT yet verified end-to-end

- **The actual rendered UI of the Journal page has never been visually
  confirmed by an AI agent.** Every route requires a live Clerk session;
  browser automation tools available in past sessions had no signed-in
  session and correctly refused to enter credentials. All verification to
  date has been API-level (curl / httpx against a rolled-back DB
  transaction) plus `tsc --noEmit` + production `npm run build` succeeding.
  If asked to "check the UI," the honest answer is: data-layer correctness
  is proven, pixel-level layout is not.
- IBKR Flex sync (`POST /api/ingest/ibkr`) has real rate-limiting behavior
  (Flex error codes 1001/1018/1019, only 1019 is retried in-request because
  retrying 1001 was found to EXTEND the lockout) that has not been
  re-exercised since the most recent schema changes. Two Flex query IDs are
  configured (comma-separated in `IBKR_QUERY_ID`): a TCF (Trade
  Confirmation) query for near-real-time fills and an Activity (AF) query
  for historical/commission data — both required because neither alone is
  complete, and the `ibkr_executions.transaction_id` UNIQUE constraint was
  specifically verified (by sorting fills by ID and checking for
  chronological violations) to make running both safe against duplication.

## How to sanity-check the whole system in one pass

```bash
curl -s https://p01--host--5zs8snzvhrhl.code.run/health
# want: {"status":"ok","database":"ok","database_error":null,
#         "supabase_project_ref":"zqrmekexzlhhmviabjya"}
# database_error present-and-null (not just "key absent") confirms the
# currently-deployed backend is at or after commit 192e6ba.

cd api && python -m pytest tests/ -q
# 138 tests as of commit 9b89aa7, across test_auth.py, test_ibkr_parser.py,
# test_matching_engine.py, test_error_responses.py, test_round_trips.py

npx tsc --noEmit   # from the frontend root — must be clean
```
