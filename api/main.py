import logging
import os
import uuid
from contextlib import asynccontextmanager
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    CHAR,
    Boolean,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from asyncpg.exceptions import UndefinedTableError
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from auth import verify_clerk_token

load_dotenv()

logger = logging.getLogger(__name__)


def _build_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    user = os.environ["SUPABASE_DB_USER"]
    password = os.environ["SUPABASE_DB_PASSWORD"]
    host = os.environ["SUPABASE_DB_HOST"]
    port = os.environ.get("SUPABASE_DB_PORT", "5432")
    name = os.environ.get("SUPABASE_DB_NAME", "postgres")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


DATABASE_URL = _build_database_url()

# statement_cache_size=0 is required when connecting through Supabase's
# transaction-mode pooler (PgBouncer, port 6543), which does not support
# the prepared statements asyncpg caches by default.
engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"statement_cache_size": 0},
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class ReviewStatus(str, Enum):
    """Lifecycle of a position's qualitative review.

    One vocabulary for every surface: the Trade Inbox checklist and the
    Analytics notes/mistakes drawer both terminate at 'reviewed', so a single
    `review_status = 'pending'` filter drives both queues.
    """

    pending = "pending"
    reviewed = "reviewed"


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    # Predates migration 002; retained so existing rows keep their data.
    instruments = Column(ARRAY(Text), default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Playbook fields (migration 003). Default to '' rather than NULL so the
    # editor always has a string to bind to.
    method = Column(Text, default="")
    entry_criteria = Column(Text, default="")
    exit_criteria = Column(Text, default="")


class Discipline(Base):
    """A rule the trader holds themselves to. User-editable (migration 013)."""

    __tablename__ = "disciplines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(Text, nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PositionDiscipline(Base):
    """Whether one round trip honoured one rule (migration 014).

    A join table rather than columns on `positions`, because the rules are
    user-editable: three fixed booleans could only ever answer the three rules
    that shipped, and a rule the user added had nowhere to store its answer.

    Absence of a row means "not reviewed against this rule", which is NOT the
    same as `followed = False`. Conflating them would let an unreviewed
    backlog read as indiscipline, and drag every compliance rate toward zero.
    """

    __tablename__ = "position_disciplines"

    position_id = Column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    discipline_id = Column(
        UUID(as_uuid=True),
        ForeignKey("disciplines.id", ondelete="CASCADE"),
        primary_key=True,
    )
    followed = Column(Boolean, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AppSetting(Base):
    """The one row of trader-level defaults (migration 015).

    Holds what the position-size calculator needs and the ledger cannot infer:
    current account size and the share of it a trade may risk. Single-row by
    construction -- `CHECK (id = 1)` in the schema -- so reads and writes never
    have to choose between rows.

    Deliberately current-state only. What each trade actually risked is on
    `trades.risk_amount`, captured at entry; keeping a second history here
    would give the same question two answers.
    """

    __tablename__ = "app_settings"

    SINGLETON_ID = 1

    id = Column(Integer, primary_key=True, default=SINGLETON_ID)
    # Nullable: unset means unknown, not zero. The calculator withholds a share
    # count rather than confidently suggesting none.
    account_size = Column(Numeric(14, 2), nullable=True)
    # Percent, matching trades.risk_percent's unit so the two compare directly.
    risk_percent = Column(Numeric(5, 2), nullable=False, default=Decimal("1.00"))
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SuppressedExecution(Base):
    """A broker fill the user deleted on purpose (migration 016).

    Ingest is idempotent through ON CONFLICT DO NOTHING, which skips rows that
    still exist -- a deleted row does not, so the next sync would re-insert it
    and the delete would silently undo itself. This is the tombstone that makes
    a deletion permanent.

    Keyed by the broker id rather than a foreign key precisely because the row
    it names is gone; a FK would cascade away with it and suppress nothing.
    """

    __tablename__ = "suppressed_executions"

    ibkr_exec_id = Column(Text, primary_key=True)
    ticker = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # The fill's own facts (migration 017), copied at tombstone time. The row
    # they came from is deleted, so they cannot be joined back from `trades` --
    # without them the management list can name an id but not a trade.
    direction = Column(Text, nullable=True)
    quantity = Column(Numeric(18, 8), nullable=True)
    price = Column(Numeric(10, 4), nullable=True)
    executed_at = Column(DateTime(timezone=True), nullable=True)


class Trade(Base):
    __tablename__ = "trades"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ibkr_exec_id = Column(String(100), unique=True, nullable=True)
    ticker = Column(String(10), nullable=False)
    direction = Column(String(5), nullable=False)
    style = Column(String(15), nullable=False)
    entry_date = Column(DateTime(timezone=True), nullable=False)
    exit_date = Column(DateTime(timezone=True), nullable=True)
    actual_entry = Column(Numeric(10, 4), nullable=False)
    exit_price = Column(Numeric(10, 4), nullable=True)
    # NUMERIC not INTEGER: fractional fills are ordinary on this account, and
    # rounding them destroyed sub-half-share positions outright (migration 010).
    quantity = Column(Numeric(18, 8), nullable=False)
    planned_entry = Column(Numeric(10, 4), nullable=True)
    stop_loss = Column(Numeric(10, 4), nullable=True)
    # Where the stop ACTUALLY sat, after any mid-trade moves. Separate from
    # stop_loss so widening a stop shows up instead of overwriting the intent.
    actual_stop_loss = Column(Numeric(10, 4), nullable=True)
    target = Column(Numeric(10, 4), nullable=True)
    # Widened from (4,2) in migration 015: derived from actual quantity, a
    # margined position can exceed 100% of the account.
    risk_percent = Column(Numeric(6, 2), default=1.00)
    # The absolute figure, which is what turns an R-multiple back into money.
    risk_amount = Column(Numeric(12, 2), nullable=True)
    # Provenance for hand-corrected fills (migration 016). NULL edited_at means
    # untouched since it arrived. broker_original snapshots what the broker
    # said before the FIRST edit and is never overwritten, so a second edit
    # cannot quietly replace the original with the first edit's values.
    edited_at = Column(DateTime(timezone=True), nullable=True)
    broker_original = Column(JSONB, nullable=True)
    strategy_id = Column(
        UUID(as_uuid=True), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    grade = Column(CHAR(1), nullable=True)
    market_regime = Column(String(20), nullable=True)
    source_tag = Column(String(10), default="Own")
    # Why this trade was taken, captured at entry. Written before the
    # outcome is known, which is the whole point -- a thesis reconstructed
    # afterwards is just the result with reasoning attached.
    thesis = Column(Text, nullable=True)
    # Rated at entry, before the outcome is known. Correlating conviction
    # against realised R is how overconfidence becomes a number.
    conviction = Column(Integer, nullable=True)
    emotional_state = Column(Text, nullable=True)
    screenshot_url = Column(Text, nullable=True)
    hard_sl_set = Column(Boolean, default=True)
    waited_retest = Column(Boolean, default=True)
    followed_plan = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Which pre-trade plan this fill came from, once a sync matched one
    # (migration 018). NULL means the fill arrived unplanned, which is the
    # normal state for everything imported before plans existed.
    plan_id = Column(
        UUID(as_uuid=True), ForeignKey("planned_trades.id", ondelete="SET NULL"), nullable=True
    )


# Plan lifecycle. Mirrored by a CHECK constraint in migration 018, so a typo
# here fails loudly at write time instead of quietly emptying the dock.
PLAN_OPEN = "OPEN"
PLAN_ATTACHED = "ATTACHED"
PLAN_CANCELLED = "CANCELLED"

# Every execution id records where the row came from. `trades` carries a CHECK
# permitting only these two, which is what makes hand-logging a duplicate of a
# broker fill unrepresentable rather than merely discouraged.
EXEC_PREFIX_BROKER = "IBKR-"
EXEC_PREFIX_REPAIR = "REPAIR-"

# How long an unattached plan stays eligible for auto-attachment. Without a
# bound, a plan written months ago and forgotten would silently claim the next
# fill on that ticker -- and the mis-attribution would look like a feature
# working rather than a stale row.
PLAN_ATTACH_MAX_AGE = timedelta(days=30)


class PlannedTrade(Base):
    """A trade you intend to take, before the broker knows anything about it.

    This exists so that planning a trade cannot create an execution. `trades`
    deduplicates on `ibkr_exec_id`, so a hand-logged row and the broker's copy
    of the same fill had no way to recognise each other -- planning here and
    executing at IBKR produced two rows for one real trade. Content matching
    cannot fix that: IBKR splits an order into several executions (one position
    on this account arrived as 11), so there is often no single quantity to
    match, and matching loosely merges genuine scale-ins instead.

    A plan holds no fill price and never enters P&L. It is joined to reality
    only when a sync imports an execution it can belong to.
    """

    __tablename__ = "planned_trades"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Widths deliberately match `trades`: attaching copies these across, and a
    # plan holding a value the ledger cannot store would fail at attach time.
    ticker = Column(String(10), nullable=False)
    direction = Column(String(5), nullable=False)
    quantity = Column(Numeric(18, 8), nullable=True)

    planned_entry = Column(Numeric(10, 4), nullable=True)
    stop_loss = Column(Numeric(10, 4), nullable=True)
    take_profit = Column(Numeric(10, 4), nullable=True)

    # Computed by Postgres (GENERATED ALWAYS ... STORED), never by this code:
    # writing it from Python is exactly what lets an edited stop leave a stale
    # R behind. `Computed` is not decoration -- it is what makes SQLAlchemy
    # omit the column from every INSERT and UPDATE it builds. Without it,
    # Postgres rejects the write, because a generated column cannot be
    # assigned. The expression must stay identical to migration 018's.
    planned_r = Column(
        Numeric(12, 2),
        Computed(
            "round((take_profit - planned_entry) "
            "/ NULLIF(planned_entry - stop_loss, 0), 2)",
            persisted=True,
        ),
        nullable=True,
    )

    risk_percent = Column(Numeric(6, 2), nullable=True)
    risk_amount = Column(Numeric(12, 2), nullable=True)

    strategy_id = Column(
        UUID(as_uuid=True), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    thesis = Column(Text, nullable=True)

    status = Column(String(20), nullable=False, default=PLAN_OPEN)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    # planned_r and the timestamps are all filled in by the database, so the
    # ORM has to re-read them after an INSERT rather than assume what it wrote.
    __mapper_args__ = {"eager_defaults": True}


class Position(Base):
    """A closed round trip produced by the FIFO matching engine.

    Kept separate from `trades` so raw broker executions remain an immutable
    log; matching only ever appends here.
    """

    __tablename__ = "positions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol = Column(String(20), nullable=False)
    style = Column(String(50), nullable=False)
    quantity = Column(Numeric(18, 8), nullable=False)
    entry_price = Column(Numeric(10, 4), nullable=False)
    exit_price = Column(Numeric(10, 4), nullable=False)
    entry_time = Column(DateTime(timezone=True), nullable=False)
    exit_time = Column(DateTime(timezone=True), nullable=False)
    realized_pnl = Column(Numeric(12, 4), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Which pair of executions produced this position. A unique index on the
    # pair makes re-running the matching engine a no-op instead of a duplicate.
    open_trade_id = Column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="SET NULL"), nullable=True
    )
    close_trade_id = Column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="SET NULL"), nullable=True
    )

    # Review workflow (migration 002). The matching engine writes review_status
    # 'pending'; everything else is filled in by the user via the Trade Inbox.
    strategy_id = Column(
        UUID(as_uuid=True), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    review_status = Column(String(20), default=ReviewStatus.pending.value)
    tag_hard_sl = Column(Boolean, default=False)
    tag_retest = Column(Boolean, default=False)
    tag_plan_compliant = Column(Boolean, default=False)
    trade_grade = Column(String(5), nullable=True)

    # Qualitative review (migration 007). Consolidated here from `trades` so a
    # round trip has exactly one review state.
    notes = Column(Text, nullable=True)
    mistakes = Column(ARRAY(Text), default=list)

    # Post-mortem, split by question (migration 011). One combined box
    # collapses into only ever recording what went wrong.
    review_went_well = Column(Text, nullable=True)
    review_went_wrong = Column(Text, nullable=True)
    review_lessons = Column(Text, nullable=True)

    # How the round trip ended (migration 012). The cheapest field that
    # exposes cutting winners early while letting losers run to the stop.
    exit_reason = Column(Text, nullable=True)

    # Hindsight levels, in two families that must not be merged.
    # ideal_*   -- what this trade's levels should have been, judged after the
    #              fact. Scores PLAN quality, which a good plan executed badly
    #              and a bad plan executed well cannot be told apart without.
    ideal_entry = Column(Numeric(10, 4), nullable=True)
    ideal_stop = Column(Numeric(10, 4), nullable=True)
    ideal_target = Column(Numeric(10, 4), nullable=True)
    # revised_* -- the corrected rule for the NEXT instance of this setup.
    #              Aggregates by strategy and feeds the playbook.
    revised_entry = Column(Numeric(10, 4), nullable=True)
    revised_stop = Column(Numeric(10, 4), nullable=True)
    revised_target = Column(Numeric(10, 4), nullable=True)


class PositionFill(Base):
    """Which executions composed a round trip (migration 009).

    A position aggregates flat-to-flat, so scaling in or out collapses several
    fills into one row in `positions`. This preserves the individual executions
    behind it for drill-down and slippage work.

    `quantity` is the share count attributed to this position rather than the
    fill's full size: one execution can span two round trips when an oversell
    closes a long and opens a short.
    """

    __tablename__ = "position_fills"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    position_id = Column(
        UUID(as_uuid=True),
        ForeignKey("positions.id", ondelete="CASCADE"),
        nullable=False,
    )
    trade_id = Column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(String(5), nullable=False)  # OPEN | CLOSE
    quantity = Column(Numeric(18, 8), nullable=False)
    price = Column(Numeric(10, 4), nullable=False)
    executed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class IBKRExecution(Base):
    """Raw broker fills, staged before promotion into `trades`.

    `transaction_id` is UNIQUE: that constraint is what makes re-syncing an
    overlapping date range a no-op instead of a duplicate.
    """

    __tablename__ = "ibkr_executions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id = Column(Text, nullable=False, unique=True)
    symbol = Column(Text, nullable=False)
    # Signed as IBKR reports it: positive bought, negative sold.
    quantity = Column(Numeric(18, 8), nullable=False)
    price = Column(Numeric(14, 6), nullable=True)
    commission = Column(Numeric(14, 6), nullable=True)
    execution_time = Column(DateTime(timezone=True), nullable=True)
    processed = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


async def get_session():
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown.

    Deliberately does NOT call Base.metadata.create_all. Auto-creating missing
    tables at boot looks like a convenience and is a trap here: create_all
    issues DDL and schema-reflection queries, which Supabase's transaction-mode
    pooler (PgBouncer, port 6543) does not support on a startup connection.
    asyncpg fails, the process dies before serving a single request, and the
    whole service is down -- a strictly worse outcome than the one missing
    table it was meant to paper over.

    It is also the wrong shape of fix: create_all builds schema but never data,
    so it produced an empty `disciplines` table with no seed rows and no column
    defaults, leaving the database quietly diverged from what the migration
    files describe. Schema changes belong in api/migrations/, applied
    deliberately.
    """
    yield
    await engine.dispose()


app = FastAPI(title="Trading Journal API", lifespan=lifespan)

def _cors_origins() -> list[str]:
    """Exact origins allowed to call the API.

    Local dev is always permitted; anything else comes from
    CORS_ALLOW_ORIGINS as a comma-separated list.
    """
    configured = os.environ.get("CORS_ALLOW_ORIGINS", "")
    extra = [origin.strip() for origin in configured.split(",") if origin.strip()]
    return ["http://localhost:3000", *extra]


def _cors_origin_regex() -> Optional[str]:
    """Pattern for origins that change on every deploy.

    Vercel mints a fresh hostname per deployment, so pinning one exact URL
    breaks the moment anything is redeployed. CORS_ALLOW_ORIGIN_REGEX takes a
    pattern (e.g. ``https://trading-journal-.*\\.vercel\\.app``) that survives
    those rotations.

    Widening this is a smaller concession than it looks: CORS governs which
    *sites* a browser lets call the API, not who may read data. Every route
    still demands a valid Clerk JWT, so a permitted origin without a token
    gets a 401 exactly like anyone else.
    """
    return os.environ.get("CORS_ALLOW_ORIGIN_REGEX", "").strip() or None


@app.middleware("http")
async def unhandled_errors_keep_cors_headers(request: Request, call_next):
    """Turn a crash into a JSON 500 that still carries CORS headers.

    Starlette builds its own 500 response *outside* the CORS middleware, so an
    unhandled exception reaches the browser stripped of
    Access-Control-Allow-Origin. The browser then refuses to expose the
    response at all, and the fetch fails as a bare "Network Error" -- which
    reads as "the API is unreachable" when the API in fact answered and said
    precisely what was wrong.

    That misdirection cost a real debugging session: a rotated database
    password surfaced in the UI as a network fault, sending the search to
    hosting and CORS while the API was up the whole time. Catching here, inside
    the CORS layer, means the status and the reason survive the trip out.

    Registration order matters and is the reason this sits above the
    add_middleware call below: Starlette treats the last-added middleware as
    the outermost, so this must be added first to end up *inside* CORS.
    """
    try:
        return await call_next(request)
    except Exception:
        logger.exception(
            "Unhandled error serving %s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    "The server hit an internal error. If this persists, check "
                    "/health -- a database that has gone unreachable presents "
                    "this way."
                )
            },
        )


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_origin_regex=_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# IBKR automated ingestion
# ---------------------------------------------------------------------------


class IngestResult(BaseModel):
    """Outcome of one ingest run, at each stage of the pipeline."""

    executions_parsed: int
    staged_new: int
    staged_duplicates: int
    trades_created: int
    trades_duplicates: int
    positions_matched: int
    symbols_touched: list[str]
    # Rows the statement carried that were not tradeable positions -- chiefly
    # currency conversions in a multi-currency account, which outnumbered the
    # real fills and would otherwise each become a position.
    skipped_non_tradeable: int
    # Queries that did not return this run -- IBKR rate-limits report
    # generation per token, and its cooldown outlasts a request. Reported
    # rather than swallowed so a partial sync never looks like a full one.
    queries_failed: list[str] = []
    # Fills the broker re-sent that the user had deliberately deleted. Surfaced
    # rather than silently dropped: a number that keeps climbing means the Flex
    # query is still returning something the journal does not want.
    suppressed_skipped: int = 0
    # True when at least one failed query was throttled rather than rejected.
    # The distinction is the whole point of showing it: a throttle clears on
    # its own and is worth retrying in a few minutes, a bad token never is.
    rate_limited: bool = False
    # Pre-trade plans this sync matched to the fills that finally arrived.
    # Worth its own line because it is the moment the two halves of the
    # journal meet: the plan you wrote, and what the broker actually did.
    plans_attached: int = 0


@app.post(
    "/api/ingest/ibkr",
    response_model=IngestResult,
    dependencies=[Depends(verify_clerk_token)],
)
async def ingest_ibkr(session: AsyncSession = Depends(get_session)):
    """Fetch, stage, promote, and match IBKR executions.

    Pipeline, each step idempotent:
      1. Flex handshake -> statement XML
      2. Parse into normalized executions
      3. Batch upsert into `ibkr_executions` (ON CONFLICT DO NOTHING on
         transaction_id) -- the absolute duplicate guard
      4. Promote only genuinely new rows into `trades`
      5. Re-run FIFO matching for each affected symbol
    """
    from services import ibkr_client, ibkr_parser  # noqa: PLC0415 - import cycle
    from services.matching_engine import (  # noqa: PLC0415
        UNCLASSIFIED_STYLE,
        run_matching_for_ticker,
    )

    # --- 1 & 2: fetch and parse -------------------------------------------
    try:
        statements, query_failures = await ibkr_client.fetch_statements()
    except ibkr_client.IBKRError as exc:
        # Transient "still compiling" conditions are a 503 so callers retry;
        # everything else is an upstream failure.
        raise HTTPException(status_code=503 if exc.retryable else 502, detail=str(exc))

    # Several queries can report the same fill -- a Trade Confirmation query
    # covers today, an Activity query covers history, and they overlap. Merge
    # on transaction_id here so the batch insert cannot conflict with itself;
    # the UNIQUE index still guards against overlap with earlier runs.
    merged: dict[str, object] = {}
    skipped_non_tradeable = 0
    for _query_id, root in statements:
        skipped_non_tradeable += ibkr_parser.count_non_tradeable(root)
        for execution in ibkr_parser.parse_statement(root):
            merged.setdefault(execution.transaction_id, execution)

    executions = list(merged.values())
    if not executions:
        return IngestResult(
            executions_parsed=0,
            staged_new=0,
            staged_duplicates=0,
            trades_created=0,
            trades_duplicates=0,
            positions_matched=0,
            symbols_touched=[],
            skipped_non_tradeable=skipped_non_tradeable,
            queries_failed=query_failures,
            rate_limited=any(
                ibkr_client.is_transient_failure(f) for f in query_failures
            ),
        )

    # --- 3: stage, skipping anything already seen -------------------------
    staging_rows = [ibkr_parser.to_staging_row(e) for e in executions]
    staged_stmt = (
        pg_insert(IBKRExecution)
        .values(staging_rows)
        .on_conflict_do_nothing(index_elements=["transaction_id"])
        .returning(IBKRExecution.transaction_id)
    )
    staged_ids = {row[0] for row in (await session.execute(staged_stmt)).fetchall()}

    # Only executions that were genuinely new to staging get promoted; the rest
    # were ingested on a previous run and already have a trades row.
    new_executions = [e for e in executions if e.transaction_id in staged_ids]

    # --- 4: promote into the trades ledger --------------------------------
    trade_rows = [
        {
            "id": uuid.uuid4(),
            # Namespaced so a manual entry can never collide with a broker fill.
            "ibkr_exec_id": f"IBKR-{e.transaction_id}"[:100],
            "ticker": e.symbol[:10],
            "direction": e.side,
            "style": UNCLASSIFIED_STYLE,
            "entry_date": e.execution_time or datetime.now(MARKET_TZ),
            # IBKR's execution price is the fill actually received.
            "actual_entry": e.price if e.price is not None else 0,
            # Side lives in `direction`; store magnitude only.
            "quantity": e.abs_quantity,
            "source_tag": "IBKR",
        }
        for e in new_executions
        if e.price is not None
    ]

    # Fills the user has deleted on purpose. ON CONFLICT DO NOTHING cannot
    # express this: it skips rows that still EXIST, and a deleted row does not,
    # so without this filter every sync would resurrect what was just removed
    # and the delete button would silently undo itself.
    suppressed = set(
        (await session.execute(select(SuppressedExecution.ibkr_exec_id))).scalars().all()
    )
    resurrected = 0
    if suppressed:
        before = len(trade_rows)
        trade_rows = [r for r in trade_rows if r["ibkr_exec_id"] not in suppressed]
        resurrected = before - len(trade_rows)

    created_ids: list[str] = []
    if trade_rows:
        trade_stmt = (
            pg_insert(Trade)
            .values(trade_rows)
            .on_conflict_do_nothing(index_elements=["ibkr_exec_id"])
            .returning(Trade.ibkr_exec_id)
        )
        created_ids = [row[0] for row in (await session.execute(trade_stmt)).fetchall()]

    # Mark staged rows processed so a later failure does not re-promote them.
    if staged_ids:
        await session.execute(
            update(IBKRExecution)
            .where(IBKRExecution.transaction_id.in_(staged_ids))
            .values(processed=True)
        )

    # --- 4b: match new fills against open plans ---------------------------
    # Before matching, so a position is built from trades that already carry
    # the stop and target their plan specified.
    plans_attached = await _auto_attach_plans(session, created_ids)

    await session.commit()

    # --- 5: re-run FIFO for every affected symbol -------------------------
    symbols = sorted({e.symbol for e in new_executions})
    positions_matched = 0
    for symbol in symbols:
        result = await run_matching_for_ticker(session, symbol, persist=True)
        positions_matched += len(result.positions)

    return IngestResult(
        executions_parsed=len(executions),
        staged_new=len(staged_ids),
        staged_duplicates=len(executions) - len(staged_ids),
        trades_created=len(created_ids),
        trades_duplicates=len(trade_rows) - len(created_ids),
        positions_matched=positions_matched,
        symbols_touched=symbols,
        skipped_non_tradeable=skipped_non_tradeable,
        queries_failed=query_failures,
        rate_limited=any(ibkr_client.is_transient_failure(f) for f in query_failures),
        suppressed_skipped=resurrected,
        plans_attached=plans_attached,
    )


# ---------------------------------------------------------------------------
# Repairing the execution ledger by hand
# ---------------------------------------------------------------------------
#
# This was general-purpose manual entry. It is now narrower on purpose: the
# only legitimate reason to type an execution into `trades` is that IBKR did
# not send one it should have, and you are looking at the position that is
# short a fill.
#
# Planning a trade no longer comes through here at all -- see /api/plans. That
# separation is the whole point: a plan and the broker's copy of the same trade
# used to be two rows in one table with no shared identifier, so they could
# never recognise each other, and syncing after hand-logging double-counted the
# position.
#
# The route keeps its `/manual` path deliberately. Renaming it would break the
# ledger's add-fill button during any window where the frontend and API deploy
# out of step, which buys nothing a docstring cannot say.

# Naive timestamps from the client are interpreted as US market time, matching
# how the analytics service buckets sessions.
MARKET_TZ = ZoneInfo("America/New_York")


class ManualTradeCreate(BaseModel):
    """One execution added by hand to repair a gap in the broker feed."""

    symbol: str = Field(..., min_length=1, max_length=10)
    side: str = Field(..., description="BUY or SELL")
    quantity: float = Field(..., gt=0)
    # The fill price actually received. Maps to trades.actual_entry (NOT NULL).
    price: float = Field(..., gt=0)
    # Omitted -> now in America/New_York. A naive value is read as market time.
    execution_time: Optional[datetime] = None

    # --- Planning / risk setup (all optional) ---------------------------
    # These map onto columns that already exist on the trades ledger; the two
    # renamed ones are noted so the mapping is obvious at the call site.
    planned_entry: Optional[float] = Field(None, gt=0)
    planned_stop_loss: Optional[float] = Field(None, gt=0)  # -> trades.stop_loss
    take_profit_price: Optional[float] = Field(None, gt=0)  # -> trades.target
    # Left blank while a trade is still running.
    exit_price: Optional[float] = Field(None, gt=0)

    # What the position-size calculator sized this trade against. Recorded per
    # trade rather than read back from app_settings at query time, because
    # account size drifts: a trade sized against $2,500 must keep reading as 1%
    # of $2,500 forever, not 1% of whatever the account holds today.
    #
    # risk_amount is what converts an R-multiple back into money, so a trade
    # missing it can be scored in R but never in dollars.
    #
    # Bound matches trades.risk_percent's NUMERIC(6,2) exactly, so an oversized
    # figure fails Pydantic validation with a 422 that names the field rather
    # than reaching the database and surfacing as an opaque 500.
    risk_percent: Optional[float] = Field(None, ge=0, le=9999.99)
    risk_amount: Optional[float] = Field(None, ge=0)

    # Which playbook entry this trade follows, and why it was taken.
    strategy_id: Optional[uuid.UUID] = None
    thesis: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("side")
    @classmethod
    def _valid_side(cls, value: str) -> str:
        side = value.strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be 'BUY' or 'SELL'")
        return side



class ManualTradeResult(BaseModel):
    trade_id: uuid.UUID
    ticker: str
    direction: str
    quantity: float
    price: float
    execution_time: datetime
    planned_entry: Optional[float]
    planned_stop_loss: Optional[float]
    take_profit_price: Optional[float]
    exit_price: Optional[float]
    # Round trips the FIFO engine closed as a result of this execution.
    positions_created: int
    open_quantity: float


@app.post(
    "/api/trades/manual",
    response_model=ManualTradeResult,
    status_code=201,
    dependencies=[Depends(verify_clerk_token)],
)
async def create_manual_trade(
    params: ManualTradeCreate, session: AsyncSession = Depends(get_session)
):
    """Add a fill the broker never sent, and re-run FIFO for its ticker.

    The execution lands in `trades` exactly like a synced fill, so the matching
    engine treats hand-added and broker-sourced fills identically. The REPAIR-
    prefix is the only thing that distinguishes them afterwards, which is why
    the ledger surfaces it: a hand-typed price is an assertion, and it should
    be visible as one next to figures the broker vouched for.
    """
    from services.matching_engine import (  # noqa: PLC0415 - avoids import cycle
        UNCLASSIFIED_STYLE,
        run_matching_for_ticker,
    )

    executed_at = params.execution_time or datetime.now(MARKET_TZ)
    if executed_at.tzinfo is None:
        # datetime-local inputs arrive without an offset; anchor to market time.
        executed_at = executed_at.replace(tzinfo=MARKET_TZ)

    trade = Trade(
        id=uuid.uuid4(),
        # Synthetic id, distinct from broker rows and satisfying the UNIQUE
        # constraint. The prefix is also load-bearing: migration 018 added a
        # CHECK permitting only IBKR- and REPAIR-, so this is what keeps the
        # row insertable at all.
        ibkr_exec_id=f"{EXEC_PREFIX_REPAIR}{uuid.uuid4()}",
        ticker=params.symbol,
        direction=params.side,
        style=UNCLASSIFIED_STYLE,
        entry_date=executed_at,
        # The form's price field is explicitly the fill actually received.
        actual_entry=params.price,
        quantity=Decimal(str(params.quantity)),
        # Planning fields map onto the ledger's existing columns; stop_loss and
        # target are the canonical homes for the planned stop and take-profit,
        # and are what PUT /api/trades/{id} reads and writes.
        planned_entry=params.planned_entry,
        stop_loss=params.planned_stop_loss,
        target=params.take_profit_price,
        exit_price=params.exit_price,
        # Omitting risk_percent does NOT leave it NULL -- the column carries
        # default=1.00, which SQLAlchemy applies whenever the value is None.
        # So it cannot distinguish "risked 1%" from "never sized"; 185 of the
        # imported rows read 1.00 for exactly that reason.
        #
        # risk_amount has no such default, which makes it the honest test for
        # "was this trade sized at all" -- and the one analytics should filter
        # on before converting R-multiples into money.
        risk_percent=params.risk_percent,
        risk_amount=params.risk_amount,
        strategy_id=params.strategy_id,
        thesis=params.thesis,
        source_tag="Repair",
    )
    session.add(trade)
    await session.commit()
    await session.refresh(trade)

    # Re-run matching for this ticker. The engine is idempotent (unique index
    # on the open/close execution pair), so already-matched round trips are not
    # duplicated -- only newly closable ones are written.
    result = await run_matching_for_ticker(session, params.symbol, persist=True)

    return ManualTradeResult(
        trade_id=trade.id,
        ticker=trade.ticker,
        direction=trade.direction,
        quantity=trade.quantity,
        price=float(trade.actual_entry),
        execution_time=trade.entry_date,
        planned_entry=float(trade.planned_entry) if trade.planned_entry is not None else None,
        planned_stop_loss=float(trade.stop_loss) if trade.stop_loss is not None else None,
        take_profit_price=float(trade.target) if trade.target is not None else None,
        exit_price=float(trade.exit_price) if trade.exit_price is not None else None,
        positions_created=len(result.positions),
        open_quantity=result.open_quantity,
    )


# ---------------------------------------------------------------------------
# Trade plans
# ---------------------------------------------------------------------------


def to_decimal(value: Optional[float]) -> Optional[Decimal]:
    """Money and quantities are Decimal in the database, float on the wire.

    Via str() rather than Decimal(float): Decimal(0.1) is 0.1000000000000000055,
    while Decimal("0.1") is exactly 0.1.
    """
    return Decimal(str(value)) if value is not None else None


# Journal fields a plan hands to the fill it attaches to. Keys are the plan's
# attribute, values are the trade's -- `take_profit` and `target` are the same
# idea under two names, which the ledger has always called `target`.
#
# Copied onto the trade rather than read through the FK on every query, for two
# reasons: every existing analytic already reads these columns and would
# otherwise need rewriting, and a plan later deleted would take the trade's
# recorded intent with it. The plan is frozen once attached, so the copy cannot
# drift from its source.
PLAN_TO_TRADE_FIELDS = {
    "planned_entry": "planned_entry",
    "stop_loss": "stop_loss",
    "take_profit": "target",
    "strategy_id": "strategy_id",
    "thesis": "thesis",
}


async def _opening_leg_fills(
    session: AsyncSession, trade: Trade, plan_id: Optional[uuid.UUID] = None
) -> list[Trade]:
    """Every fill belonging to the same entry as `trade`, earliest first.

    IBKR splits one order into several executions -- 49 of this account's 265
    ticker/side/day groups are multi-fill, one of them 11 fills deep -- so
    "the trade you entered" is usually several rows. Same ticker, same side,
    same market day is the boundary the ledger already groups on.

    Fills already claimed by a different plan are excluded, so attaching one
    plan can never quietly steal another's.
    """
    day_start = trade.entry_date.astimezone(MARKET_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    rows = (
        await session.execute(
            select(Trade)
            .where(
                Trade.ticker == trade.ticker,
                Trade.direction == trade.direction,
                Trade.entry_date >= day_start,
                Trade.entry_date < day_start + timedelta(days=1),
                or_(Trade.plan_id.is_(None), Trade.plan_id == plan_id),
            )
            .order_by(Trade.entry_date, Trade.id)
        )
    ).scalars().all()
    return list(rows)


def _plan_can_claim(
    plan: PlannedTrade,
    ticker: str,
    direction: Optional[str],
    executed_at: datetime,
) -> bool:
    """Whether an open plan is allowed to claim a fill that just arrived.

    Separated from the allocation loop because this rule, not the loop, is what
    decides whether an attachment is trustworthy.

    A plan must predate the fill: one written after a trade executed cannot be
    that trade's plan, whatever it says. And it must be recent -- without
    PLAN_ATTACH_MAX_AGE, a setup written months ago and never cancelled would
    silently claim the next fill on that ticker, and the mis-attribution would
    look exactly like the feature working.
    """
    return (
        plan.status == PLAN_OPEN
        and plan.ticker == ticker
        and (plan.direction or "").upper() == (direction or "").upper()
        and plan.created_at is not None
        and plan.created_at <= executed_at
        and executed_at - plan.created_at <= PLAN_ATTACH_MAX_AGE
    )


def _apply_plan_to_fills(plan: PlannedTrade, fills: Sequence[Trade]) -> list[str]:
    """Link a plan to its fills and hand its numbers to the earliest one.

    `plan_id` goes on every fill: that is the provenance link, and putting it
    everywhere keeps it correct no matter which fill FIFO later treats as the
    position's opener.

    The journal values go on the earliest fill ALONE. The ledger reads them
    from the opening trade, and prices copied across seven rows would be
    redundant -- but `risk_amount` would be actively wrong, because it is an
    absolute figure and anything that summed it would report seven times the
    risk actually taken.

    Existing values are never overwritten. A field already filled in was
    either edited by hand or copied by an earlier attach, and there is no way
    to tell those apart, so the safe reading is that it is yours.
    """
    if not fills:
        return []

    for fill in fills:
        fill.plan_id = plan.id

    anchor = fills[0]
    copied: list[str] = []

    for plan_attr, trade_attr in PLAN_TO_TRADE_FIELDS.items():
        value = getattr(plan, plan_attr)
        if value is not None and getattr(anchor, trade_attr) is None:
            setattr(anchor, trade_attr, value)
            copied.append(trade_attr)

    # risk_amount is the honest test for "was this sized at all": it has no
    # column default, unlike risk_percent, which silently reads 1.00 on every
    # row that never went near a calculator. So it gates both.
    if anchor.risk_amount is None and plan.risk_amount is not None:
        anchor.risk_amount = plan.risk_amount
        copied.append("risk_amount")
        if plan.risk_percent is not None:
            anchor.risk_percent = plan.risk_percent
            copied.append("risk_percent")

    plan.status = PLAN_ATTACHED
    plan.updated_at = datetime.now(timezone.utc)
    return copied


async def _auto_attach_plans(
    session: AsyncSession, created_exec_ids: Sequence[str]
) -> int:
    """Match freshly imported fills against open plans. Returns plans attached.

    Runs on the fills this sync actually created, never on the whole ledger:
    re-examining old fills would let a plan written today claim a trade from
    last month.

    Two rules keep a stale plan from claiming a fill it has nothing to do with:
    a plan must have been written BEFORE the fill executed -- a plan cannot
    describe a trade that already happened -- and it must be no older than
    PLAN_ATTACH_MAX_AGE, so a setup you wrote up and forgot stops competing.

    When several plans qualify, the most recent wins, and each is used once.
    Getting that choice wrong mislabels a trade; it cannot duplicate one,
    because the plan never becomes a row in `trades`. Quantity and P&L are
    correct either way, and the attachment is reversible from the journal.
    """
    if not created_exec_ids:
        return 0

    new_fills = (
        await session.execute(
            select(Trade)
            .where(Trade.ibkr_exec_id.in_(list(created_exec_ids)))
            .order_by(Trade.entry_date, Trade.id)
        )
    ).scalars().all()
    if not new_fills:
        return 0

    # Same grouping the ledger uses: one entry, however many executions the
    # broker split it into.
    groups: dict[tuple[str, str, object], list[Trade]] = {}
    for fill in new_fills:
        day = fill.entry_date.astimezone(MARKET_TZ).date()
        groups.setdefault((fill.ticker, fill.direction, day), []).append(fill)

    candidates = (
        await session.execute(
            select(PlannedTrade)
            .where(
                PlannedTrade.status == PLAN_OPEN,
                PlannedTrade.ticker.in_({t for t, _, _ in groups}),
            )
            .order_by(PlannedTrade.created_at.desc())
        )
    ).scalars().all()
    if not candidates:
        return 0

    # Allocated in Python rather than re-querying per group, so a plan claimed
    # by one group is not offered to the next before the flush lands.
    used: set[uuid.UUID] = set()
    attached = 0

    for (ticker, direction, _day), fills in sorted(
        groups.items(), key=lambda kv: kv[1][0].entry_date
    ):
        executed_at = fills[0].entry_date
        match = next(
            (
                plan
                for plan in candidates
                if plan.id not in used
                and _plan_can_claim(plan, ticker, direction, executed_at)
            ),
            None,
        )
        if match is None:
            continue

        _apply_plan_to_fills(match, fills)
        used.add(match.id)
        attached += 1

    return attached


class PlanCreate(BaseModel):
    """A trade you intend to take. Deliberately carries no fill price."""

    ticker: str = Field(..., min_length=1, max_length=10)
    direction: str = Field(..., description="BUY or SELL")

    # All optional: a plan is worth recording the moment you have a ticker and
    # a bias. Requiring a full price triangle is what pushed people into
    # inventing numbers to get the form to save.
    quantity: Optional[float] = Field(None, gt=0)
    planned_entry: Optional[float] = Field(None, gt=0)
    stop_loss: Optional[float] = Field(None, gt=0)
    take_profit: Optional[float] = Field(None, gt=0)

    # Bounds match planned_trades' NUMERIC widths, so an oversized figure fails
    # as a 422 naming the field rather than a 500 from the driver.
    risk_percent: Optional[float] = Field(None, ge=0, le=9999.99)
    risk_amount: Optional[float] = Field(None, ge=0)

    strategy_id: Optional[uuid.UUID] = None
    thesis: Optional[str] = None

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("direction")
    @classmethod
    def _valid_direction(cls, value: str) -> str:
        # BUY/SELL only, matching trades.direction. Accepting LONG/SHORT as
        # well would mean every query needed equivalence handling, and the
        # first one to forget would silently miss half the rows.
        raw = value.strip().upper()
        alias = {"LONG": "BUY", "SHORT": "SELL"}.get(raw, raw)
        if alias not in {"BUY", "SELL"}:
            raise ValueError("direction must be BUY or SELL (LONG/SHORT accepted)")
        return alias


class PlanUpdate(BaseModel):
    """A partial edit. Only keys actually present are applied."""

    ticker: Optional[str] = Field(None, min_length=1, max_length=10)
    direction: Optional[str] = None
    quantity: Optional[float] = Field(None, gt=0)
    planned_entry: Optional[float] = Field(None, gt=0)
    stop_loss: Optional[float] = Field(None, gt=0)
    take_profit: Optional[float] = Field(None, gt=0)
    risk_percent: Optional[float] = Field(None, ge=0, le=9999.99)
    risk_amount: Optional[float] = Field(None, ge=0)
    strategy_id: Optional[uuid.UUID] = None
    thesis: Optional[str] = None
    status: Optional[str] = None

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, value: Optional[str]) -> Optional[str]:
        return value.strip().upper() if value else value

    @field_validator("direction")
    @classmethod
    def _valid_direction(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        raw = value.strip().upper()
        alias = {"LONG": "BUY", "SHORT": "SELL"}.get(raw, raw)
        if alias not in {"BUY", "SELL"}:
            raise ValueError("direction must be BUY or SELL (LONG/SHORT accepted)")
        return alias

    @field_validator("status")
    @classmethod
    def _valid_status(cls, value: Optional[str]) -> Optional[str]:
        """Only the two statuses a client may actually choose.

        ATTACHED is deliberately absent: it is reached by attaching to a real
        fill, never by assertion, or a plan could claim a trade that does not
        point back at it. Refused here rather than at the endpoint so the
        message names the reason instead of failing a CHECK constraint.
        """
        if value is None:
            return None
        status = value.strip().upper()
        settable = {PLAN_OPEN, PLAN_CANCELLED}
        if status not in settable:
            if status == PLAN_ATTACHED:
                raise ValueError(
                    "attach a plan to a trade instead of setting its status to "
                    f"{PLAN_ATTACHED}"
                )
            raise ValueError(f"status must be one of {PLAN_OPEN}, {PLAN_CANCELLED}")
        return status


class PlanOut(BaseModel):
    id: uuid.UUID
    ticker: str
    direction: str
    quantity: Optional[float] = None
    planned_entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    # Computed by Postgres from the three prices above; never sent by a client.
    planned_r: Optional[float] = None
    risk_percent: Optional[float] = None
    risk_amount: Optional[float] = None
    strategy_id: Optional[uuid.UUID] = None
    thesis: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Fills this plan ended up attached to. Derived rather than stored: one
    # plan covers every fill of its opening leg, so a single id column could
    # not represent it, and a second copy of the link would be free to
    # disagree with trades.plan_id.
    attached_trade_ids: list[uuid.UUID] = []


def _plan_out(plan: PlannedTrade, attached_ids: Sequence[uuid.UUID] = ()) -> PlanOut:
    """Serialise a plan, converting Decimal to float at the boundary."""
    def num(value) -> Optional[float]:
        return float(value) if value is not None else None

    return PlanOut(
        id=plan.id,
        ticker=plan.ticker,
        direction=plan.direction,
        quantity=num(plan.quantity),
        planned_entry=num(plan.planned_entry),
        stop_loss=num(plan.stop_loss),
        take_profit=num(plan.take_profit),
        planned_r=num(plan.planned_r),
        risk_percent=num(plan.risk_percent),
        risk_amount=num(plan.risk_amount),
        strategy_id=plan.strategy_id,
        thesis=plan.thesis,
        status=plan.status,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        attached_trade_ids=list(attached_ids),
    )


async def _attached_ids_by_plan(
    session: AsyncSession, plan_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Reverse of trades.plan_id, in one query rather than one per plan."""
    if not plan_ids:
        return {}
    rows = (
        await session.execute(
            select(Trade.plan_id, Trade.id).where(Trade.plan_id.in_(plan_ids))
        )
    ).all()
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for plan_id, trade_id in rows:
        out.setdefault(plan_id, []).append(trade_id)
    return out


@app.get(
    "/api/plans",
    response_model=list[PlanOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_plans(
    status: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """Trade plans, newest first.

    Defaults to OPEN because that is the only status that is actionable --
    the dock exists to show what you are still waiting to be filled on.
    Pass `status=ALL` to see cancelled and attached plans too.
    """
    wanted = (status or PLAN_OPEN).strip().upper()

    stmt = select(PlannedTrade).order_by(PlannedTrade.created_at.desc())
    if wanted != "ALL":
        if wanted not in {PLAN_OPEN, PLAN_ATTACHED, PLAN_CANCELLED}:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"status must be one of {PLAN_OPEN}, {PLAN_ATTACHED}, "
                    f"{PLAN_CANCELLED}, ALL"
                ),
            )
        stmt = stmt.where(PlannedTrade.status == wanted)

    plans = (await session.execute(stmt)).scalars().all()
    attached = await _attached_ids_by_plan(session, [p.id for p in plans])
    return [_plan_out(p, attached.get(p.id, [])) for p in plans]


@app.post(
    "/api/plans",
    response_model=PlanOut,
    status_code=201,
    dependencies=[Depends(verify_clerk_token)],
)
async def create_plan(
    params: PlanCreate, session: AsyncSession = Depends(get_session)
):
    """Record a trade you intend to take.

    Writes to `planned_trades` and nowhere else. No row reaches `trades`, so
    nothing here moves P&L, win rate, exposure or any other statistic until a
    real fill arrives and the plan attaches to it.

    `planned_r` is not accepted or computed here -- Postgres generates it from
    entry, stop and target, so it cannot drift out of step with them.
    """
    plan = PlannedTrade(
        id=uuid.uuid4(),
        ticker=params.ticker,
        direction=params.direction,
        quantity=to_decimal(params.quantity),
        planned_entry=to_decimal(params.planned_entry),
        stop_loss=to_decimal(params.stop_loss),
        take_profit=to_decimal(params.take_profit),
        risk_percent=to_decimal(params.risk_percent),
        risk_amount=to_decimal(params.risk_amount),
        strategy_id=params.strategy_id,
        thesis=params.thesis,
        status=PLAN_OPEN,
    )
    session.add(plan)
    await session.commit()
    # planned_r and the timestamps were produced by the database; re-read
    # rather than report what we sent, which did not include them.
    await session.refresh(plan)
    return _plan_out(plan)


@app.patch(
    "/api/plans/{plan_id}",
    response_model=PlanOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def update_plan(
    plan_id: uuid.UUID,
    params: PlanUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Edit a plan that has not been attached yet.

    An ATTACHED plan is frozen. Once a fill has copied the plan's numbers onto
    itself, editing the plan would leave the two disagreeing about what was
    intended, with nothing to say which came first. Detach it if the
    attachment was wrong -- that is the operation that makes it editable
    again, and it says so.
    """
    plan = await session.get(PlannedTrade, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")

    changes = params.model_dump(exclude_unset=True)

    if plan.status == PLAN_ATTACHED and set(changes) - {"status"}:
        raise HTTPException(
            status_code=409,
            detail=(
                "This plan is attached to a filled trade and cannot be edited. "
                "Detach it first, or edit the trade itself in the journal."
            ),
        )

    # ATTACHED is reached by attaching, never by asserting it. Allowing it here
    # would let a plan claim a fill that does not point back at it.
    if changes.get("status") == PLAN_ATTACHED:
        raise HTTPException(
            status_code=422,
            detail="Attach a plan to a trade instead of setting its status directly.",
        )

    decimal_fields = {
        "quantity", "planned_entry", "stop_loss", "take_profit",
        "risk_percent", "risk_amount",
    }
    for key, value in changes.items():
        setattr(plan, key, to_decimal(value) if key in decimal_fields else value)

    plan.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(plan)

    attached = await _attached_ids_by_plan(session, [plan.id])
    return _plan_out(plan, attached.get(plan.id, []))


@app.delete(
    "/api/plans/{plan_id}",
    response_model=PlanOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def cancel_plan(
    plan_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    """Cancel a plan you did not take.

    Marked CANCELLED rather than deleted. The setups you talked yourself out
    of are evidence about your process, and a row that vanishes takes that
    with it -- but a cancelled plan must stop competing for incoming fills,
    which the status change is what accomplishes.
    """
    plan = await session.get(PlannedTrade, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")

    if plan.status == PLAN_ATTACHED:
        raise HTTPException(
            status_code=409,
            detail=(
                "This plan is attached to a filled trade. Detach it first if "
                "the attachment was wrong."
            ),
        )

    plan.status = PLAN_CANCELLED
    plan.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(plan)
    return _plan_out(plan)


class PlanAttachResult(BaseModel):
    plan_id: uuid.UUID
    ticker: str
    # Every fill the plan now covers, not just the one named in the request:
    # attaching to a single fill of a multi-fill position would describe a
    # fraction of the trade and read as though the rest were unplanned.
    trade_ids: list[uuid.UUID]
    fields_copied: list[str]
    status: str


@app.post(
    "/api/trades/{trade_id}/attach-plan",
    response_model=PlanAttachResult,
    dependencies=[Depends(verify_clerk_token)],
)
async def attach_plan(
    trade_id: uuid.UUID,
    plan_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Link an open plan to a fill the sync did not match automatically.

    Attaches to every fill in the same opening leg, not only the one named --
    IBKR splits an order into several executions, and a plan describes the
    position rather than one slice of it.
    """
    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found.")

    plan = await session.get(PlannedTrade, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if plan.status == PLAN_ATTACHED:
        raise HTTPException(
            status_code=409, detail="That plan is already attached to a trade."
        )
    if plan.ticker != trade.ticker:
        raise HTTPException(
            status_code=422,
            detail=f"Plan is for {plan.ticker}, but this trade is {trade.ticker}.",
        )
    if (plan.direction or "").upper() != (trade.direction or "").upper():
        raise HTTPException(
            status_code=422,
            detail=(
                f"Plan is a {plan.direction} and this fill is a {trade.direction}."
            ),
        )

    fills = await _opening_leg_fills(session, trade, plan_id=plan.id)
    copied = _apply_plan_to_fills(plan, fills)
    await session.commit()

    attached = await _attached_ids_by_plan(session, [plan.id])
    return PlanAttachResult(
        plan_id=plan.id,
        ticker=plan.ticker,
        trade_ids=attached.get(plan.id, []),
        fields_copied=copied,
        status=plan.status,
    )


class PlanDetachResult(BaseModel):
    plan_id: uuid.UUID
    ticker: str
    trades_unlinked: int
    status: str


@app.post(
    "/api/trades/{trade_id}/detach-plan",
    response_model=PlanDetachResult,
    dependencies=[Depends(verify_clerk_token)],
)
async def detach_plan(
    trade_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    """Unlink a wrongly attached plan and make it available again.

    The values the plan copied onto the trade are deliberately left in place.
    They may have been edited since, and there is no way to tell an untouched
    copy from a corrected one -- so clearing them could silently discard your
    own work. The plan returns to OPEN and can attach elsewhere.
    """
    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found.")
    if trade.plan_id is None:
        raise HTTPException(status_code=404, detail="This trade has no plan attached.")

    plan = await session.get(PlannedTrade, trade.plan_id)

    result = await session.execute(
        update(Trade)
        .where(Trade.plan_id == trade.plan_id)
        .values(plan_id=None)
        .returning(Trade.id)
    )
    unlinked = len(result.fetchall())

    if plan is not None:
        plan.status = PLAN_OPEN
        plan.updated_at = datetime.now(timezone.utc)

    await session.commit()

    return PlanDetachResult(
        plan_id=plan.id if plan else trade_id,
        ticker=plan.ticker if plan else (trade.ticker or ""),
        trades_unlinked=unlinked,
        status=plan.status if plan else PLAN_OPEN,
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class SettingsOut(BaseModel):
    account_size: Optional[float]
    risk_percent: float
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


class SettingsUpdate(BaseModel):
    """Partial update. Only keys present in the body are applied.

    account_size is Optional and settable to null, which is a real state --
    "I no longer want a remembered account size" -- rather than a no-op. That
    makes `model_fields_set` the authority on what to write, not truthiness.
    """

    account_size: Optional[float] = Field(None, ge=0)
    # app_settings.risk_percent is NUMERIC(5,2); this is a default to start from
    # rather than a recorded outcome, so a tighter bound than the ledger's is
    # correct -- nobody sets a default risk of 900%.
    risk_percent: Optional[float] = Field(None, ge=0, le=999.99)


async def _get_or_create_settings(session: AsyncSession) -> AppSetting:
    """The single settings row, created on first read if the seed never ran.

    Migration 015 seeds it, but a database restored from a schema-only dump
    would have the table and no row. Creating it here keeps the endpoint total
    rather than 404-ing on a condition the user cannot act on.
    """
    row = await session.get(AppSetting, AppSetting.SINGLETON_ID)
    if row is None:
        row = AppSetting(id=AppSetting.SINGLETON_ID, risk_percent=Decimal("1.00"))
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


@app.get(
    "/api/settings",
    response_model=SettingsOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def get_settings(session: AsyncSession = Depends(get_session)):
    return SettingsOut.model_validate(await _get_or_create_settings(session))


@app.put(
    "/api/settings",
    response_model=SettingsOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def update_settings(
    params: SettingsUpdate, session: AsyncSession = Depends(get_session)
):
    row = await _get_or_create_settings(session)
    for key in params.model_fields_set:
        value = getattr(params, key)
        # account_size is nullable and may be cleared; risk_percent is NOT NULL,
        # so an explicit null there is rejected rather than allowed through to
        # become an integrity error the client cannot interpret.
        if value is None and key != "account_size":
            raise HTTPException(status_code=422, detail=f"{key} cannot be null.")
        setattr(row, key, Decimal(str(value)) if value is not None else None)
    await session.commit()
    await session.refresh(row)
    return SettingsOut.model_validate(row)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


class StrategyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    # Playbook fields default to '' so a strategy can be created from just a
    # name and filled in later from the editor.
    method: str = ""
    entry_criteria: str = ""
    exit_criteria: str = ""


class StrategyUpdate(BaseModel):
    """Partial update from the strategy editor.

    Every field is optional; only keys present in the request body are
    applied, so saving one pane never clears another.
    """

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    method: Optional[str] = None
    entry_criteria: Optional[str] = None
    exit_criteria: Optional[str] = None


class StrategyUsage(BaseModel):
    """How much history is riding on one playbook entry.

    Sent with every strategy so the delete affordance can state the stakes
    before it is pressed, rather than after. `positions` is counted separately
    from `trades` because they are different grains -- a round trip versus the
    individual fills behind it -- and summing them would report one trade
    twice.
    """

    trades: int = 0
    positions: int = 0
    plans: int = 0

    @property
    def total(self) -> int:
        return self.trades + self.positions + self.plans


class StrategyOut(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    # Rows created before migration 003 could still read back NULL; coerced to
    # '' below so the client always receives a string.
    method: str = ""
    entry_criteria: str = ""
    exit_criteria: str = ""
    created_at: Optional[datetime]
    # Absent on create/update responses, where nothing has had a chance to
    # reference the row yet. Only the list endpoint fills it in.
    usage: Optional[StrategyUsage] = None

    @field_validator("method", "entry_criteria", "exit_criteria", mode="before")
    @classmethod
    def _null_to_empty(cls, value: Optional[str]) -> str:
        return value or ""

    class Config:
        from_attributes = True


async def _strategy_usage(session: AsyncSession) -> dict[uuid.UUID, StrategyUsage]:
    """Reference counts per strategy, in three grouped queries rather than 3N.

    Counting per strategy in a loop would issue a query per playbook entry on
    every page load, for a number the UI shows on every row.
    """
    usage: dict[uuid.UUID, StrategyUsage] = {}

    for model, field in (
        (Trade, "trades"),
        (Position, "positions"),
        (PlannedTrade, "plans"),
    ):
        rows = (
            await session.execute(
                select(model.strategy_id, func.count())
                .where(model.strategy_id.is_not(None))
                .group_by(model.strategy_id)
            )
        ).all()
        for strategy_id, count in rows:
            entry = usage.setdefault(strategy_id, StrategyUsage())
            setattr(entry, field, count)

    return usage


@app.get(
    "/api/strategies",
    response_model=list[StrategyOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_strategies(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Strategy).order_by(Strategy.name))
    usage = await _strategy_usage(session)
    out: list[StrategyOut] = []
    for strategy in result.scalars().all():
        item = StrategyOut.model_validate(strategy)
        item.usage = usage.get(strategy.id, StrategyUsage())
        out.append(item)
    return out


@app.post(
    "/api/strategies",
    response_model=StrategyOut,
    status_code=201,
    dependencies=[Depends(verify_clerk_token)],
)
async def create_strategy(
    params: StrategyCreate, session: AsyncSession = Depends(get_session)
):
    strategy = Strategy(
        name=params.name,
        description=params.description,
        method=params.method,
        entry_criteria=params.entry_criteria,
        exit_criteria=params.exit_criteria,
    )
    session.add(strategy)
    try:
        await session.commit()
    except IntegrityError:
        # strategies.name carries a UNIQUE constraint.
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"A strategy named '{params.name}' already exists"
        )
    await session.refresh(strategy)
    return StrategyOut.model_validate(strategy)


@app.patch(
    "/api/strategies/{strategy_id}",
    response_model=StrategyOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def update_strategy(
    strategy_id: uuid.UUID,
    params: StrategyUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Save edits from the strategy playbook editor."""
    strategy = await session.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    for field, value in params.model_dump(exclude_unset=True).items():
        setattr(strategy, field, value)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"A strategy named '{params.name}' already exists"
        )
    await session.refresh(strategy)
    return StrategyOut.model_validate(strategy)


class StrategyDeleteResult(BaseModel):
    """What a deletion moved before it removed anything."""

    deleted_id: uuid.UUID
    deleted_name: str
    reassigned_to_id: Optional[uuid.UUID] = None
    reassigned_to_name: Optional[str] = None
    trades_reassigned: int = 0
    positions_reassigned: int = 0
    plans_reassigned: int = 0


@app.delete(
    "/api/strategies/{strategy_id}",
    response_model=StrategyDeleteResult,
    dependencies=[Depends(verify_clerk_token)],
)
async def delete_strategy(
    strategy_id: uuid.UUID,
    reassign_to: Optional[uuid.UUID] = None,
    session: AsyncSession = Depends(get_session),
):
    """Remove a playbook entry, moving its history to another entry first.

    The three FKs pointing here are all ON DELETE SET NULL, so the database
    would happily accept a bare delete -- no trade would be lost. What would be
    lost is the *attribution*: every trade tagged with this strategy would fall
    into the "Unassigned" bucket in the analytics breakdown, and there is no
    record afterwards of which setup they belonged to. On this account one
    playbook entry carries 76 of 123 closed trades, so that is a real
    analysis destroyed by a single click.

    Hence `reassign_to` is mandatory whenever anything references the strategy.
    An unused entry deletes outright -- there is nothing to preserve.

    The moves and the delete share one transaction: a reassignment that
    committed without the delete would leave two strategies looking identical,
    and a delete that committed without the reassignment is the silent-NULL
    outcome this endpoint exists to prevent.
    """
    strategy = await session.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")

    usage = (await _strategy_usage(session)).get(strategy_id, StrategyUsage())

    if usage.total == 0:
        name = strategy.name
        await session.delete(strategy)
        await session.commit()
        return StrategyDeleteResult(deleted_id=strategy_id, deleted_name=name)

    if reassign_to is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{strategy.name}' is used by {usage.trades} trade(s), "
                f"{usage.positions} round trip(s) and {usage.plans} plan(s). "
                "Choose a strategy to reassign them to before deleting."
            ),
        )

    if reassign_to == strategy_id:
        raise HTTPException(
            status_code=422,
            detail="Cannot reassign a strategy to itself.",
        )

    target = await session.get(Strategy, reassign_to)
    if target is None:
        raise HTTPException(
            status_code=404, detail="The strategy to reassign to was not found."
        )

    moved: dict[str, int] = {}
    for model, key in (
        (Trade, "trades"),
        (Position, "positions"),
        (PlannedTrade, "plans"),
    ):
        result = await session.execute(
            update(model)
            .where(model.strategy_id == strategy_id)
            .values(strategy_id=reassign_to)
        )
        moved[key] = result.rowcount or 0

    name = strategy.name
    target_name = target.name
    await session.delete(strategy)
    await session.commit()

    return StrategyDeleteResult(
        deleted_id=strategy_id,
        deleted_name=name,
        reassigned_to_id=reassign_to,
        reassigned_to_name=target_name,
        trades_reassigned=moved["trades"],
        positions_reassigned=moved["positions"],
        plans_reassigned=moved["plans"],
    )


# ---------------------------------------------------------------------------
# Disciplines
# ---------------------------------------------------------------------------


class DisciplineCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("Discipline name cannot be empty")
        return trimmed


class DisciplineOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


DEFAULT_DISCIPLINES = [
    "Hard stop-loss set",
    "Waited for retest",
    "Followed the plan",
]


@app.get(
    "/api/disciplines",
    response_model=list[DisciplineOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_disciplines(session: AsyncSession = Depends(get_session)):
    """List all discipline rules, oldest first.

    Seeding moved out of the read path and into the migration. A GET that
    writes is surprising on its own, and this one would resurrect the default
    rules every time the user deleted all of them -- the list emptying is a
    deliberate act, not a state to be repaired.

    Only a missing table is swallowed, and only so a deployment that has not
    yet had migration 013/014 applied degrades to an empty checklist instead
    of a broken review panel. Every other error propagates: catching bare
    Exception here would render a dropped connection or a timeout as "you have
    no discipline rules", which is indistinguishable from the truth and is the
    same failure that once made a dead dashboard read as a flat 0% win rate.
    """
    try:
        result = await session.execute(
            select(Discipline).order_by(Discipline.created_at.asc())
        )
    except ProgrammingError as exc:
        # asyncpg raises UndefinedTableError, which SQLAlchemy wraps.
        if not isinstance(getattr(exc, "orig", None), UndefinedTableError):
            raise
        logger.warning("disciplines table is missing; apply migration 013/014")
        await session.rollback()
        return []

    return [DisciplineOut.model_validate(d) for d in result.scalars().all()]


@app.post(
    "/api/disciplines",
    response_model=DisciplineOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_clerk_token)],
)
async def create_discipline(
    params: DisciplineCreate,
    session: AsyncSession = Depends(get_session),
):
    """Add a new discipline rule."""
    discipline = Discipline(name=params.name)
    session.add(discipline)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A discipline rule named '{params.name}' already exists",
        )
    except ProgrammingError as exc:
        await session.rollback()
        if not isinstance(getattr(exc, "orig", None), UndefinedTableError):
            raise
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The disciplines table does not exist. Apply migration "
                "014_position_disciplines.sql to this database."
            ),
        )
    await session.refresh(discipline)
    return DisciplineOut.model_validate(discipline)


@app.delete(
    "/api/disciplines/{discipline_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(verify_clerk_token)],
)
async def delete_discipline(
    discipline_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Delete a discipline rule."""
    discipline = await session.get(Discipline, discipline_id)
    if discipline is None:
        raise HTTPException(status_code=404, detail="Discipline not found")

    await session.delete(discipline)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Position review (Trade Inbox)
# ---------------------------------------------------------------------------


class PositionReviewUpdate(BaseModel):
    """Review payload for a round trip.

    Covers both surfaces that review a position: the Trade Inbox checklist
    (strategy / discipline tags / grade) and the Analytics drawer (notes and
    behavioural mistake tags). Every field is optional and only keys present
    in the request are applied, so one surface never clears the other's work.
    """

    strategy_id: Optional[uuid.UUID] = None
    tag_hard_sl: Optional[bool] = None
    tag_retest: Optional[bool] = None
    tag_plan_compliant: Optional[bool] = None
    trade_grade: Optional[str] = Field(None, max_length=5)
    notes: Optional[str] = None
    # Behavioural tags, e.g. ['FOMO', 'Chased', 'Early Liquidation'].
    mistakes: Optional[list[str]] = None

    # The post-mortem, asked as three separate questions (migration 011).
    review_went_well: Optional[str] = None
    review_went_wrong: Optional[str] = None
    review_lessons: Optional[str] = None

    # How it ended, and the two families of hindsight levels (migration 012).
    exit_reason: Optional[str] = None
    ideal_entry: Optional[float] = Field(None, gt=0)
    ideal_stop: Optional[float] = Field(None, gt=0)
    ideal_target: Optional[float] = Field(None, gt=0)
    revised_entry: Optional[float] = Field(None, gt=0)
    revised_stop: Optional[float] = Field(None, gt=0)
    revised_target: Optional[float] = Field(None, gt=0)

    # Answers to the user's own discipline rules (migration 014), keyed by
    # discipline id. Omitting the field leaves existing answers untouched;
    # including a rule with false records "reviewed, did not follow", which is
    # a different statement from leaving it out.
    disciplines: Optional[dict[uuid.UUID, bool]] = None


class PositionDisciplineOut(BaseModel):
    """One rule's answer for one round trip.

    Carries `name` alongside the id so a caller can render the checklist
    without a second lookup, and so a historical answer stays readable if the
    rule is later renamed.
    """

    discipline_id: uuid.UUID
    name: str
    followed: bool


class PositionOut(BaseModel):
    id: uuid.UUID
    symbol: str
    style: str
    quantity: float
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    realized_pnl: float
    strategy_id: Optional[uuid.UUID]
    review_status: Optional[str]
    tag_hard_sl: Optional[bool]
    tag_retest: Optional[bool]
    tag_plan_compliant: Optional[bool]
    trade_grade: Optional[str]
    notes: Optional[str]
    mistakes: list[str] = []
    review_went_well: Optional[str] = None
    review_went_wrong: Optional[str] = None
    review_lessons: Optional[str] = None
    exit_reason: Optional[str] = None
    ideal_entry: Optional[float] = None
    ideal_stop: Optional[float] = None
    ideal_target: Optional[float] = None
    revised_entry: Optional[float] = None
    revised_stop: Optional[float] = None
    revised_target: Optional[float] = None
    # Answers to the user's own rules (migration 014). Only rules actually
    # answered appear; a rule missing here is unreviewed, not unfollowed.
    disciplines: list[PositionDisciplineOut] = []
    created_at: Optional[datetime]

    @field_validator("mistakes", mode="before")
    @classmethod
    def _null_to_list(cls, value: Optional[list[str]]) -> list[str]:
        return list(value or [])

    class Config:
        from_attributes = True


async def _disciplines_by_position(
    session: AsyncSession, position_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[PositionDisciplineOut]]:
    """Discipline answers for many positions in one query, not one per row."""
    if not position_ids:
        return {}

    rows = (
        await session.execute(
            select(
                PositionDiscipline.position_id,
                PositionDiscipline.discipline_id,
                PositionDiscipline.followed,
                Discipline.name,
            )
            .join(Discipline, Discipline.id == PositionDiscipline.discipline_id)
            .where(PositionDiscipline.position_id.in_(position_ids))
            .order_by(Discipline.created_at)
        )
    ).all()

    grouped: dict[uuid.UUID, list[PositionDisciplineOut]] = {}
    for position_id, discipline_id, followed, name in rows:
        grouped.setdefault(position_id, []).append(
            PositionDisciplineOut(
                discipline_id=discipline_id, name=name, followed=followed
            )
        )
    return grouped


async def _position_out(session: AsyncSession, position: Position) -> PositionOut:
    """Serialize one position with its discipline answers attached."""
    out = PositionOut.model_validate(position)
    out.disciplines = (await _disciplines_by_position(session, [position.id])).get(
        position.id, []
    )
    return out


class PositionFillOut(BaseModel):
    """One execution behind a round trip.

    `quantity` is the share count attributed to this position, which is not
    always the fill's full size: an oversell closes one position and opens the
    next with a single execution.
    """

    id: uuid.UUID
    trade_id: uuid.UUID
    role: str  # OPEN | CLOSE
    quantity: float
    price: float
    executed_at: datetime

    class Config:
        from_attributes = True


@app.get("/health")
async def health(session: AsyncSession = Depends(get_session)):
    """Liveness and database reachability.

    Deliberately unauthenticated, and deliberately free of any trade data: it
    reports *which* database this deployment is talking to, not what is in it.

    The absence of this endpoint made a whole class of problem undiagnosable.
    A deployment pointed at the wrong database serves perfectly valid empty
    responses, which look exactly like an account that has never traded --
    from outside, the two are indistinguishable. The project ref settles it.
    """
    database = "ok"
    database_error: Optional[str] = None
    try:
        await session.execute(select(1))
    except Exception as exc:  # noqa: BLE001 - health must never itself 500
        logger.error("Health check could not reach the database: %s", exc)
        database = "unreachable"
        # The exception *class* names the fault exactly -- InvalidPasswordError
        # is a stale credential, gaierror is a wrong host, TimeoutError is a
        # network path -- and unlike the message it cannot carry the
        # connection string, so it is safe on an unauthenticated endpoint.
        database_error = type(exc).__name__

    # Structure only, never the password. Parsed with urlsplit rather than a
    # regex: the regex read `postgres\.([a-z0-9]+)` off the *whole* string and
    # so could match text that was never the username at all, reporting a
    # confident wrong ref for a malformed URL -- the failure mode this endpoint
    # exists to rule out. Reading the username field cannot make that mistake.
    host = ""
    project_ref: Optional[str] = None
    try:
        parts = urlsplit(DATABASE_URL)
        # rpartition drops any user:password prefix without needing to parse it.
        host = parts.netloc.rpartition("@")[2]
        project_ref = (parts.username or "").partition(".")[2] or None
    except ValueError as exc:
        logger.error("DATABASE_URL could not be parsed as a URL: %s", type(exc).__name__)

    return {
        "status": "ok",
        "database": database,
        "database_error": database_error,
        "database_host": host,
        "supabase_project_ref": project_ref,
    }


class TradeOut(BaseModel):
    """One execution in the ledger, as the master list shows it."""

    id: uuid.UUID
    ticker: str
    direction: str
    quantity: float
    actual_entry: float
    exit_price: Optional[float]
    entry_date: datetime
    style: str
    source_tag: Optional[str]
    strategy_id: Optional[uuid.UUID]
    thesis: Optional[str]
    planned_entry: Optional[float]
    stop_loss: Optional[float]
    actual_stop_loss: Optional[float] = None
    target: Optional[float]
    risk_percent: Optional[float] = None
    risk_amount: Optional[float] = None
    conviction: Optional[int] = None
    emotional_state: Optional[str] = None
    # True once FIFO matching has folded this fill into a closed round trip.
    # A fill with no counterpart is an open position, which is precisely what
    # the positions list cannot show -- and why a hand-logged buy appeared to
    # vanish before this endpoint existed.
    is_matched: bool
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


class TradeAnnotationUpdate(BaseModel):
    """Annotate an execution after the fact.

    Only keys present are applied, so setting a strategy never clears a thesis.
    """

    strategy_id: Optional[uuid.UUID] = None
    thesis: Optional[str] = None

    # The plan. Carried by the opening execution of a round trip, which is the
    # only place a still-open trade can hold one -- `positions` rows do not
    # exist until the trade closes.
    planned_entry: Optional[float] = Field(None, gt=0)
    stop_loss: Optional[float] = Field(None, gt=0)
    actual_stop_loss: Optional[float] = Field(None, gt=0)
    target: Optional[float] = Field(None, gt=0)
    risk_percent: Optional[float] = Field(None, ge=0)
    risk_amount: Optional[float] = Field(None, ge=0)
    conviction: Optional[int] = Field(None, ge=1, le=5)
    emotional_state: Optional[str] = None


@app.get(
    "/api/trades",
    response_model=list[TradeOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_trades(
    ticker: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """Every execution, newest first -- the master list.

    `positions` only ever contains *closed* round trips, so a buy that has not
    been sold has no row there and was invisible everywhere in the app. This is
    the ledger view: open and closed alike, each carrying its own thesis.
    """
    stmt = select(Trade).order_by(Trade.entry_date.desc())
    if ticker:
        stmt = stmt.where(Trade.ticker == ticker.strip().upper())
    trades = (await session.execute(stmt)).scalars().all()

    # One query for the whole ledger rather than a lookup per row.
    matched = (await session.execute(select(PositionFill.trade_id))).scalars().all()
    matched_ids = set(matched)

    return [
        TradeOut(
            **{c.name: getattr(trade, c.name) for c in Trade.__table__.columns
               if c.name in TradeOut.model_fields},
            is_matched=trade.id in matched_ids,
        )
        for trade in trades
    ]


@app.patch(
    "/api/trades/{trade_id}",
    response_model=TradeOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def annotate_trade(
    trade_id: uuid.UUID,
    params: TradeAnnotationUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Attach a strategy or thesis to an execution already in the ledger.

    Synced fills arrive with neither -- IBKR does not know why you traded --
    so the reasoning has to be attachable after import.
    """
    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    for field, value in params.model_dump(exclude_unset=True).items():
        setattr(trade, field, value)

    await session.commit()
    await session.refresh(trade)

    matched = await session.execute(
        select(PositionFill.id).where(PositionFill.trade_id == trade_id).limit(1)
    )
    return TradeOut(
        **{c.name: getattr(trade, c.name) for c in Trade.__table__.columns
           if c.name in TradeOut.model_fields},
        is_matched=matched.first() is not None,
    )


class ExecutionUpdate(BaseModel):
    """Correct the facts of a fill: what, how much, at what price, when.

    Separate from TradeAnnotationUpdate, which deliberately locks these fields.
    Annotation records what you THOUGHT; this records what HAPPENED, and the
    two have different consequences -- changing a quantity re-runs FIFO and can
    dissolve or create round trips, which no annotation ever does.
    """

    direction: Optional[str] = None
    quantity: Optional[float] = Field(None, gt=0)
    price: Optional[float] = Field(None, gt=0)  # -> trades.actual_entry
    execution_time: Optional[datetime] = None

    @field_validator("direction")
    @classmethod
    def _valid_side(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        side = value.strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("direction must be 'BUY' or 'SELL'")
        return side


class ExecutionUpdateResult(BaseModel):
    trade_id: uuid.UUID
    ticker: str
    direction: str
    quantity: float
    price: float
    execution_time: datetime
    edited_at: Optional[datetime]
    # What the broker originally reported, once this fill has been edited.
    # Null on untouched rows and on manual entries, which had no broker value.
    broker_original: Optional[dict] = None
    positions_removed: int
    positions_rebuilt: int
    reviews_discarded: int


@app.patch(
    "/api/trades/{trade_id}/execution",
    response_model=ExecutionUpdateResult,
    dependencies=[Depends(verify_clerk_token)],
)
async def update_execution(
    trade_id: uuid.UUID,
    params: ExecutionUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Correct a fill, then rebuild every round trip that depended on it.

    Editing quantity, price, side or time changes what FIFO matches, so the
    same care delete_trade takes is required here: positions built from this
    fill assert a size and a realised P&L derived from its old values, and
    leaving them in place would let analytics keep reporting figures no
    execution supports.

    The first edit snapshots the broker's values into `broker_original`. That
    is what keeps the ledger reconcilable after it stops agreeing with IBKR --
    without it, a hand-corrected fill is indistinguishable from a broker one
    and the next reconciliation silently "finds" a discrepancy it cannot
    explain.
    """
    from services.matching_engine import (  # noqa: PLC0415 - avoids import cycle
        run_matching_for_ticker,
    )

    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    changes = params.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update.")

    ticker = trade.ticker

    # Snapshot once, before the first mutation. Re-snapshotting on a later edit
    # would overwrite the broker's figures with the previous edit's and destroy
    # the only copy of what actually arrived.
    if trade.broker_original is None and trade.source_tag == "IBKR":
        trade.broker_original = {
            "direction": trade.direction,
            "quantity": str(trade.quantity),
            "price": str(trade.actual_entry),
            "execution_time": trade.entry_date.isoformat() if trade.entry_date else None,
        }

    if "direction" in changes:
        trade.direction = changes["direction"]
    if "quantity" in changes:
        trade.quantity = Decimal(str(changes["quantity"]))
    if "price" in changes:
        trade.actual_entry = Decimal(str(changes["price"]))
    if "execution_time" in changes:
        when = changes["execution_time"]
        if when is not None and when.tzinfo is None:
            # datetime-local inputs arrive bare; anchor to market time, exactly
            # as manual entry does, or the heatmap buckets them by the server's
            # timezone instead.
            when = when.replace(tzinfo=MARKET_TZ)
        trade.entry_date = when

    trade.edited_at = datetime.now(MARKET_TZ)

    # Same reasoning as delete_trade: captured before the rebuild, because
    # position_fills.trade_id cascades and the link is about to disappear.
    affected_ids = list(set((
        await session.execute(
            select(PositionFill.position_id).where(PositionFill.trade_id == trade_id)
        )
    ).scalars().all()))

    reviews_discarded = 0
    if affected_ids:
        affected = (
            await session.execute(select(Position).where(Position.id.in_(affected_ids)))
        ).scalars().all()
        reviews_discarded = sum(
            1
            for p in affected
            if p.review_status == ReviewStatus.reviewed.value
            or any((p.review_went_well, p.review_went_wrong, p.review_lessons, p.notes))
        )
        await session.execute(delete(Position).where(Position.id.in_(affected_ids)))

    await session.commit()

    result = await run_matching_for_ticker(session, ticker, persist=True)
    await session.refresh(trade)

    return ExecutionUpdateResult(
        trade_id=trade.id,
        ticker=trade.ticker,
        direction=trade.direction,
        quantity=float(trade.quantity),
        price=float(trade.actual_entry),
        execution_time=trade.entry_date,
        edited_at=trade.edited_at,
        broker_original=trade.broker_original,
        positions_removed=len(affected_ids),
        positions_rebuilt=len(result.positions),
        reviews_discarded=reviews_discarded,
    )


class SuppressedExecutionOut(BaseModel):
    """One tombstoned broker fill, as the management list shows it.

    Every field except the id is nullable. Tombstones written before migration
    017 recorded only the id, ticker and reason, and there is nothing to
    backfill them from -- the fill they name is deleted. A null reads as "not
    recorded", which is true; a zero quantity would be an invention.
    """

    ibkr_exec_id: str
    ticker: Optional[str]
    reason: Optional[str]
    direction: Optional[str]
    quantity: Optional[float]
    price: Optional[float]
    executed_at: Optional[datetime]
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


@app.get(
    "/api/trades/suppressed",
    response_model=list[SuppressedExecutionOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_suppressed_executions(session: AsyncSession = Depends(get_session)):
    """Broker fills the user deleted, which ingest is skipping.

    Ordered by when the fill happened rather than when it was suppressed: the
    user arrives here looking for a trade they remember taking, not for the
    order in which they pressed delete. Rows with no recorded time sort last
    rather than being hidden.
    """
    rows = (
        await session.execute(
            select(SuppressedExecution).order_by(
                SuppressedExecution.executed_at.desc().nullslast(),
                SuppressedExecution.created_at.desc(),
            )
        )
    ).scalars().all()
    return [SuppressedExecutionOut.model_validate(r) for r in rows]


class UnsuppressResult(BaseModel):
    ibkr_exec_id: str
    ticker: Optional[str]
    # Deliberately explicit: removing the tombstone does NOT put the fill back.
    # The row was deleted; only the broker still has it. Nothing changes until
    # the next sync covers this fill's date, which the UI has to say plainly or
    # "Restore" promises something it cannot deliver.
    restored_immediately: bool = False


@app.delete(
    "/api/trades/suppressed/{exec_id}",
    response_model=UnsuppressResult,
    dependencies=[Depends(verify_clerk_token)],
)
async def unsuppress_execution(
    exec_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Lift the tombstone so a future sync may re-import this fill.

    NOTE ON THE ROUTE. The obvious shape would be
    `POST /api/trades/{trade_id}/unsuppress`, but there is no trade to address:
    suppression exists precisely because the row was deleted, and
    `suppressed_executions` is keyed by the broker's own id for that reason. So
    the broker id is the path parameter, and the verb is DELETE, because the
    thing being removed is the tombstone.

    This does not restore anything by itself, and says so in its response. The
    fill returns only when a sync next covers its date -- which for an old fill
    means the Flex query window has to reach back that far.
    """
    row = await session.get(SuppressedExecution, exec_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No suppression found for that id.")

    ticker = row.ticker
    await session.delete(row)
    await session.commit()
    return UnsuppressResult(ibkr_exec_id=exec_id, ticker=ticker)


class TradeDeleteResult(BaseModel):
    """What a deletion actually did, beyond removing one row.

    Returned instead of a bare 204 because the side effects are not guessable
    from the request: deleting one fill can dissolve a whole round trip and
    take its review with it. The UI reports this rather than letting the user
    find out later.
    """

    deleted_trade_id: uuid.UUID
    ticker: str
    positions_removed: int
    positions_rebuilt: int
    reviews_discarded: int
    # True when a tombstone was written, i.e. this was a broker fill and the
    # next sync will not bring it back. False for manual entries, which no
    # sync would re-send anyway.
    suppressed_from_future_syncs: bool = False


@app.delete(
    "/api/trades/{trade_id}",
    response_model=TradeDeleteResult,
    dependencies=[Depends(verify_clerk_token)],
)
async def delete_trade(
    trade_id: uuid.UUID,
    reason: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """Remove an execution, then rebuild every round trip it belonged to.

    Deleting the row alone is not enough, and silently corrupts the journal.
    A `positions` row stores quantity, weighted entry/exit and realised P&L
    computed from a specific set of executions; drop one of them and the
    position survives asserting a size its remaining fills no longer support,
    with a P&L derived from a fill that no longer exists. Analytics keeps
    reporting that figure, so the damage is invisible.

    Worse, `positions.open_trade_id` is ON DELETE SET NULL, and the opening
    execution is where the plan lives. Deleting it silently detached the stop,
    thesis, conviction and strategy from the round trip -- the R-multiple went
    to None with nothing on screen to say why.

    So affected positions are removed and FIFO matching is re-run for the
    ticker, exactly as manual entry and IBKR ingest already do. The rebuild is
    lossy by nature: a round trip that no longer exists cannot keep its
    review, so the count of discarded reviews is reported rather than left for
    the user to discover.
    """
    from services.matching_engine import (  # noqa: PLC0415 - avoids import cycle
        run_matching_for_ticker,
    )

    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    ticker = trade.ticker

    # Captured before the delete: position_fills.trade_id is ON DELETE CASCADE,
    # so the link disappears with the trade and the positions become
    # unreachable orphans.
    affected_ids = (
        await session.execute(
            select(PositionFill.position_id).where(PositionFill.trade_id == trade_id)
        )
    ).scalars().all()
    affected_ids = list(set(affected_ids))

    reviews_discarded = 0
    if affected_ids:
        affected = (
            await session.execute(select(Position).where(Position.id.in_(affected_ids)))
        ).scalars().all()
        reviews_discarded = sum(
            1
            for p in affected
            if p.review_status == ReviewStatus.reviewed.value
            or any((p.review_went_well, p.review_went_wrong, p.review_lessons, p.notes))
        )
        # position_fills.position_id cascades, so the fills go with them.
        await session.execute(delete(Position).where(Position.id.in_(affected_ids)))

    # Tombstone broker fills so the next sync does not resurrect them.
    # Hand-added repairs are skipped: their REPAIR-<uuid> key is generated
    # fresh each time and no sync will ever re-send it, so suppressing one
    # would only grow a table nothing reads.
    suppressed = False
    if trade.ibkr_exec_id and trade.ibkr_exec_id.startswith(EXEC_PREFIX_BROKER):
        await session.execute(
            pg_insert(SuppressedExecution)
            .values(
                ibkr_exec_id=trade.ibkr_exec_id,
                ticker=ticker,
                reason=reason or "Deleted from the journal",
                # Copied now or lost: the row is about to be deleted.
                direction=trade.direction,
                quantity=trade.quantity,
                price=trade.actual_entry,
                executed_at=trade.entry_date,
            )
            # Deleting the same broker id twice is not an error; it is the user
            # being thorough after a sync re-sent something.
            .on_conflict_do_nothing(index_elements=["ibkr_exec_id"])
        )
        suppressed = True

    await session.delete(trade)
    await session.commit()

    # Rebuild from what actually remains. Idempotent, so untouched round trips
    # on this ticker are not duplicated.
    result = await run_matching_for_ticker(session, ticker, persist=True)

    return TradeDeleteResult(
        deleted_trade_id=trade_id,
        ticker=ticker,
        positions_removed=len(affected_ids),
        positions_rebuilt=len(result.positions),
        reviews_discarded=reviews_discarded,
        suppressed_from_future_syncs=suppressed,
    )


class PositionDeleteResult(BaseModel):
    """What removing a round trip actually removed.

    Deleting a position means deleting the executions underneath it, so the
    count of fills is reported: this is a bigger action than dismissing, and
    the response should say so rather than let it look like a queue operation.
    """

    position_id: uuid.UUID
    ticker: str
    executions_deleted: int
    positions_rebuilt: int
    suppressed_from_future_syncs: int


@app.delete(
    "/api/positions/{position_id}",
    response_model=PositionDeleteResult,
    dependencies=[Depends(verify_clerk_token)],
)
async def delete_position(
    position_id: uuid.UUID,
    reason: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """Delete a round trip AND the executions it was built from.

    The distinction from dismissing matters. A position is derived data -- it
    exists because two fills paired off -- so deleting the position alone would
    leave those fills behind for the next rebuild to pair up again, and the row
    would silently return. Removing the executions is the only deletion that
    holds.

    That is what this is for: a round trip that never happened, such as the
    phantom CAT and UNH trades a duplicate sync invented. For a real trade you
    simply do not want to review, dismiss it instead and keep the P&L.

    Broker fills are tombstoned on the way out so the sync cannot re-add them.
    """
    from services.matching_engine import (  # noqa: PLC0415 - avoids import cycle
        run_matching_for_ticker,
    )

    position = await session.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")

    ticker = position.symbol
    trade_ids = list(set((
        await session.execute(
            select(PositionFill.trade_id).where(PositionFill.position_id == position_id)
        )
    ).scalars().all()))

    suppressed = 0
    if trade_ids:
        trades = (
            await session.execute(select(Trade).where(Trade.id.in_(trade_ids)))
        ).scalars().all()
        for trade in trades:
            if trade.ibkr_exec_id and trade.ibkr_exec_id.startswith(EXEC_PREFIX_BROKER):
                await session.execute(
                    pg_insert(SuppressedExecution)
                    .values(
                        ibkr_exec_id=trade.ibkr_exec_id,
                        ticker=ticker,
                        reason=reason or "Round trip deleted from the inbox",
                        direction=trade.direction,
                        quantity=trade.quantity,
                        price=trade.actual_entry,
                        executed_at=trade.entry_date,
                    )
                    .on_conflict_do_nothing(index_elements=["ibkr_exec_id"])
                )
                suppressed += 1

    # The position goes first; position_fills cascades from both sides, and
    # deleting the trades while fills still referenced the position would leave
    # it asserting a size its executions no longer support.
    await session.delete(position)
    if trade_ids:
        await session.execute(delete(Trade).where(Trade.id.in_(trade_ids)))
    await session.commit()

    # Other round trips on this ticker are untouched by the rebuild; matching
    # is idempotent on the open/close pair.
    result = await run_matching_for_ticker(session, ticker, persist=True)

    return PositionDeleteResult(
        position_id=position_id,
        ticker=ticker,
        executions_deleted=len(trade_ids),
        positions_rebuilt=len(result.positions),
        suppressed_from_future_syncs=suppressed,
    )


@app.post(
    "/api/positions/{position_id}/dismiss",
    response_model=PositionOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def dismiss_position(
    position_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Take a round trip out of the review queue without reviewing it.

    Deliberately distinct from deleting. The trade happened, its P&L is real
    and stays in every analytic; the user simply has nothing to write about it.
    Marking it reviewed is what empties the queue, and leaving the review
    fields empty is what keeps it out of discipline and grade breakdowns --
    absence of an answer is not the same as a bad answer, which is the same
    distinction position_disciplines was built on.
    """
    position = await session.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")

    position.review_status = ReviewStatus.reviewed.value
    await session.commit()
    await session.refresh(position)
    return await _position_out(session, position)


@app.get(
    "/api/positions",
    response_model=list[PositionOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_positions(
    review_status: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """List closed positions, newest first.

    `review_status=pending` backs the Trade Inbox queue.
    """
    stmt = select(Position).order_by(Position.exit_time.desc())
    if review_status:
        stmt = stmt.where(Position.review_status == review_status)

    positions = (await session.execute(stmt)).scalars().all()

    # One query for the whole page rather than a lookup per row.
    by_position = await _disciplines_by_position(session, [p.id for p in positions])

    out: list[PositionOut] = []
    for position in positions:
        row = PositionOut.model_validate(position)
        row.disciplines = by_position.get(position.id, [])
        out.append(row)
    return out


@app.get(
    "/api/positions/{position_id}/fills",
    response_model=list[PositionFillOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_position_fills(
    position_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """The individual executions behind one round trip, oldest first.

    A position aggregates flat-to-flat, so its entry and exit prices are
    quantity-weighted. This is where the underlying scale-ins and scale-outs
    are visible -- the detail that weighted averages hide.
    """
    exists = await session.get(Position, position_id)
    if exists is None:
        raise HTTPException(status_code=404, detail="Position not found")

    stmt = (
        select(PositionFill)
        .where(PositionFill.position_id == position_id)
        .order_by(PositionFill.executed_at, PositionFill.role)
    )
    result = await session.execute(stmt)
    return [PositionFillOut.model_validate(f) for f in result.scalars().all()]


# ---------------------------------------------------------------------------
# The journal: round trips, not executions
# ---------------------------------------------------------------------------


class RoundTripOut(BaseModel):
    """One trade idea, whatever number of executions it took.

    The ledger used to list raw fills, which is why a single CRWD trade showed
    up as four rows: the broker filled the entry with two orders and the exit
    with two more. Those four executions were always one round trip in the
    data -- `positions` plus `position_fills` -- the view simply never used it.

    `kind` distinguishes the two things a journal must show side by side:
      closed -- a completed round trip, backed by a `positions` row.
      open   -- exposure with no counterpart yet, which has no position row at
                all and so was invisible on every other surface.
    """

    kind: str  # "closed" | "open"
    key: str  # stable react key; position id, or "open:TICKER"
    position_id: Optional[uuid.UUID] = None
    # The execution that carries the plan. For a scale-in there are several
    # candidates and exactly one must own it, or two contradictory stops could
    # be stored with no way to say which was meant.
    plan_trade_id: Optional[uuid.UUID] = None

    symbol: str
    direction: str
    quantity: float
    entry_price: float
    exit_price: Optional[float] = None
    entry_time: datetime
    exit_time: Optional[datetime] = None
    realized_pnl: Optional[float] = None
    execution_count: int

    # Scored, never stored: R recomputed from the current entry/exit/stop, so
    # correcting a stop cannot leave a stale R behind.
    r_multiple: Optional[float] = None
    planned_r_multiple: Optional[float] = None

    # --- the plan, from the opening execution -------------------------
    strategy_id: Optional[uuid.UUID] = None
    thesis: Optional[str] = None
    planned_entry: Optional[float] = None
    stop_loss: Optional[float] = None
    actual_stop_loss: Optional[float] = None
    target: Optional[float] = None
    risk_percent: Optional[float] = None
    risk_amount: Optional[float] = None
    conviction: Optional[int] = None
    emotional_state: Optional[str] = None

    # --- did this come from a plan? -----------------------------------
    # Set when the opening fill was matched to a pre-trade plan. The
    # difference matters: planned_entry filled in by an attached plan was
    # committed to before the outcome was known, while the same column typed
    # into the journal afterwards is a recollection. The UI labels them
    # differently for that reason.
    plan_id: Optional[uuid.UUID] = None
    plan_created_at: Optional[datetime] = None
    # Per-share, and signed so positive always means better than planned --
    # which is the opposite arithmetic on a short. See _entry_slippage.
    entry_slippage: Optional[float] = None
    # True when any fill in this round trip was typed in by hand to repair a
    # gap in the broker feed, rather than coming from IBKR.
    has_hand_added_fills: bool = False

    # --- the review, from the position (closed only) ------------------
    review_status: Optional[str] = None
    trade_grade: Optional[str] = None
    notes: Optional[str] = None
    mistakes: list[str] = []
    review_went_well: Optional[str] = None
    review_went_wrong: Optional[str] = None
    review_lessons: Optional[str] = None
    exit_reason: Optional[str] = None
    ideal_entry: Optional[float] = None
    ideal_stop: Optional[float] = None
    ideal_target: Optional[float] = None
    revised_entry: Optional[float] = None
    revised_stop: Optional[float] = None
    revised_target: Optional[float] = None
    disciplines: list[PositionDisciplineOut] = []

    fills: list[PositionFillOut] = []


def _score_r(
    direction: str,
    entry: Optional[Decimal],
    exit_price: Optional[Decimal],
    stop: Optional[Decimal],
) -> Optional[float]:
    """Reward in units of risk, or None when it cannot honestly be scored.

    Delegates to the analytics implementation rather than restating the
    formula: two copies of a sign convention drift, and a journal that scores
    a short trade differently from the analytics page is worse than one that
    does not score it at all.
    """
    from services.analytics import (  # noqa: PLC0415 - avoids an import cycle
        ReviewedTrade,
        compute_r_multiple,
    )

    if entry is None or exit_price is None or stop is None:
        return None

    return compute_r_multiple(
        ReviewedTrade(
            trade_id="",
            ticker="",
            direction=direction,
            quantity=0,
            actual_entry=Decimal(str(entry)),
            exit_price=Decimal(str(exit_price)),
            planned_entry=None,
            stop_loss=Decimal(str(stop)),
            mistakes=[],
            review_status=None,
        )
    )


def _plan_fields(trade: Optional[Trade]) -> dict:
    """Plan attributes off the opening execution, or empty when absent."""
    if trade is None:
        return {}
    return {
        "strategy_id": trade.strategy_id,
        "thesis": trade.thesis,
        "planned_entry": trade.planned_entry,
        "stop_loss": trade.stop_loss,
        "actual_stop_loss": trade.actual_stop_loss,
        "target": trade.target,
        "risk_percent": trade.risk_percent,
        "risk_amount": trade.risk_amount,
        "conviction": trade.conviction,
        "emotional_state": trade.emotional_state,
        # Provenance, not a value: whether these numbers came from a plan
        # written before the fill, or were typed into the journal afterwards.
        # Both are legitimate; only one is evidence about your process.
        "plan_id": trade.plan_id,
    }


def _entry_slippage(
    direction: Optional[str],
    planned_entry: Optional[Decimal],
    actual_entry: Optional[Decimal],
) -> Optional[float]:
    """Per-share difference between the fill and the plan, signed by intent.

    Positive is always BETTER than planned, negative always worse -- which
    requires knowing the side. A long filled above its planned entry paid up;
    a short filled above its planned entry got a better price for the same
    trade. Returning a raw subtraction would read correctly on longs and
    backwards on every short.
    """
    if planned_entry is None or actual_entry is None:
        return None
    delta = (
        planned_entry - actual_entry
        if (direction or "BUY").upper() == "BUY"
        else actual_entry - planned_entry
    )
    return float(delta)


@app.get(
    "/api/round-trips",
    response_model=list[RoundTripOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_round_trips(
    ticker: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """The journal, grouped the way trades are actually thought about.

    Closed round trips come from `positions`; open exposure is reconstructed
    from executions that FIFO matching never paired off. Both carry their plan
    and their fills, so one row is a whole trade rather than a fragment.
    """
    # Reused rather than hardcoded: the role vocabulary and the cost-basis
    # replay both belong to the matching engine, and two copies would drift.
    from services.matching_engine import (  # noqa: PLC0415 - import cycle
        ROLE_CLOSE,
        ROLE_OPEN,
        replay_open_exposure,
    )

    positions = (
        await session.execute(select(Position).order_by(Position.exit_time.desc()))
    ).scalars().all()
    trades = (await session.execute(select(Trade))).scalars().all()
    fills = (await session.execute(select(PositionFill))).scalars().all()

    trade_by_id = {t.id: t for t in trades}
    # When each attached plan was written. Loaded once for the whole page --
    # it is the evidence that a plan predates its fill, which is the only
    # thing separating a plan from a post-hoc annotation.
    plan_created_at: dict[uuid.UUID, datetime] = {}
    plan_ids = {t.plan_id for t in trades if t.plan_id is not None}
    if plan_ids:
        plan_created_at = {
            row[0]: row[1]
            for row in (
                await session.execute(
                    select(PlannedTrade.id, PlannedTrade.created_at).where(
                        PlannedTrade.id.in_(plan_ids)
                    )
                )
            ).all()
        }
    disciplines_by_position = await _disciplines_by_position(
        session, [p.id for p in positions]
    )
    fills_by_position: dict[uuid.UUID, list[PositionFill]] = {}
    for fill in fills:
        fills_by_position.setdefault(fill.position_id, []).append(fill)
    matched_trade_ids = {f.trade_id for f in fills}

    rows: list[RoundTripOut] = []

    # --- closed round trips ------------------------------------------------
    for position in positions:
        if ticker and position.symbol != ticker.strip().upper():
            continue
        opening = trade_by_id.get(position.open_trade_id)
        direction = (opening.direction if opening else "BUY") or "BUY"
        plan = _plan_fields(opening)

        entry = position.entry_price
        stop = plan.get("actual_stop_loss") or plan.get("stop_loss")
        # Realised R is measured against the stop that was actually live; the
        # planned ratio is what the trade was *supposed* to return, so it uses
        # the planned stop and target. Mixing them would flatter every trade
        # where the stop was widened.
        planned_r = _score_r(
            direction, plan.get("planned_entry") or entry,
            plan.get("target"), plan.get("stop_loss"),
        )

        position_fills = sorted(
            fills_by_position.get(position.id, []),
            key=lambda f: (f.executed_at, f.role),
        )
        # Measured against the position's realised entry, which on a multi-fill
        # entry is the average of every opening fill -- not the first one.
        # Slippage is a property of the position you ended up with.
        slippage = _entry_slippage(direction, plan.get("planned_entry"), entry)
        hand_added = any(
            (t := trade_by_id.get(f.trade_id)) is not None
            and (t.ibkr_exec_id or "").startswith(EXEC_PREFIX_REPAIR)
            for f in position_fills
        )
        rows.append(
            RoundTripOut(
                kind="closed",
                key=str(position.id),
                position_id=position.id,
                plan_trade_id=position.open_trade_id,
                symbol=position.symbol,
                direction=direction,
                quantity=float(position.quantity or 0),
                entry_price=float(entry or 0),
                plan_created_at=plan_created_at.get(plan.get("plan_id")),
                entry_slippage=slippage,
                has_hand_added_fills=hand_added,
                exit_price=float(position.exit_price) if position.exit_price is not None else None,
                entry_time=position.entry_time,
                exit_time=position.exit_time,
                realized_pnl=float(position.realized_pnl) if position.realized_pnl is not None else None,
                execution_count=len(position_fills),
                r_multiple=_score_r(direction, entry, position.exit_price, stop),
                planned_r_multiple=planned_r,
                review_status=position.review_status,
                trade_grade=position.trade_grade,
                notes=position.notes,
                mistakes=list(position.mistakes or []),
                review_went_well=position.review_went_well,
                review_went_wrong=position.review_went_wrong,
                review_lessons=position.review_lessons,
                exit_reason=position.exit_reason,
                ideal_entry=position.ideal_entry,
                ideal_stop=position.ideal_stop,
                ideal_target=position.ideal_target,
                revised_entry=position.revised_entry,
                revised_stop=position.revised_stop,
                revised_target=position.revised_target,
                disciplines=disciplines_by_position.get(position.id, []),
                fills=[PositionFillOut.model_validate(f) for f in position_fills],
                **plan,
            )
        )

    # --- open exposure -----------------------------------------------------
    # Executions FIFO never paired off. Grouped per ticker, because that is the
    # unit of exposure: two unsold AAPL buys are one open position, not two.
    open_by_ticker: dict[str, list[Trade]] = {}
    for trade in trades:
        if trade.id in matched_trade_ids:
            continue
        if ticker and trade.ticker != ticker.strip().upper():
            continue
        open_by_ticker.setdefault(trade.ticker, []).append(trade)

    for symbol, group in open_by_ticker.items():
        group.sort(key=lambda t: t.entry_date)
        signed = sum(
            (t.quantity or Decimal("0"))
            * (Decimal("1") if (t.direction or "BUY").upper() == "BUY" else Decimal("-1"))
            for t in group
        )
        if signed == 0:
            # Nets flat without ever being matched -- a data oddity rather than
            # live exposure. Skipped rather than rendered as a zero-size row.
            continue

        net_direction = "BUY" if signed > 0 else "SELL"
        # Replayed rather than averaged. Taking the mean of every same-direction
        # fill treats shares that have already been sold as though they were
        # still held: MSFT reported 415.45, the average of all 17 shares ever
        # bought, when only 2 remained at 409.40.
        exposure = replay_open_exposure(
            (t.direction, t.quantity or Decimal("0"), t.actual_entry or Decimal("0"))
            for t in group
        )
        avg_entry = exposure.average_cost

        opening = group[0]
        plan = _plan_fields(opening)
        rows.append(
            RoundTripOut(
                kind="open",
                key=f"open:{symbol}",
                position_id=None,
                plan_trade_id=opening.id,
                symbol=symbol,
                direction=net_direction,
                quantity=float(abs(signed)),
                entry_price=float(avg_entry),
                entry_time=opening.entry_date,
                execution_count=len(group),
                plan_created_at=plan_created_at.get(plan.get("plan_id")),
                # Against the replayed cost basis, for the same reason the
                # closed branch uses the position's entry: what you are still
                # holding is what the plan should be judged against.
                entry_slippage=_entry_slippage(
                    net_direction, plan.get("planned_entry"), avg_entry
                ),
                has_hand_added_fills=any(
                    (t.ibkr_exec_id or "").startswith(EXEC_PREFIX_REPAIR) for t in group
                ),
                planned_r_multiple=_score_r(
                    net_direction, plan.get("planned_entry") or avg_entry,
                    plan.get("target"), plan.get("stop_loss"),
                ),
                fills=[
                    PositionFillOut(
                        id=t.id,
                        trade_id=t.id,
                        # A fill's role is what it DID, not which group it
                        # landed in. Hardcoding OPEN here labelled every
                        # partial sell as an opening buy -- MSFT showed seven
                        # OPEN fills when three of them were sells.
                        role=(
                            ROLE_OPEN
                            if (t.direction or "BUY").upper() == net_direction
                            else ROLE_CLOSE
                        ),
                        quantity=float(t.quantity or 0),
                        price=float(t.actual_entry or 0),
                        executed_at=t.entry_date,
                    )
                    for t in group
                ],
                **plan,
            )
        )

    # Open exposure first (it needs decisions), then closed by recency.
    rows.sort(key=lambda r: (r.kind != "open", -(r.exit_time or r.entry_time).timestamp()))
    return rows


# Both verbs hit the same handler: PUT is the documented route, PATCH is
# retained because the Trade Inbox already calls it. The body is a partial
# update either way (exclude_unset), which is why PATCH remains accurate.
@app.put(
    "/api/positions/{position_id}/review",
    response_model=PositionOut,
    dependencies=[Depends(verify_clerk_token)],
)
@app.patch(
    "/api/positions/{position_id}/review",
    response_model=PositionOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def review_position(
    position_id: uuid.UUID,
    params: PositionReviewUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Record the review checklist and mark the position completed."""
    position = await session.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")

    updates = params.model_dump(exclude_unset=True)

    # Pulled out before the setattr loop below: `disciplines` lives in its own
    # table, and assigning it to the ORM object would silently become a stray
    # Python attribute that never reaches the database.
    discipline_answers = updates.pop("disciplines", None)

    # Reject a dangling strategy reference up front rather than surfacing a
    # foreign-key error from the database.
    strategy_id = updates.get("strategy_id")
    if strategy_id is not None:
        if await session.get(Strategy, strategy_id) is None:
            raise HTTPException(
                status_code=400, detail=f"Strategy {strategy_id} does not exist"
            )

    if "mistakes" in updates:
        # Trim, drop blanks, de-duplicate while preserving order.
        seen: set[str] = set()
        cleaned: list[str] = []
        for tag in updates["mistakes"] or []:
            tag = (tag or "").strip()
            if tag and tag not in seen:
                seen.add(tag)
                cleaned.append(tag)
        updates["mistakes"] = cleaned

    for field, value in updates.items():
        setattr(position, field, value)

    if discipline_answers:
        known = set(
            (
                await session.execute(
                    select(Discipline.id).where(
                        Discipline.id.in_(list(discipline_answers))
                    )
                )
            ).scalars().all()
        )
        unknown = set(discipline_answers) - known
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown discipline rule(s): {sorted(str(u) for u in unknown)}",
            )

        # Upsert: re-saving a review must correct the previous answer, not
        # collide with it. Deleting and re-inserting would lose created_at and
        # briefly leave the round trip looking unreviewed.
        for discipline_id, followed in discipline_answers.items():
            stmt = (
                pg_insert(PositionDiscipline)
                .values(
                    position_id=position_id,
                    discipline_id=discipline_id,
                    followed=bool(followed),
                )
                .on_conflict_do_update(
                    index_elements=["position_id", "discipline_id"],
                    set_={"followed": bool(followed)},
                )
            )
            await session.execute(stmt)

    position.review_status = ReviewStatus.reviewed.value

    await session.commit()
    await session.refresh(position)
    return await _position_out(session, position)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@app.get("/api/analytics/dashboard", dependencies=[Depends(verify_clerk_token)])
async def analytics_dashboard(session: AsyncSession = Depends(get_session)):
    """Core stats plus the day/session heatmap grid."""
    from services.analytics import build_dashboard

    return await build_dashboard(session)


@app.get("/api/analytics/advanced", dependencies=[Depends(verify_clerk_token)])
async def analytics_advanced(session: AsyncSession = Depends(get_session)):
    """R-multiples, slippage, expectancy, and the per-mistake breakdown.

    Sourced from `trades` rather than `positions`: R-multiple and slippage
    need the plan (stop_loss, planned_entry), which only the ledger carries.
    """
    from services.analytics import build_advanced_analytics

    return await build_advanced_analytics(session)

