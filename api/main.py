import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
    quantity = Column(Integer, nullable=False)
    planned_entry = Column(Numeric(10, 4), nullable=True)
    stop_loss = Column(Numeric(10, 4), nullable=True)
    target = Column(Numeric(10, 4), nullable=True)
    risk_percent = Column(Numeric(4, 2), default=1.00)
    strategy_id = Column(
        UUID(as_uuid=True), ForeignKey("strategies.id", ondelete="SET NULL"), nullable=True
    )
    grade = Column(CHAR(1), nullable=True)
    market_regime = Column(String(20), nullable=True)
    source_tag = Column(String(10), default="Own")
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
    quantity = Column(Numeric(12, 4), nullable=False)
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
    quantity = Column(Integer, nullable=False)
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

# Local dev (localhost:3000) and the production Vercel deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://trading-journal-seven-ivory.vercel.app",
    ],
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
    fractional_quantities: int


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
        root = await ibkr_client.fetch_statement()
    except ibkr_client.IBKRError as exc:
        # Transient "still compiling" conditions are a 503 so callers retry;
        # everything else is an upstream failure.
        raise HTTPException(status_code=503 if exc.retryable else 502, detail=str(exc))

    executions = ibkr_parser.parse_statement(root)
    if not executions:
        return IngestResult(
            executions_parsed=0,
            staged_new=0,
            staged_duplicates=0,
            trades_created=0,
            trades_duplicates=0,
            positions_matched=0,
            symbols_touched=[],
            fractional_quantities=0,
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
        # Fills IBKR reported fractionally that had to be rounded to satisfy
        # the INTEGER ledger column; each one is also logged as a warning.
        fractional_quantities=sum(
            1 for e in new_executions if e.quantity_was_rounded
        ),
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

    @field_validator("quantity")
    @classmethod
    def _whole_shares(cls, value: float) -> float:
        # trades.quantity is an INTEGER column. Reject fractional input rather
        # than silently truncating 0.5 shares to 0.
        if value != int(value):
            raise ValueError(
                "quantity must be a whole number of shares "
                "(the trades table stores an integer quantity)"
            )
        return value


class ManualTradeResult(BaseModel):
    trade_id: uuid.UUID
    ticker: str
    direction: str
    quantity: int
    price: float
    execution_time: datetime
    planned_entry: Optional[float]
    planned_stop_loss: Optional[float]
    take_profit_price: Optional[float]
    exit_price: Optional[float]
    # Round trips the FIFO engine closed as a result of this execution.
    positions_created: int
    open_quantity: int


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
        quantity=int(params.quantity),
        # Planning fields map onto the ledger's existing columns; stop_loss and
        # target are the canonical homes for the planned stop and take-profit,
        # and are what PUT /api/trades/{id} reads and writes.
        planned_entry=params.planned_entry,
        stop_loss=params.planned_stop_loss,
        target=params.take_profit_price,
        exit_price=params.exit_price,
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
    created_at: Optional[datetime]

    @field_validator("mistakes", mode="before")
    @classmethod
    def _null_to_list(cls, value: Optional[list[str]]) -> list[str]:
        return list(value or [])

    class Config:
        from_attributes = True


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

