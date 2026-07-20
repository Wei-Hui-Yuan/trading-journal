import asyncio
import os
import uuid
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
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
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

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


class TradeStatus(str, Enum):
    pending_review = "pending_review"
    completed = "completed"


class ReviewStatus(str, Enum):
    """Lifecycle of a position in the Trade Inbox."""

    pending = "pending"
    completed = "completed"


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    # Predates migration 002; retained so existing rows keep their data.
    instruments = Column(ARRAY(Text), default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Trade(Base):
    __tablename__ = "trades"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ibkr_exec_id = Column(String(100), unique=True, nullable=True)
    ticker = Column(String(10), nullable=False)
    direction = Column(String(5), nullable=False)
    style = Column(String(15), nullable=False)
    status = Column(String(20), default=TradeStatus.pending_review.value)
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
    lessons_comments = Column(Text, nullable=True)
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


async def get_session():
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title="Trading Journal API", lifespan=lifespan)

# Next.js frontend runs on localhost:3000.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TradeManualUpdate(BaseModel):
    """Manual planning + qualitative fields only.

    IBKR-populated execution fields (actual_entry, exit_price, quantity,
    entry_date, exit_date, ticker, direction, ibkr_exec_id) are intentionally
    excluded and cannot be modified through this endpoint.
    """

    planned_entry: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    risk_percent: Optional[float] = Field(None, gt=0)
    strategy_id: Optional[uuid.UUID] = None
    style: Optional[str] = Field(None, max_length=15)
    grade: Optional[str] = Field(None, min_length=1, max_length=1)
    market_regime: Optional[str] = Field(None, max_length=20)
    source_tag: Optional[str] = Field(None, max_length=10)
    screenshot_url: Optional[str] = None
    lessons_comments: Optional[str] = None
    hard_sl_set: Optional[bool] = None
    waited_retest: Optional[bool] = None
    followed_plan: Optional[bool] = None


class TradeOut(BaseModel):
    id: uuid.UUID
    ibkr_exec_id: Optional[str]
    ticker: str
    direction: str
    style: str
    status: str
    entry_date: datetime
    exit_date: Optional[datetime]
    actual_entry: float
    exit_price: Optional[float]
    quantity: int
    planned_entry: Optional[float]
    stop_loss: Optional[float]
    target: Optional[float]
    risk_percent: Optional[float]
    strategy_id: Optional[uuid.UUID]
    grade: Optional[str]
    market_regime: Optional[str]
    source_tag: Optional[str]
    screenshot_url: Optional[str]
    lessons_comments: Optional[str]
    hard_sl_set: Optional[bool]
    waited_retest: Optional[bool]
    followed_plan: Optional[bool]
    created_at: datetime

    class Config:
        from_attributes = True


@app.get("/api/trades")
async def get_trades_grouped_by_status(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Trade))
    trades = result.scalars().all()

    grouped: dict[str, list[TradeOut]] = {}
    for trade in trades:
        grouped.setdefault(trade.status, []).append(TradeOut.model_validate(trade))
    return grouped


@app.put("/api/trades/{trade_id}")
async def complete_trade(
    trade_id: uuid.UUID,
    params: TradeManualUpdate,
    session: AsyncSession = Depends(get_session),
):
    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    if trade.status != TradeStatus.pending_review.value:
        raise HTTPException(
            status_code=400,
            detail=f"Trade {trade_id} is not pending review (current status: {trade.status})",
        )

    updates = params.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(trade, field, value)
    trade.status = TradeStatus.completed.value

    await session.commit()
    await session.refresh(trade)
    return TradeOut.model_validate(trade)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


class StrategyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None


class StrategyOut(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


@app.get("/api/strategies", response_model=list[StrategyOut])
async def list_strategies(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Strategy).order_by(Strategy.name))
    return [StrategyOut.model_validate(s) for s in result.scalars().all()]


@app.post("/api/strategies", response_model=StrategyOut, status_code=201)
async def create_strategy(
    params: StrategyCreate, session: AsyncSession = Depends(get_session)
):
    strategy = Strategy(name=params.name, description=params.description)
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


# ---------------------------------------------------------------------------
# Position review (Trade Inbox)
# ---------------------------------------------------------------------------


