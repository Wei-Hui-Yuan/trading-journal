# Trading Journal API

FastAPI app backed by a Supabase Postgres instance.

## Setup

```bash
pip install -r requirements-dev.txt
```

```bash
cp .env.example .env   # then fill in your Supabase credentials
```

`requirements-dev.txt` is the one to install for development: it pulls in the
runtime lock plus the test suite. `requirements.txt` is runtime only and is what
the Dockerfile installs, so the production image does not ship pytest.

Both files are **fully pinned, including transitive dependencies**, so an
install today resolves to exactly what CI and production run. Upgrades are
deliberate — see the header comment in `requirements.txt` for the procedure.

## Run

```bash
uvicorn main:app --reload
```

Interactive docs at http://localhost:8000/docs

## Tests

```bash
python -m pytest tests/
```

Runs offline by default. The ~30 tests that exercise real Postgres semantics
(EXISTS correlation, transaction/savepoint behaviour a stub session cannot
stand in for) skip themselves unless `DATABASE_URL` is set; the rest use stub
sessions and need nothing external.

CI (`.github/workflows/ci.yml`) runs the full suite on every push and pull
request against an actual Postgres 17 service container — matching
production's version — with the schema built from nothing but `migrate.py`
applying every migration in order. That build-from-scratch is itself real
coverage: it is what caught migration 000's absence (see below) the first time
a genuinely empty database tried to run these files. `DATABASE_URL` is set at
the job level, so the `requires_db` tests run there instead of skipping.

Dependencies install with `pip install --no-deps`, so a dependency that is
imported but not pinned fails the build rather than being silently fetched.

To run the same thing locally, point `DATABASE_URL` at any empty Postgres and
apply the migrations first:

```bash
python migrate.py
```

```bash
python -m pytest tests/
```

## Response caching

`GET /api/analytics/dashboard`, `GET /api/analytics/advanced` and
`GET /api/round-trips` answer conditional requests. Each sends an `ETag` and
`Cache-Control: private, no-cache`; a client that sends back a matching
`If-None-Match` gets a `304` with no body.

The saving is compute, not bandwidth. These endpoints scan whole tables —
round-trips loads every position, every execution and every fill before it can
answer — so the 304 is only worth having if producing it skips that work. It
does: the only database access before the decision is a single primary-key
lookup of a counter.

That counter is `data_version` (migration 030), incremented by statement-level
triggers on every table the cached endpoints read. It notices inserts, updates
and deletes, which `MAX(created_at)` would not — these tables have no
`updated_at`, so a review being written or a rematch rewriting P&L would leave
a timestamp-keyed cache serving stale numbers.

There is deliberately **one** counter rather than one per endpoint. Renaming a
strategy invalidates the equity curve unnecessarily; that costs a
recomputation nobody notices. Per-endpoint scopes would mean every future
endpoint has to correctly declare its dependencies, and getting that wrong
produces a stale figure that looks plausible.

Before migration 030 is applied the counter cannot be read, and the endpoints
degrade to untagged `200`s — exactly their previous behaviour. They never 500
over it.

## Migrations

SQL files in `migrations/`, applied by `migrate.py`, which records what it has
run in a `schema_migrations` table so nothing runs twice.

```bash
python migrate.py --status
```

```bash
python migrate.py --dry-run
```

```bash
python migrate.py
```

**Connect on 5432, not 6543.** These are DDL statements, and Supabase's
transaction-mode pooler does not reliably support them — the same constraint
that keeps `create_all` out of app startup. The runner refuses port 6543 unless
passed `--allow-pooler`.

### Migration 000: two tables this directory did not create

`trades` and `strategies` were created by hand in the Supabase SQL Editor
before `migrations/` existed. No file here ever issues their `CREATE TABLE` —
every migration from 001 onward simply assumes both are already there, which
was invisible for as long as every environment descended from the same
hand-created database.

It stopped being invisible the moment CI needed a genuinely empty Postgres:
migration 001 failed immediately with `relation "trades" does not exist`.
`000_bootstrap_hand_created_tables.sql` recreates both tables in their
pre-migration-001 shape — reconstructed by reading every later migration for
what it assumes already exists, then verified column-for-column against
production's actual `information_schema.columns` (a byte-for-byte match: 34
columns on `trades`, 8 on `strategies`, including types, nullability and
defaults).

Safe to run against production, where both tables already exist: every
statement is `IF NOT EXISTS`, so it is a no-op there and only does real work
on a database that has never seen either table. It is new, though, so
production's `schema_migrations` shows it as genuinely pending — a plain
`python migrate.py` picks it up (not `--baseline`, which would skip running it
and record it with an unverified NULL checksum for a file that is in fact safe
to actually execute).

### Adopting a database migrated by hand

