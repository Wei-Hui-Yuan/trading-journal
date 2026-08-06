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

Runs offline. The ~30 tests that exercise real Postgres semantics skip
themselves unless `DATABASE_URL` is set; the rest use stub sessions.

CI (`.github/workflows/ci.yml`) runs this on every push and pull request, along
with the frontend typecheck and production build. It installs dependencies with
`pip install --no-deps`, so a dependency that is imported but not pinned fails
the build rather than being silently fetched.

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