class PositionReviewUpdate(BaseModel):
    """Checklist payload from the Trade Inbox.

    Every field is optional; only those present in the request body are
    applied, so a partial save never clears untouched fields.
    """

    strategy_id: Optional[uuid.UUID] = None
    tag_hard_sl: Optional[bool] = None
    tag_retest: Optional[bool] = None
    tag_plan_compliant: Optional[bool] = None
    trade_grade: Optional[str] = Field(None, max_length=5)


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
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


@app.get("/api/positions", response_model=list[PositionOut])
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


@app.patch("/api/positions/{position_id}/review", response_model=PositionOut)
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

    for field, value in updates.items():
        setattr(position, field, value)

    position.review_status = ReviewStatus.completed.value

    await session.commit()
    await session.refresh(position)
    return PositionOut.model_validate(position)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@app.get("/api/analytics/dashboard")
async def analytics_dashboard(session: AsyncSession = Depends(get_session)):
    """Core stats plus the day/session heatmap grid."""
    from services.analytics import build_dashboard

    return await build_dashboard(session)


# ---------------------------------------------------------------------------
# IBKR Flex Web Service sync
# ---------------------------------------------------------------------------

IBKR_FLEX_BASE = "https://ndcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService"
IBKR_SEND_REQUEST_URL = f"{IBKR_FLEX_BASE}.SendRequest"
IBKR_GET_STATEMENT_URL = f"{IBKR_FLEX_BASE}.GetStatement"

# IBKR returns these while the report is still being generated on their side.
IBKR_NOT_READY_CODES = {"1018", "1019"}

# style is NOT NULL in the schema but is a manual classification IBKR cannot
# supply, so freshly synced executions get a placeholder for the user to edit.
DEFAULT_STYLE = "Unclassified"

GET_STATEMENT_MAX_ATTEMPTS = 5
GET_STATEMENT_RETRY_DELAY = 3.0


class SyncResult(BaseModel):
    reference_code: str
    fills_found: int
    inserted: int
    skipped_duplicates: int
    skipped_unparseable: int
    inserted_trade_ids: list[str]


def _ibkr_credentials() -> tuple[str, str]:
    token = os.environ.get("IBKR_TOKEN")
    query_id = os.environ.get("IBKR_QUERY_ID")
    if not token or not query_id:
        raise HTTPException(
            status_code=500,
            detail="IBKR_TOKEN and IBKR_QUERY_ID must be set in the environment",
        )
    return token, query_id


def _text(root: ET.Element, tag: str) -> Optional[str]:
    node = root.find(f".//{tag}")
    return node.text.strip() if node is not None and node.text else None