The production database already has 31 of these migrations applied, from
before this tracking existed (000 and 030 came later — see above and
`030_data_version.sql`). Run this against it **once**, and never again:

```bash
python migrate.py --baseline
```

That records every migration currently on disk as applied **without running
any of them**. From then on only genuinely new files run. A fresh, empty
database needs no baseline — just run `python migrate.py`.

Baselined rows carry a NULL checksum, which `--status` shows as `~`. It means
"this ran at some point, but what ran was never recorded", which is the honest
position for a database migrated by hand.

### Rules

- **Never edit a migration that has been applied.** The database keeps the old
  shape while the file describes a new one, and every environment built fresh
  from that file diverges from production. The runner detects this by checksum
  and refuses to proceed. Write a new migration instead.
- **Never reuse a number.** Two migrations sharing a prefix have no defined
  order. `migrate.py` refuses to run when it finds one, and a test in
  `tests/test_migrate.py` catches it before that.
- **A migration's filename is its identity.** Renaming one that has already run
  makes the database think it is new. This is why the two historical collisions
  below were left alone rather than renumbered.

### The 021 and 022 collisions

Two prefixes were each used twice, before any of the above was enforced:

| Prefix | Files |
| --- | --- |
| 021 | `021_drop_redundant_position_fill_indexes.sql`, `021_timeframe_presets.sql` |
| 022 | `022_positions_direction.sql`, `022_realized_legs.sql` |

They are recorded as known exceptions in `GRANDFATHERED_DUPLICATES` and allowed
through. Renumbering was considered and rejected: an applied migration's
filename is its identity, and roughly a hundred comments across the codebase
cite these numbers.

Leaving them is safe because the four touch entirely disjoint objects — two
indexes on `position_fills`, a new `timeframe_presets` table, a column on
`positions`, and a new `realized_legs` table — so no ordering between them
changes the result. The runner still orders them deterministically (by number,
then by full filename), so every environment applies them identically.

Do not add to that list. Any *new* duplicate is an error.

## Diagnostic scripts

Standalone, run by hand when something is worth checking rather than from CI
or the app itself. Every one is read-only unless noted otherwise.

- `python check_storage_usage.py` — Supabase free-tier quota usage, projected
  forward at the current growth rate.
- `python diagnose_flex.py` — runs the configured IBKR Flex queries one at a
  time and explains any error code, rather than the app's own "did not
  return".
- `python reconcile_positions.py` — re-runs FIFO for every ticker and diffs
  the result against what `positions` currently stores. A clean run prints
  zeros; run it after any bulk import or when a headline figure looks wrong.
- `python verify_bootstrap_schema.py` — checks migration 000's hand-written
  reconstruction of `trades`/`strategies` against a real database's actual
  `information_schema`. Meant to be run against production.
- `python verify_convergence.py` — checks that incremental matching (fill by
  fill, as the sync does it) converges to the same state as a full rebuild
  from scratch.
- `python verify_round_trips.py` — checks the current `/api/round-trips`
  query against the Python implementation it replaced, on real data.

## Routes

- `GET /api/trades` — returns all trades grouped by status.
- `PUT /api/trades/{id}` — submits manual planning and qualitative fields (planned_entry,
  stop_loss, target, risk_percent, strategy_id, style, grade, market_regime, source_tag,
  screenshot_url, lessons_comments, and the discipline checkboxes) and transitions a
  `pending_review` trade to `completed`. Only fields present in the request body are updated.
  IBKR-populated execution fields (actual_entry, exit_price, quantity, entry_date, exit_date,
  ticker, direction, ibkr_exec_id) are locked and cannot be changed through this endpoint.
  Returns 400 if the trade is not currently pending review, 404 if it does not exist.

- `POST /api/sync/ibkr` — pulls executions from the IBKR Flex Web Service. Initiates a
  request, polls `GetStatement` until the report compiles (retries on codes 1018/1019),
  maps `<TradeConfirmation>` nodes into `trades`, and inserts them with status
  `pending_review` using `ON CONFLICT (ibkr_exec_id) DO NOTHING`. Safe to run repeatedly.
  Returns a summary: fills found, inserted, duplicates skipped, unparseable rows skipped.

  Requires `IBKR_TOKEN` and `IBKR_QUERY_ID` in `.env`.

  Field mapping: `tradeID`→`ibkr_exec_id`, `symbol`→`ticker`, `price`→`actual_entry`,
  `quantity`→`quantity` (absolute value; side is carried by `direction`),
  `buySell`→`direction`, `dateTime`→`entry_date`. `style` is NOT NULL in the schema but
  IBKR cannot supply it, so synced rows get the placeholder `Unclassified` for you to set
  during review. `currency` has no column in the current schema and is not stored.

## Schema

Models in `main.py` mirror the `strategies` and `trades` tables (UUID primary keys). See the
`CREATE TABLE` definitions in Supabase for the source of truth.
