import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    CHAR,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
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
    risk_percent = Column(Numeric(4, 2), default=1.00)
    # The absolute figure, which is what turns an R-multiple back into money.
    risk_amount = Column(Numeric(12, 2), nullable=True)
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
    )


# ---------------------------------------------------------------------------
# Manual trade entry
# ---------------------------------------------------------------------------

# Naive timestamps from the client are interpreted as US market time, matching
# how the analytics service buckets sessions.
MARKET_TZ = ZoneInfo("America/New_York")


class ManualTradeCreate(BaseModel):
    """One hand-logged execution, for traders not on an automated broker sync."""

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
    """Log an execution by hand and re-run FIFO matching for its ticker.

    The execution lands in `trades` exactly like a synced fill, so the matching
    engine treats hand-logged and broker-sourced fills identically.
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
        # Synthetic id keeps manual fills traceable and distinct from broker
        # rows, while still satisfying the UNIQUE constraint.
        ibkr_exec_id=f"MANUAL-{uuid.uuid4()}",
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
        strategy_id=params.strategy_id,
        thesis=params.thesis,
        source_tag="Manual",
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

    @field_validator("method", "entry_criteria", "exit_criteria", mode="before")
    @classmethod
    def _null_to_empty(cls, value: Optional[str]) -> str:
        return value or ""

    class Config:
        from_attributes = True


@app.get(
    "/api/strategies",
    response_model=list[StrategyOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_strategies(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Strategy).order_by(Strategy.name))
    return [StrategyOut.model_validate(s) for s in result.scalars().all()]


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
    created_at: Optional[datetime]

    @field_validator("mistakes", mode="before")
    @classmethod
    def _null_to_list(cls, value: Optional[list[str]]) -> list[str]:
        return list(value or [])

    class Config:
        from_attributes = True


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

    result = await session.execute(stmt)
    return [PositionOut.model_validate(p) for p in result.scalars().all()]


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
    }


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
    # Reused rather than hardcoded as "OPEN": the role vocabulary belongs to
    # the matching engine, and two copies would drift.
    from services.matching_engine import ROLE_OPEN  # noqa: PLC0415 - import cycle

    positions = (
        await session.execute(select(Position).order_by(Position.exit_time.desc()))
    ).scalars().all()
    trades = (await session.execute(select(Trade))).scalars().all()
    fills = (await session.execute(select(PositionFill))).scalars().all()

    trade_by_id = {t.id: t for t in trades}
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
        side = [t for t in group if (t.direction or "BUY").upper() == net_direction]
        qty = sum((t.quantity or Decimal("0")) for t in side) or Decimal("1")
        # Quantity-weighted, so scaling in reports the real average cost.
        avg_entry = sum(
            (t.actual_entry or Decimal("0")) * (t.quantity or Decimal("0")) for t in side
        ) / qty

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
                planned_r_multiple=_score_r(
                    net_direction, plan.get("planned_entry") or avg_entry,
                    plan.get("target"), plan.get("stop_loss"),
                ),
                fills=[
                    PositionFillOut(
                        id=t.id,
                        trade_id=t.id,
                        role=ROLE_OPEN,
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

    position.review_status = ReviewStatus.reviewed.value

    await session.commit()
    await session.refresh(position)
    return PositionOut.model_validate(position)


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