def _parse_ibkr_datetime(raw: Optional[str]) -> Optional[datetime]:
    """Parse IBKR's dateTime field into a timezone-aware datetime.

    IBKR emits `yyyyMMdd;HHmmss` (e.g. 20250115;103045) but the separator and
    field widths vary by Flex query configuration, so several shapes are tried.
    """
    if not raw:
        return None

    cleaned = raw.strip().replace(";", " ").replace(",", " ")
    cleaned = " ".join(cleaned.split())

    for fmt in (
        "%Y%m%d %H%M%S",
        "%Y%m%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y%m%d",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        # IBKR reports in the timezone configured on the Flex query; it carries
        # no offset, so it is anchored to UTC here.
        return parsed.replace(tzinfo=timezone.utc)

    return None


async def _initiate_flex_request(client: httpx.AsyncClient, token: str, query_id: str) -> str:
    """Call SendRequest and return the ReferenceCode."""
    response = await client.get(
        IBKR_SEND_REQUEST_URL,
        params={"t": token, "q": query_id, "v": "3"},
    )
    response.raise_for_status()

    try:
        root = ET.fromstring(response.text)
    except ET.ParseError as exc:
        raise HTTPException(
            status_code=502, detail=f"IBKR returned unparseable XML: {exc}"
        ) from exc

    status = (_text(root, "Status") or "").lower()
    if status != "success":
        error_code = _text(root, "ErrorCode") or ""
        error_message = _text(root, "ErrorMessage") or "unknown error"
        if error_code in IBKR_NOT_READY_CODES:
            raise HTTPException(
                status_code=503,
                detail=f"IBKR statement not ready yet (code {error_code}): {error_message}. Retry shortly.",
            )
        raise HTTPException(
            status_code=502,
            detail=f"IBKR request failed (code {error_code}): {error_message}",
        )

    reference_code = _text(root, "ReferenceCode")
    if not reference_code:
        raise HTTPException(status_code=502, detail="IBKR response contained no ReferenceCode")
    return reference_code


async def _fetch_flex_statement(
    client: httpx.AsyncClient, token: str, reference_code: str
) -> ET.Element:
    """Poll GetStatement until the report is compiled, then return its XML root."""
    last_detail = "IBKR statement was never ready"

    for attempt in range(GET_STATEMENT_MAX_ATTEMPTS):
        if attempt:
            await asyncio.sleep(GET_STATEMENT_RETRY_DELAY)

        response = await client.get(
            IBKR_GET_STATEMENT_URL,
            params={"q": reference_code, "t": token, "v": "3"},
        )
        response.raise_for_status()

        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise HTTPException(
                status_code=502, detail=f"IBKR returned unparseable XML: {exc}"
            ) from exc

        # A still-generating report comes back as a FlexStatementResponse with
        # an error code rather than the statement payload.
        error_code = _text(root, "ErrorCode")
        if error_code:
            error_message = _text(root, "ErrorMessage") or "unknown error"
            if error_code in IBKR_NOT_READY_CODES:
                last_detail = f"IBKR statement still generating (code {error_code}): {error_message}"
                continue
            raise HTTPException(
                status_code=502,
                detail=f"IBKR statement fetch failed (code {error_code}): {error_message}",
            )

        return root

    raise HTTPException(status_code=503, detail=f"{last_detail}. Retry shortly.")


def _map_trade_confirmation(node: ET.Element) -> Optional[dict[str, Any]]:
    """Map one <TradeConfirmation> node onto the trades schema.

    Returns None when required fields are missing or malformed so the caller can
    skip the row rather than fail the whole sync.
    """
    attrs = node.attrib

    trade_id = (attrs.get("tradeID") or "").strip()
    ticker = (attrs.get("symbol") or "").strip()
    raw_price = attrs.get("price")
    raw_quantity = attrs.get("quantity")
    buy_sell = (attrs.get("buySell") or "").strip().upper()
    entry_date = _parse_ibkr_datetime(attrs.get("dateTime"))

    if not trade_id or not ticker or entry_date is None:
        return None

    try:
        price = float(raw_price)
        # Quantity arrives signed on sells; direction already encodes the side.
        quantity = abs(int(float(raw_quantity)))
    except (TypeError, ValueError):
        return None

    if buy_sell not in {"BUY", "SELL"}:
        return None

    return {
        "id": uuid.uuid4(),
        "ibkr_exec_id": trade_id[:100],
        "ticker": ticker[:10],
        "direction": buy_sell[:5],
        "style": DEFAULT_STYLE,
        "status": TradeStatus.pending_review.value,
        "entry_date": entry_date,
        "actual_entry": price,
        "quantity": quantity,
    }


@app.post("/api/sync/ibkr", response_model=SyncResult)
async def sync_ibkr(session: AsyncSession = Depends(get_session)):
    """Pull executions from the IBKR Flex Web Service into the trades table.

    Deduplicates on ibkr_exec_id via ON CONFLICT DO NOTHING, so repeated runs
    never create duplicate rows.
    """
    token, query_id = _ibkr_credentials()

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            reference_code = await _initiate_flex_request(client, token, query_id)
            root = await _fetch_flex_statement(client, token, reference_code)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"IBKR request failed: {exc}") from exc

    confirmations = root.findall(".//TradeConfirmation")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped_unparseable = 0

    for node in confirmations:
        mapped = _map_trade_confirmation(node)
        if mapped is None:
            skipped_unparseable += 1
            continue
        # Guard against the same execution appearing twice in one payload.
        if mapped["ibkr_exec_id"] in seen:
            continue
        seen.add(mapped["ibkr_exec_id"])
        rows.append(mapped)

    inserted_ids: list[str] = []
    if rows:
        stmt = (
            pg_insert(Trade)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["ibkr_exec_id"])
            .returning(Trade.ibkr_exec_id)
        )
        result = await session.execute(stmt)
        inserted_ids = [row[0] for row in result.fetchall()]
        await session.commit()

    return SyncResult(
        reference_code=reference_code,
        fills_found=len(confirmations),
        inserted=len(inserted_ids),
        skipped_duplicates=len(rows) - len(inserted_ids),
        skipped_unparseable=skipped_unparseable,
        inserted_trade_ids=inserted_ids,
    )
