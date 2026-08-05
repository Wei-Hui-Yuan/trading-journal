import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    CHAR,
    Boolean,
    Column,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    bindparam,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    union_all,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from asyncpg.exceptions import UndefinedTableError
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from auth import verify_clerk_or_cron_token, verify_clerk_token

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

    One vocabulary for every surface: the Trade Inbox checklist and the Trade
    Ledger's post-mortem both terminate at 'reviewed' -- a single
    `review_status = 'pending'` filter drives the Trade Inbox queue. The
    Analytics drawer edits the same notes/mistakes fields but opts out via
    `mark_reviewed=False`, so jotting a note on an old trade does not pull it
    out of that queue.
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


class TimeframePreset(Base):
    """A saved dashboard window (migration 021).

    The built-in YTD / 1Y / ALL pills are resolved in services.analytics and
    deliberately have no rows here: they are definitions, and a row asserting
    "1Y means 365 days" could disagree with the code that computes it.

    These are the trader's own windows, which is why they live in the database
    rather than in localStorage -- a filter you look at every day should not
    disappear because you opened the journal on a different device.

    Dates, not timestamps: a preset is a calendar range in market time, which
    is the same unit the dashboard buckets closes into. See migration 021 for
    why this is a table rather than JSONB on `app_settings`.
    """

    __tablename__ = "timeframe_presets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(Text, nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


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
    # What this fill cost to execute (migration 020). A COST, so positive is
    # money paid and negative is a rebate -- IBKR's ibCommission uses the
    # opposite sign and is negated on promotion, never abs()'d, because 10 of
    # the 328 fills on this account report a genuine rebate.
    commission = Column(Numeric(14, 6), nullable=False, default=Decimal("0"))
    # What IBKR says this fill realised, net of commission AND of every other
    # charge (migration 023). The column above stays the raw ibCommission and
    # is never derived from this one -- provenance and reconciliation are two
    # different jobs, and blending them would destroy the first.
    #
    # NULL means the broker never told us: a REPAIR- fill has no broker figure
    # by definition, and the Flex window reaches back only 365 days.
    broker_realized_pnl = Column(Numeric(12, 4), nullable=True)
    # All-in acquisition cost the broker reported (migration 024): notional
    # plus commission plus tax. Consulted only for fills that open a long,
    # which is what makes an open position's basis tie to the statement.
    broker_cost_basis = Column(Numeric(18, 8), nullable=True)
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

    # The chart you were looking at when you wrote this (migration 025). The
    # bytes are in Supabase Storage; `chart_path` is the key. Size is kept
    # here so "does this plan have a chart, and how much is it costing" is
    # answerable without an API call to Storage per plan.
    chart_path = Column(Text, nullable=True)
    chart_mime = Column(String(32), nullable=True)
    chart_bytes = Column(Integer, nullable=True)
    chart_uploaded_at = Column(DateTime(timezone=True), nullable=True)

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
    # LONG or SHORT, as the matcher computed it (migration 022). Stored rather
    # than re-derived from the opening execution at read time: that lookup can
    # come back empty, and the fallback every reader used was "long", which
    # flips the sign of entry slippage on a short.
    direction = Column(String(5), nullable=False)
    style = Column(String(50), nullable=False)
    quantity = Column(Numeric(18, 8), nullable=False)
    entry_price = Column(Numeric(10, 4), nullable=False)
    exit_price = Column(Numeric(10, 4), nullable=False)
    entry_time = Column(DateTime(timezone=True), nullable=False)
    exit_time = Column(DateTime(timezone=True), nullable=False)
    # NET of commission since migration 020: gross_pnl - commission. Every
    # statistic in the app reads this column, so net is what it has to hold --
    # a win rate computed on the price move alone counts a trade that made
    # $0.40 and paid $0.36 in commission as a full win.
    realized_pnl = Column(Numeric(12, 4), nullable=False)
    # The same figure before costs, and the costs themselves. Stored rather
    # than derived so the difference is inspectable, and so the identity
    # gross_pnl - commission = realized_pnl can be checked against the row.
    gross_pnl = Column(Numeric(12, 4), nullable=True)
    commission = Column(Numeric(12, 4), nullable=False, default=Decimal("0"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Which pair of executions produced this position. A unique index on the
    # pair makes re-running the matching engine a no-op instead of a duplicate.
    #
    # NOT NULL since migration 019, which is what makes that index trustworthy:
    # Postgres treats NULLs as distinct, so a row keyed (NULL, ...) collides
    # with nothing and the next rebuild inserts the same round trip again.
    # SET NULL is kept deliberately -- combined with NOT NULL it means deleting
    # an execution out from under a surviving position fails loudly instead of
    # leaving one behind with no fills.
    open_trade_id = Column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="SET NULL"), nullable=False
    )
    close_trade_id = Column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="SET NULL"), nullable=False
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


class RealizedLeg(Base):
    """One FIFO pairing, and the money it actually realised (migration 022).

    WHERE MONEY LIVES. `positions` gets a row only when a ticker returns to
    flat, which is the right grain for counting ideas and the wrong grain for
    cash: selling half a position banks that P&L whatever happens to the other
    half. Before this table, every dollar realised on the way out of a position
    still held was recorded nowhere at all -- MSFT alone was carrying -63.34 of
    real realised P&L that no figure in the app could see, and the year-to-date
    total was wrong by 72.34 as a result.

    It also fixes dating. A round trip's P&L used to be attributed entirely to
    its FINAL exit, so scaling out in December and closing in January put every
    dollar in January. A leg knows the date its own money was realised.

    So: MONEY (net P&L, gross, commission, the equity curve, drawdown, every
    windowed total) sums legs by `exit_time`. COUNTS (trade count, win rate,
    profit factor, ROI, grades, reviews) stay on `positions`, one row per
    completed idea. A partial exit moves the money and does not count as a
    trade.

    `position_id` is NULL when the run has not closed -- money banked, idea
    still running. Purely derived from `trades`, so a rebuild replaces a
    ticker's legs outright; there is no user-entered field to preserve.
    """

    __tablename__ = "realized_legs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol = Column(String(20), nullable=False)
    direction = Column(String(5), nullable=False)  # LONG | SHORT
    quantity = Column(Numeric(18, 8), nullable=False)
    open_trade_id = Column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="CASCADE"), nullable=False
    )
    close_trade_id = Column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="CASCADE"), nullable=False
    )
    entry_price = Column(Numeric(10, 4), nullable=False)
    exit_price = Column(Numeric(10, 4), nullable=False)
    entry_time = Column(DateTime(timezone=True), nullable=False)
    # The date every period figure buckets on.
    exit_time = Column(DateTime(timezone=True), nullable=False)
    gross_pnl = Column(Numeric(14, 4), nullable=False)
    # ALL-IN cost of this slice (migration 023): commission plus exchange,
    # clearing and regulatory charges. Derived as gross_pnl minus the broker's
    # own realised figure where there is one, so net P&L ties to the IBKR
    # statement exactly instead of to the commission column alone.
    commission = Column(Numeric(14, 4), nullable=False, default=Decimal("0"))
    # The apportioned raw ibCommission, kept beside it. "What did I pay IBKR"
    # and "what did this trade cost me in total" stay two answerable questions.
    ib_commission = Column(Numeric(14, 4), nullable=False, default=Decimal("0"))
    # The broker's figure for this leg's share of its closing fill. NULL when
    # the fill carried none, which is what `commission` falling back to
    # ib_commission is conditioned on -- and what coverage reporting counts.
    broker_realized_pnl = Column(Numeric(14, 4), nullable=True)
    realized_pnl = Column(Numeric(14, 4), nullable=False)
    position_id = Column(
        UUID(as_uuid=True), ForeignKey("positions.id", ondelete="SET NULL"), nullable=True
    )
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
    # IBKR's own realised P&L for this execution (migration 023), as sent: net
    # of commission and of every other charge. Refreshed on every sync rather
    # than written once, because IBKR re-lots occasionally and the figure for a
    # fill can change after settlement.
    fifo_pnl_realized = Column(Numeric(14, 6), nullable=True)
    # IBKR's `cost` for this fill (migration 024). All-in acquisition cost on
    # a BUY; the basis relieved on a SELL.
    broker_cost = Column(Numeric(18, 8), nullable=True)
    execution_time = Column(DateTime(timezone=True), nullable=True)
    processed = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


async def get_session():
    async with SessionLocal() as session:
        yield session


async def _warm_connection_pool() -> None:
    """Open every pooled connection up front, so no request has to.

    Opened concurrently and released immediately: each task holds a distinct
    connection at the same moment, which is what forces the pool to fill
    rather than one connection being handed round the loop.

    Never fatal. A database that is unreachable at boot must not stop the
    process from starting -- /health exists precisely to report that state,
    and it cannot report anything if the app died trying to warm a pool.
    """
    size = engine.pool.size() if hasattr(engine.pool, "size") else 0
    if not size:
        return

    async def touch() -> None:
        async with engine.connect() as connection:
            await connection.execute(select(1))

    try:
        await asyncio.gather(*(touch() for _ in range(size)))
        logger.info("Connection pool warmed: %d connections ready", size)
    except Exception as exc:  # noqa: BLE001 - startup must survive a cold database
        logger.warning(
            "Could not warm the connection pool (%s); serving anyway",
            type(exc).__name__,
        )


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

    It DOES fill the connection pool, because the first page load of a session
    otherwise pays for it. The dashboard issues six requests at once and the
    pool starts empty, so each one opens its own connection to Supabase --
    measured at ~89ms per handshake, turning a 107ms burst into a 341ms one.
    Doing it here moves that cost into container startup, where nobody is
    waiting. Northflank restarts the container often enough on the free tier
    that this is not a one-time saving.
    """
    await _warm_connection_pool()
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
    # Genuine fills IBKR reported with no execution time, THIS RUN. Promoting
    # one with a fabricated `datetime.now()` would insert it whenever this
    # sync happened to run rather than when it actually filled, corrupting
    # FIFO match order and misplacing it on the heatmap. Left unpromoted and
    # unprocessed instead, so a future sync with a corrected statement can
    # pick it up. Can overlap with skipped_unpriced below -- a fill missing
    # both fields counts in both.
    skipped_undated: int = 0
    # Genuine fills with no price, THIS RUN. Same treatment: promoting one
    # would price the trade at 0, so it is left out of `trades` and its
    # staging row left unprocessed rather than marked done for work that was
    # never performed.
    skipped_unpriced: int = 0
    # The true number of fills not promoted THIS RUN -- the union of the two
    # counters above, not their sum. A fill missing both price and
    # execution_time is one skipped fill, not two, and this is the number that
    # answers "how many fills did this sync actually fail to import".
    skipped_unusable: int = 0
    # How many staging rows, RIGHT NOW, can never earn `processed = True` --
    # queried fresh each sync rather than counted from this run's statement,
    # because the condition is standing, not an event. Stays nonzero across
    # every subsequent sync until IBKR resends the fill corrected or it is
    # added by hand via the repair-fill modal. See `_stranded_fills`.
    stranded_fills: int = 0
    # The distinct tickers those rows belong to, so the user knows where to
    # look without a database query.
    stranded_symbols: list[str] = []
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
    # Round trips that existed before this sync and no longer survive
    # re-matching, because a fill arrived dated earlier than ones already
    # stored and re-partitioned the FIFO queue. Nearly always zero; when it is
    # not, P&L and trade count have just changed for reasons the fill list
    # alone does not explain, so it is reported rather than left to be noticed.
    positions_removed: int = 0
    # How many of those carried a review. This is the part that cannot be
    # reconstructed, so it gets its own number.
    reviews_discarded: int = 0
    # Tickers this sync re-matched because a PREVIOUS run promoted their fills
    # and then died before building positions from them. Normally empty, and
    # never something to discover silently: a non-empty list means the ledger
    # had been under-reporting until this run, and every figure derived from
    # those tickers has just moved.
    symbols_recovered: list[str] = []


def _promoted_into_trades():
    """True exactly when some `trades` row was promoted from this staging row.

    The one definition of "promoted", shared by the processed-flag update in
    `ingest_ibkr` and the resume/stranded split below. They must never drift
    onto different predicates -- if the update used a looser one than this
    function, it could mark a row `processed` that this function still
    considers outstanding, or vice versa, and the two would disagree about
    which rows still need attention.
    """
    return exists().where(
        Trade.ibkr_exec_id == "IBKR-" + IBKRExecution.transaction_id
    )


async def _symbols_awaiting_match(session: AsyncSession) -> list[str]:
    """Tickers with staged fills that were promoted but no position built yet.

    The resume set. A staging row is marked `processed` only after FIFO has run
    for its symbol, so anything still unmarked AND backed by a `trades` row is
    work an earlier run started and did not finish -- promoted, then
    interrupted before the round trips were built. Migration 005 added the
    column for exactly this and indexed it; it was simply being set too early
    to serve.

    Deliberately not "every ticker with a fill that has no position_fills row".
    That reads as the same question and is not: an OPEN position's fills have
    no position_fills row by definition, so it returns every ticker holding a
    running trade, on every sync, forever -- and cannot distinguish one from a
    fill that was genuinely stranded. Measured here it names three tickers
    where the right answer is none, and rebuilding those three costs ~146ms of
    round trips per sync that buys nothing. The queries themselves are within
    5ms of each other; the waste is the work they trigger, and it grows with
    the number of positions held open.

    Also deliberately not "every unprocessed staging row" any more. A row with
    no execution_time or no price is unprocessed forever -- ingest_ibkr's step
    4 promotes neither into `trades` on purpose -- and including it here fed it
    into `recovered`, which told the UI a ticker's fills "were imported but
    never matched" when they were never imported at all. See
    `_stranded_fills` for that set.
    """
    return sorted(
        set((
            await session.execute(
                select(IBKRExecution.symbol)
                # `is_not(True)`, not `== False`: the column is nullable and a
                # NULL means "never marked", which is the state being sought.
                .where(
                    IBKRExecution.processed.is_not(True),
                    _promoted_into_trades(),
                )
                .distinct()
            )
        ).scalars().all())
    )


async def _stranded_fills(session: AsyncSession) -> list[str]:
    """One symbol per staging row stranded by a data-quality problem IBKR sent.

    Deliberately narrower than "unprocessed and no backing trade" -- that
    weaker predicate also matches a fill that WAS priced and dated, WAS
    promoted, and was then deliberately deleted: `delete_trade` removes the
    `trades` row and tombstones it in `suppressed_executions`, but never
    touches the staging row, so an execution deleted before its symbol's
    processed-flag update could run (an ingest crash mid-run, followed by the
    user deleting the just-promoted trade before the next sync resumes it) is
    unprocessed with no backing trade for a reason that has nothing to do with
    what IBKR sent. Requiring price or execution_time to actually be NULL is
    what distinguishes "IBKR never gave us enough to import this" from "this
    was imported and removed on purpose" -- a suppressed fill like that now
    matches neither this function nor `_symbols_awaiting_match`, which is
    correct: there is nothing left to report about it either way.

    A hand-repair does not clear a genuine match here: the repair-fill modal
    mints a fresh `REPAIR-<uuid>` id for the correction, never
    `IBKR-<transaction_id>`, so it cannot satisfy `_promoted_into_trades()`.
    The only way one of these clears is IBKR resending the same
    transaction_id with the missing field filled in, inside the rolling Flex
    query window.

    Returns one entry per stranded ROW, not deduplicated by symbol -- callers
    read `len(...)` as the fill count and `sorted(set(...))` as the affected
    tickers, without a second query.
    """
    return (
        await session.execute(
            select(IBKRExecution.symbol).where(
                IBKRExecution.processed.is_not(True),
                or_(
                    IBKRExecution.price.is_(None),
                    IBKRExecution.execution_time.is_(None),
                ),
                ~_promoted_into_trades(),
            )
        )
    ).scalars().all()


async def _stranded_summary(session: AsyncSession) -> tuple[int, list[str]]:
    """(count, symbols) for `_stranded_fills`, queried fresh.

    The STANDING total `IngestResult.stranded_fills` / `stranded_symbols`
    report -- not a per-run delta like `skipped_undated` / `skipped_unpriced`
    below, which count only what THIS run's statement contained. A stranded
    row sits in the ledger across every sync until it is resolved, so the
    count queried here does too.
    """
    rows = await _stranded_fills(session)
    return len(rows), sorted(set(rows))


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
        # Queried even on an empty run: stranded_fills is a standing condition
        # of the ledger, not an event this run produced, so it must not go
        # quiet just because IBKR had nothing new to report.
        stranded_count, stranded_syms = await _stranded_summary(session)
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
            stranded_fills=stranded_count,
            stranded_symbols=stranded_syms,
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

    # --- 3b: refresh the broker's own realised P&L ------------------------
    #
    # Deliberately a SEPARATE statement from the staging insert above, not a
    # DO UPDATE on it. That insert's RETURNING clause is what decides which
    # executions are new and therefore get promoted; upgrading it to DO UPDATE
    # would return every row and re-promote the entire history on every sync.
    #
    # Run over ALL parsed executions rather than just the new ones, which is
    # what makes an ordinary sync self-healing: fills ingested before migration
    # 023, or before the Flex query exposed the field, acquire their broker
    # figure the next time a statement covers them. IBKR also re-lots
    # occasionally, so the value is refreshed rather than written once.
    broker_pnl = [
        {"tid": e.transaction_id, "pnl": e.fifo_pnl_realized, "cost": e.broker_cost}
        for e in executions
        if e.fifo_pnl_realized is not None or e.broker_cost is not None
    ]
    if broker_pnl:
        # Core updates against `__table__`, not the ORM classes. Handed an ORM
        # entity plus a list of dicts, SQLAlchemy reads it as a bulk update BY
        # PRIMARY KEY and rejects rows that carry none -- these are keyed on
        # the broker's transaction id, not on `id`. Targeting the table is what
        # keeps this an ordinary executemany.
        await session.execute(
            update(IBKRExecution.__table__)
            .where(IBKRExecution.__table__.c.transaction_id == bindparam("tid"))
            .values(
                fifo_pnl_realized=bindparam("pnl"),
                broker_cost=bindparam("cost"),
            ),
            broker_pnl,
        )
        # And onto the ledger, for rows that already existed. New rows carry it
        # from the promotion below; this catches everything older.
        await session.execute(
            update(Trade.__table__)
            .where(
                Trade.__table__.c.ibkr_exec_id
                == "IBKR-" + IBKRExecution.__table__.c.transaction_id,
                or_(
                    IBKRExecution.__table__.c.fifo_pnl_realized.is_not(None),
                    IBKRExecution.__table__.c.broker_cost.is_not(None),
                ),
            )
            .values(
                broker_realized_pnl=IBKRExecution.__table__.c.fifo_pnl_realized,
                broker_cost_basis=IBKRExecution.__table__.c.broker_cost,
            )
        )

    # --- 4: promote into the trades ledger --------------------------------
    #
    # Undated and unpriced fills are both left out, and both for the same
    # reason: there is no honest value to promote them with. A fabricated
    # `datetime.now()` would insert an old fill wherever this sync happened to
    # run, corrupting FIFO match order and misplacing it on the heatmap; a
    # fabricated price of 0 would misprice the trade outright. Logged rather
    # than silently dropped -- a fill missing either field is a data-quality
    # problem in what IBKR sent, worth a human's attention via the repair-fill
    # modal, not a routine skip. Left unprocessed in staging (below) so a
    # later statement carrying the correction is not treated as a duplicate.
    skipped_undated_execs = [e for e in new_executions if e.execution_time is None]
    skipped_unpriced_execs = [e for e in new_executions if e.price is None]
    # The union, not the sum of the two lists above -- a fill missing both
    # fields appears in both and must still count once here.
    skipped_unusable_execs = [
        e for e in new_executions
        if e.execution_time is None or e.price is None
    ]
    if skipped_undated_execs:
        logger.error(
            "Ingest: %d execution(s) had no execution_time and were not "
            "promoted to trades: %s",
            len(skipped_undated_execs),
            [e.transaction_id for e in skipped_undated_execs],
        )
    if skipped_unpriced_execs:
        logger.error(
            "Ingest: %d execution(s) had no price and were not promoted to "
            "trades: %s",
            len(skipped_unpriced_execs),
            [e.transaction_id for e in skipped_unpriced_execs],
        )

    trade_rows = [
        {
            "id": uuid.uuid4(),
            # Namespaced so a manual entry can never collide with a broker fill.
            "ibkr_exec_id": f"IBKR-{e.transaction_id}"[:100],
            "ticker": e.symbol[:10],
            "direction": e.side,
            "style": UNCLASSIFIED_STYLE,
            "entry_date": e.execution_time,
            "actual_entry": e.price,
            # Side lives in `direction`; store magnitude only.
            "quantity": e.abs_quantity,
            # Sign flipped exactly once, here, from IBKR's debit convention to
            # a cost the engine can subtract. See commission_cost.
            "commission": e.commission_cost,
            # Carried as IBKR states it -- already net of every charge, and a
            # P&L rather than a cost, so no sign flip applies.
            "broker_realized_pnl": e.fifo_pnl_realized,
            "broker_cost_basis": e.broker_cost,
            "source_tag": "IBKR",
        }
        for e in new_executions
        if e.price is not None and e.execution_time is not None
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

    # --- 4b: match new fills against open plans ---------------------------
    # Before matching, so a position is built from trades that already carry
    # the stop and target their plan specified.
    plans_attached = await _auto_attach_plans(session, created_ids)

    await session.commit()

    # --- 5: re-run FIFO for every affected symbol -------------------------
    #
    # This run's symbols, PLUS anything a previous run left unfinished.
    #
    # The staging rows are the record of what still needs matching, which is
    # what `processed` was added for -- migration 005 documents it as existing
    # so "a partial failure mid-ingest can be resumed", and indexes it. It was
    # being set in the transaction ABOVE, before the work it guards had run, so
    # it prevented the resume instead of enabling it: a crash between that
    # commit and this loop left the fills in `trades` with no positions built
    # from them, and the next sync could not find them. `staged_ids` holds only
    # rows that were new to staging, so re-fetching the same executions returns
    # nothing and the tickers are never revisited. The fills sit in the ledger
    # as phantom open exposure that no statistic can see.
    #
    # Deliberately NOT "every ticker with a fill that has no position_fills
    # row". That looks equivalent and is not: an OPEN position's fills have no
    # position_fills row by definition, so it re-matches every open ticker on
    # every sync forever -- 3 of them here, permanently, growing with the book
    # -- and cannot tell a stranded fill from a running trade. This query is
    # exact, hits idx_ibkr_exec_processed, and normally returns nothing.
    #
    recovered = sorted(
        set(await _symbols_awaiting_match(session))
        - {e.symbol for e in new_executions}
    )
    symbols = sorted({e.symbol for e in new_executions} | set(recovered))

    positions_matched = 0
    positions_removed = 0
    reviews_discarded = 0
    for symbol in symbols:
        result = await run_matching_for_ticker(session, symbol, persist=True)
        positions_matched += len(result.positions)
        # The two Flex queries disagree by design -- TCF reports today, the
        # Activity query lags a day -- so an older fill routinely arrives after
        # a newer one has already matched, re-partitioning that ticker's queue.
        positions_removed += result.positions_removed
        reviews_discarded += result.reviews_discarded

    # Only now, and only rows `_promoted_into_trades()` actually finds a
    # `trades` row for. A staged row earns `processed = True` by being
    # promoted -- not by being staged (an unpriced or undated fill is staged
    # but deliberately never promoted, above; marking it processed here would
    # be I6 again: the next sync would see a duplicate in staging, not the
    # still-missing fill it actually is) and not by its ticker being one of
    # `symbols` alone (a symbol can hold both promoted and permanently
    # unpromotable rows side by side, and `symbols` is scoped by ticker, not
    # by row -- the EXISTS check is what keeps this update from sweeping the
    # unpromoted one up as a side effect of its neighbours resolving; as of
    # the resume/stranded split above, `_symbols_awaiting_match` also no
    # longer offers a permanently-stranded ticker as a reason to re-touch this
    # symbol, but the EXISTS check remains the actual guarantee, not that).
    # Its own commit, after the loop, so a crash at any point above leaves the
    # flag unset and the next sync picks the work back up.
    if symbols:
        await session.execute(
            update(IBKRExecution)
            .where(
                IBKRExecution.symbol.in_(symbols),
                IBKRExecution.processed.is_not(True),
                _promoted_into_trades(),
            )
            .values(processed=True)
        )
        await session.commit()

    # Queried fresh after the update above, so a row THIS run just promoted
    # (and is therefore no longer stranded) is not still counted.
    stranded_count, stranded_syms = await _stranded_summary(session)

    return IngestResult(
        executions_parsed=len(executions),
        staged_new=len(staged_ids),
        staged_duplicates=len(executions) - len(staged_ids),
        trades_created=len(created_ids),
        trades_duplicates=len(trade_rows) - len(created_ids),
        positions_matched=positions_matched,
        symbols_touched=symbols,
        skipped_non_tradeable=skipped_non_tradeable,
        skipped_undated=len(skipped_undated_execs),
        skipped_unpriced=len(skipped_unpriced_execs),
        skipped_unusable=len(skipped_unusable_execs),
        stranded_fills=stranded_count,
        stranded_symbols=stranded_syms,
        queries_failed=query_failures,
        rate_limited=any(ibkr_client.is_transient_failure(f) for f in query_failures),
        suppressed_skipped=resurrected,
        plans_attached=plans_attached,
        positions_removed=positions_removed,
        reviews_discarded=reviews_discarded,
        symbols_recovered=recovered,
    )


class RematchResult(BaseModel):
    """What re-running FIFO changed."""

    tickers: list[str]
    positions_matched: int
    positions_removed: int
    reviews_discarded: int


@app.post(
    "/api/rematch",
    response_model=RematchResult,
    dependencies=[Depends(verify_clerk_token)],
)
async def rematch(
    ticker: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """Rebuild round trips from the executions currently in the ledger.

    A repair tool, not part of any page load. The sync recovers from its own
    interruptions now -- unmatched staging rows are picked up by the next run
    -- but that only covers fills whose staging rows are still unprocessed. A
    ledger already in that state before the fix, or one edited directly, has no
    other way back.

    Safe to run at any time: matching is authoritative and idempotent, so a
    ticker whose positions already agree with its fills is left byte-identical,
    reviews and grades included. Anything reported as removed was a round trip
    FIFO no longer produces.

    Without `ticker` this walks every symbol in the ledger, which costs a few
    database round trips each -- fine for a repair, too slow to put on a page.
    Pass a ticker when you know which one is wrong.
    """
    from services.matching_engine import (  # noqa: PLC0415 - avoids import cycle
        run_matching_for_ticker,
    )

    if ticker:
        wanted = ticker.strip().upper()
        exists = (
            await session.execute(
                select(Trade.id).where(Trade.ticker == wanted).limit(1)
            )
        ).scalars().first()
        if exists is None:
            raise HTTPException(
                status_code=404, detail=f"No executions in the ledger for {wanted}."
            )
        tickers = [wanted]
    else:
        tickers = sorted(
            set((await session.execute(select(Trade.ticker).distinct())).scalars().all())
        )

    matched = removed = discarded = 0
    for symbol in tickers:
        result = await run_matching_for_ticker(session, symbol, persist=True)
        matched += len(result.positions)
        removed += result.positions_removed
        discarded += result.reviews_discarded

    if removed:
        logger.warning(
            "Manual re-match over %d ticker(s) removed %d round trip(s) and "
            "discarded %d review(s).",
            len(tickers), removed, discarded,
        )

    return RematchResult(
        tickers=tickers,
        positions_matched=matched,
        positions_removed=removed,
        reviews_discarded=discarded,
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
    # What the fill cost to execute. A repair fill stands in for a broker
    # execution that really happened, and that execution was charged -- leaving
    # this at zero makes the repaired trade look cheaper than its neighbours.
    # Not bounded below: a rebate is a legitimate negative cost.
    commission: Optional[float] = 0.0

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
    # A repair fill is backdated by definition, and a fill inserted before
    # existing ones re-partitions the FIFO queue. Round trips that no longer
    # survive that re-match are removed rather than left to double-count, and
    # said out loud here because the P&L and review they carried are gone.
    positions_removed: int = 0
    reviews_discarded: int = 0


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
        # Already a cost in the form's terms -- the user types what they paid.
        commission=Decimal(str(params.commission or 0)),
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

    # No commit here. This used to commit the trade alone before calling the
    # matcher, which is the same shape update_execution, delete_trade and
    # delete_position were fixed away from (see their docstrings): a crash
    # between that commit and the rebuild's own -- a redeploy, an OOM, a
    # dropped Supabase connection -- left the trade permanently committed with
    # no round trip built from it. That is worse here than on those three
    # handlers, because a REPAIR- id never satisfies `_promoted_into_trades()`,
    # so `_symbols_awaiting_match` cannot find it and no ordinary sync will ever
    # revisit this ticker on its own; the only way back is a human noticing and
    # calling POST /api/rematch by hand.
    #
    # `session.add` only stages the row; nothing about it needs its own commit
    # to reach the matcher's read. Autoflush (never disabled on this session)
    # issues the INSERT ahead of any query in the same transaction, so
    # load_executions_for_ticker sees it without a round trip having happened,
    # and `expire_on_commit=False` means every attribute read below stays valid
    # off the Python object after run_matching_for_ticker's own commit.
    #
    # Re-run matching for this ticker. Authoritative rather than additive: this
    # fill is almost certainly backdated -- that is what repairing a dropped
    # execution means -- and a fill inserted before existing ones re-partitions
    # the FIFO queue, so round trips stored from an earlier run may no longer
    # exist. Those are removed here instead of lingering as double-counted P&L.
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
        positions_removed=result.positions_removed,
        reviews_discarded=result.reviews_discarded,
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
    # Whether a chart screenshot is attached, and what it costs. The path
    # itself is deliberately NOT sent: it is a private-bucket key, useless to
    # a browser, and the image is served from this API instead
    # (GET /api/plans/{id}/chart).
    has_chart: bool = False
    chart_bytes: Optional[int] = None
    chart_uploaded_at: Optional[datetime] = None


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
        has_chart=plan.chart_path is not None,
        chart_bytes=plan.chart_bytes,
        chart_uploaded_at=plan.chart_uploaded_at,
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
    `risk_amount` and `risk_percent` are derived for the same reason.
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
    await _derive_plan_risk(session, plan)
    session.add(plan)
    await session.commit()
    # planned_r and the timestamps were produced by the database; re-read
    # rather than report what we sent, which did not include them.
    await session.refresh(plan)
    return _plan_out(plan)


async def _derive_plan_risk(session: AsyncSession, plan: PlannedTrade) -> None:
    """Make a plan's risk agree with the plan's own numbers.

    A plan's risk is not an opinion: entry, stop and quantity determine it
    completely. `risk_amount` was nevertheless stored as whatever the sizing
    calculator computed when the plan was first written, and PATCH applied only
    the fields it was sent -- so editing the quantity from 4 to 1 left the
    figure behind. The dock showed "Risk $20.00" on a plan whose own numbers
    risked $5.00, with the entry, stop and quantity that contradict it printed
    on the line below.

    That is not only a display problem. When a plan attaches to a fill it
    copies `risk_amount` onto the trade, and that column is what converts an
    R-multiple back into money -- so a stale figure would have reported the
    trade's R in dollars four times too large, permanently.

    Deliberately unlike `trades.risk_amount`, which is a snapshot and must
    never be recomputed: a trade sized against a $2,500 account has to keep
    reading as 1% of $2,500 after the account grows. A plan is a live
    intention, not history, so it should always describe what it would do if
    taken right now.

    Null rather than zero when it cannot be known -- no stop, no quantity, or
    a stop on the wrong side of the entry. A long stopped above its entry has
    no risk to state, and a negative one would read as a guaranteed profit.
    """
    entry, stop, quantity = plan.planned_entry, plan.stop_loss, plan.quantity

    per_share = None
    if entry is not None and stop is not None:
        per_share = entry - stop if plan.direction == "BUY" else stop - entry

    if per_share is None or per_share <= 0 or quantity is None or quantity <= 0:
        plan.risk_amount = None
        plan.risk_percent = None
        return

    plan.risk_amount = (per_share * quantity).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    # Percent needs the account it is a percent OF. Nulled rather than left
    # standing when that is unavailable: a percentage carried over from a
    # different risk_amount is worse than no percentage.
    account_size = (
        await session.execute(select(AppSetting.account_size))
    ).scalars().first()
    plan.risk_percent = (
        (plan.risk_amount / account_size * 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if account_size
        else None
    )


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

    # After the edit, never before: quantity, entry and stop are exactly what
    # risk is computed from, and applying only the fields that were sent is
    # what let the stored figure drift away from them.
    await _derive_plan_risk(session, plan)

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


# ---------------------------------------------------------------------------
# The chart behind the plan
# ---------------------------------------------------------------------------
#
# A plan records the levels and the thesis -- what you intended. For a
# discretionary setup that is only half the decision: "reclaiming the 50 EMA
# after basing three days" is a sentence, and whether the base was actually
# there is a picture. Reviewing the trade later without it grades the
# sentence rather than the decision.
#
# Bytes live in Supabase Storage, keyed by plan id; `planned_trades` keeps the
# key (migration 025). See services/storage.py for why not Postgres.

# What the browser compressor produces. WebP is the normal output -- lossless,
# and measurably the smallest encoding for flat-background chart UI -- with
# PNG as the fallback where WebP encoding is unavailable. Anything else means
# the client skipped the compressor, which is worth refusing rather than
# storing: an unbounded original is exactly what the compressor exists to
# prevent reaching the bucket.
CHART_MIME_EXTENSIONS = {"image/webp": "webp", "image/png": "png"}

# A generous ceiling, not a target. A losslessly-encoded chart screenshot
# measures in the hundreds of kilobytes; anything approaching this means the
# client sent an original rather than a compressed copy. Bounded so one
# request cannot take a worker's memory with it.
CHART_MAX_BYTES = 8 * 1024 * 1024


def _chart_etag(plan: PlannedTrade) -> str:
    """Identity of the CURRENT image for this plan.

    Both parts are needed. The path alone is stable across re-uploads -- it is
    derived from the plan id -- so a corrected screenshot would keep serving
    from cache as the old one. Upload time plus size changes whenever the
    bytes do.
    """
    stamp = plan.chart_uploaded_at.isoformat() if plan.chart_uploaded_at else "0"
    return f'"{plan.id}-{stamp}-{plan.chart_bytes}"'


@app.post(
    "/api/plans/{plan_id}/chart",
    response_model=PlanOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def upload_plan_chart(
    plan_id: uuid.UUID,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    """Attach (or replace) the chart screenshot for a plan."""
    from services import storage  # noqa: PLC0415 - deferred, keeps import graph flat

    plan = await session.get(PlannedTrade, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in CHART_MIME_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Charts are stored as WebP or PNG, not {content_type or 'an unnamed type'}. "
                "The browser converts your screenshot before uploading."
            ),
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="That file was empty.")
    if len(data) > CHART_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"That image is {len(data) / 1024 / 1024:.1f} MB, over the "
                f"{CHART_MAX_BYTES // 1024 // 1024} MB limit. It should have been "
                "compressed in the browser first."
            ),
        )

    # Keyed by plan id, so re-uploading overwrites in place rather than
    # accumulating a new object per correction. The extension tracks the mime
    # so the object is self-describing in the dashboard.
    path = f"{plan_id}.{CHART_MIME_EXTENSIONS[content_type]}"

    # Storage first, database second. The reverse order can commit a plan that
    # claims a chart whose bytes never arrived -- a broken image with no way to
    # tell it from a slow one. This order's failure mode is an object with no
    # row pointing at it, which the next upload overwrites, because the key is
    # deterministic.
    try:
        await storage.upload(path, data, content_type)
    except storage.StorageError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))

    previous = plan.chart_path
    plan.chart_path = path
    plan.chart_mime = content_type
    plan.chart_bytes = len(data)
    plan.chart_uploaded_at = datetime.now(timezone.utc)
    plan.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(plan)

    # Replacing a PNG with a WebP (or the reverse) changes the extension, so
    # the old object is a different key and would otherwise linger unreferenced.
    if previous and previous != path:
        try:
            await storage.delete(previous)
        except storage.StorageError as exc:
            # Not fatal: the new chart is stored and recorded. Logged because
            # the leftover consumes bucket quota with nothing pointing at it.
            logger.warning(
                "Replaced chart for plan %s but could not remove the previous "
                "object %s: %s", plan_id, previous, exc,
            )

    attached = await _attached_ids_by_plan(session, [plan.id])
    return _plan_out(plan, attached.get(plan.id, []))


@app.get(
    "/api/plans/{plan_id}/chart",
    dependencies=[Depends(verify_clerk_token)],
    responses={200: {"content": {"image/webp": {}, "image/png": {}}}},
)
async def get_plan_chart(
    plan_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Serve the plan's chart.

    Proxied through this API rather than handed out as a signed Storage URL.
    The bucket stays private with no policies to maintain, the image inherits
    the same Clerk auth as every other endpoint, and there is no expiry to
    outlive a page that is already open.
    """
    from services import storage  # noqa: PLC0415

    plan = await session.get(PlannedTrade, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")
    if not plan.chart_path:
        raise HTTPException(status_code=404, detail="This plan has no chart attached.")

    # Revalidation rather than a fixed lifetime: charts are immutable in
    # practice but replaceable in principle, so the browser asks and usually
    # gets a bodiless 304 instead of re-downloading. That is what keeps
    # repeated ledger views off the egress allowance.
    etag = _chart_etag(plan)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})

    try:
        stored = await storage.download(plan.chart_path)
    except storage.StorageError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))

    return Response(
        content=stored.data,
        media_type=plan.chart_mime or stored.content_type,
        headers={
            "ETag": etag,
            # private: this is one person's trading journal, and any shared
            # cache between them and the internet has no business holding it.
            "Cache-Control": "private, max-age=300, must-revalidate",
        },
    )


@app.delete(
    "/api/plans/{plan_id}/chart",
    response_model=PlanOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def delete_plan_chart(
    plan_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    """Remove the chart, keeping the plan."""
    from services import storage  # noqa: PLC0415

    plan = await session.get(PlannedTrade, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found.")

    if plan.chart_path:
        # Storage first again, and for the same reason as the upload: this
        # order can leave a row pointing at nothing only if the commit below
        # fails, which the GET reports honestly. The reverse order leaves an
        # object nothing references -- quota consumed by something the user
        # can no longer see or delete.
        try:
            await storage.delete(plan.chart_path)
        except storage.StorageError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))

        plan.chart_path = None
        plan.chart_mime = None
        plan.chart_bytes = None
        plan.chart_uploaded_at = None
        plan.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(plan)

    attached = await _attached_ids_by_plan(session, [plan.id])
    return _plan_out(plan, attached.get(plan.id, []))


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

    model_config = ConfigDict(from_attributes=True)


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
# Saved timeframe presets
# ---------------------------------------------------------------------------
#
# Custom dashboard windows, stored so they follow the trader between devices.
# The built-in YTD / 1Y / ALL pills are NOT here -- they are resolved in
# services.analytics, because a definition stored as data can disagree with the
# code that computes it.


class TimeframePresetIn(BaseModel):
    """A window the trader wants to keep.

    Both dates are required. A half-open saved preset ("since March", no end)
    would mean something different every time it was opened, which is the one
    thing a *saved* window must not do -- the ad-hoc query parameters on the
    dashboard still accept an open end for exactly that use.
    """

    name: str = Field(..., min_length=1, max_length=60)
    start_date: date
    end_date: date

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("name cannot be blank")
        return trimmed

    @model_validator(mode="after")
    def _range_is_forwards(self) -> "TimeframePresetIn":
        # Rejected here so the message names the field, rather than surfacing
        # as timeframe_presets_range_check in a 500 the client cannot read.
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date")
        return self


class TimeframePresetOut(BaseModel):
    id: uuid.UUID
    name: str
    start_date: date
    end_date: date
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


def _duplicate_preset_name(name: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            f"A timeframe named '{name}' already exists. Names are compared "
            "ignoring case and surrounding spaces, so two pills cannot look "
            "identical."
        ),
    )


@app.get(
    "/api/settings/timeframes",
    response_model=list[TimeframePresetOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_timeframes(session: AsyncSession = Depends(get_session)):
    """Saved custom windows, oldest first.

    Oldest first so the toolbar is stable: pills keep their position as new
    ones are added, and the one you reach for by muscle memory does not move.

    A missing table degrades to an empty list rather than breaking the whole
    dashboard toolbar, matching how list_disciplines handles migration 013/014
    not having been applied. Only UndefinedTable is swallowed -- anything else
    propagates, because "you have no saved timeframes" and "the database is
    unreachable" must not look the same.
    """
    try:
        rows = (
            await session.execute(
                select(TimeframePreset).order_by(
                    TimeframePreset.created_at.asc(), TimeframePreset.id.asc()
                )
            )
        ).scalars().all()
    except ProgrammingError as exc:
        if not isinstance(getattr(exc, "orig", None), UndefinedTableError):
            raise
        logger.warning("timeframe_presets is missing; apply migration 021")
        await session.rollback()
        return []

    return [TimeframePresetOut.model_validate(row) for row in rows]


@app.post(
    "/api/settings/timeframes",
    response_model=TimeframePresetOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_clerk_token)],
)
async def create_timeframe(
    params: TimeframePresetIn, session: AsyncSession = Depends(get_session)
):
    """Save a custom window."""
    preset = TimeframePreset(
        id=uuid.uuid4(),
        name=params.name,
        start_date=params.start_date,
        end_date=params.end_date,
    )
    session.add(preset)
    try:
        await session.commit()
    except IntegrityError:
        # uq_timeframe_presets_name, on lower(btrim(name)).
        await session.rollback()
        raise _duplicate_preset_name(params.name)
    except ProgrammingError as exc:
        await session.rollback()
        if not isinstance(getattr(exc, "orig", None), UndefinedTableError):
            raise
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The timeframe_presets table does not exist. Apply migration "
                "021_timeframe_presets.sql to this database."
            ),
        )
    await session.refresh(preset)
    return TimeframePresetOut.model_validate(preset)


@app.put(
    "/api/settings/timeframes/{preset_id}",
    response_model=TimeframePresetOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def update_timeframe(
    preset_id: uuid.UUID,
    params: TimeframePresetIn,
    session: AsyncSession = Depends(get_session),
):
    """Rename a saved window or move its dates.

    A full replacement rather than a patch: the three fields are one statement
    about a window, and letting an end date be edited without its start in
    view is how a range ends up backwards.
    """
    preset = await session.get(TimeframePreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="Timeframe not found.")

    preset.name = params.name
    preset.start_date = params.start_date
    preset.end_date = params.end_date
    preset.updated_at = datetime.now(timezone.utc)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise _duplicate_preset_name(params.name)

    await session.refresh(preset)
    return TimeframePresetOut.model_validate(preset)


@app.delete(
    "/api/settings/timeframes/{preset_id}",
    response_model=TimeframePresetOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def delete_timeframe(
    preset_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    """Remove a saved window.

    Hard delete, unlike a cancelled plan. A plan you talked yourself out of is
    evidence about your process; a filter you no longer want is furniture, and
    keeping tombstones for it would only clutter the toolbar's query.

    Returns the deleted row so the client can name it in a confirmation
    without having to have held onto it.
    """
    preset = await session.get(TimeframePreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="Timeframe not found.")

    out = TimeframePresetOut.model_validate(preset)
    await session.delete(preset)
    await session.commit()
    return out


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

    model_config = ConfigDict(from_attributes=True)


async def _strategy_usage(session: AsyncSession) -> dict[uuid.UUID, StrategyUsage]:
    """Reference counts per strategy, in a single round trip.

    One UNION ALL rather than three separate GROUP BYs, and certainly not a
    query per playbook entry. The shape matters more than it looks: this API
    container sits far from its database -- a bare `SELECT 1` measures ~1.2s
    from inside it against ~15ms from a host near Supabase -- so latency here
    is paid per ROUND TRIP, not per row. Three queries over nine strategies
    and a few hundred rows cost three times as much as one, entirely in
    waiting.
    """
    parts = [
        select(
            model.strategy_id.label("strategy_id"),
            literal(label).label("kind"),
            func.count().label("n"),
        )
        .where(model.strategy_id.is_not(None))
        .group_by(model.strategy_id)
        for model, label in (
            (Trade, "trades"),
            (Position, "positions"),
            (PlannedTrade, "plans"),
        )
    ]

    rows = (await session.execute(union_all(*parts))).all()

    usage: dict[uuid.UUID, StrategyUsage] = {}
    for strategy_id, kind, count in rows:
        entry = usage.setdefault(strategy_id, StrategyUsage())
        setattr(entry, kind, count)

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

    # Captured before the commit is attempted, not after it fails: a failed
    # flush expires the ORM instance immediately (not just on the rollback()
    # below), so reading strategy.name from the except block would need a
    # lazy reload the async session cannot do implicitly -- it raises
    # PendingRollbackError before rollback() and MissingGreenlet after it.
    # This is the name the update is actually about either way: the new
    # value if `name` was in the payload, or the untouched existing one if it
    # was not -- unlike params.name, which is None on a partial update that
    # never touched name, and would name "None" as the conflict instead of
    # the value the constraint actually concerns.
    attempted_name = strategy.name

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A strategy named '{attempted_name}' already exists",
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

    model_config = ConfigDict(from_attributes=True)


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

    # Completing the checklist is what empties the Trade Inbox queue, so it
    # defaults on for that surface and the Trade Ledger's post-mortem without
    # either having to ask for it. The Analytics drawer sends only `notes`
    # and `mistakes` -- jotting a note there must not silently complete the
    # review of a trade nobody has looked at yet, so it sends `false`.
    mark_reviewed: bool = True

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
    # NET of commission (migration 020). Reported alongside the gross figure
    # and the cost so the difference is visible rather than implied -- a P&L
    # that quietly means one thing on the broker statement and another here is
    # the reason this journal did not reconcile.
    realized_pnl: float
    gross_pnl: Optional[float] = None
    commission: Optional[float] = None
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

    model_config = ConfigDict(from_attributes=True)


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

    model_config = ConfigDict(from_attributes=True)


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

    model_config = ConfigDict(from_attributes=True)


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


def _position_side(position: Position) -> str:
    """A round trip's direction in the API's BUY/SELL vocabulary.

    `positions.direction` stores LONG/SHORT, which is what a position actually
    is and what the matcher computes. The journal and the analytics layer speak
    the execution's language -- BUY opened it, SELL opened a short -- and the
    frontend keys off that, so the two are mapped here rather than at each of
    the places that used to reconstruct the answer from the opening fill.

    Total by construction: a CHECK constraint admits only the two values, so
    there is no third case and no reason for a fallback. That fallback is the
    bug this replaced -- it guessed "long", which flips the sign of entry
    slippage on a short.
    """
    return "SELL" if (position.direction or "").upper() == "SHORT" else "BUY"


async def _consumed_quantity_by_trade(
    session: AsyncSession, trade_ids: Optional[list[uuid.UUID]] = None
) -> dict[uuid.UUID, Decimal]:
    """How much of each execution FIFO has folded into closed round trips.

    Quantity, not membership. An execution can be PARTLY consumed: an oversell
    closes the long it was aimed at and opens a short with whatever is left
    over, so a 15-share sell can appear in `position_fills` as a 10-share CLOSE
    while five shares of it are a live position.

    Summed across every fill row for the execution, which is what makes the
    flip case come out right in both states. While the short is open the sell
    is recorded once, as a 10-share CLOSE, and five shares remain. Once the
    short is covered the sell is recorded twice -- CLOSE 10 and OPEN 5 -- and
    the total reaches 15, so nothing is left open. Testing membership cannot
    tell those two apart, and reported the second for both.

    One grouped query for the whole ledger. It replaces a query that returned
    every fill row, so it moves less data, not more.
    """
    stmt = select(
        PositionFill.trade_id, func.sum(PositionFill.quantity)
    ).group_by(PositionFill.trade_id)
    if trade_ids is not None:
        stmt = stmt.where(PositionFill.trade_id.in_(trade_ids))
    return {
        trade_id: quantity or Decimal("0")
        for trade_id, quantity in (await session.execute(stmt)).all()
    }


def _acquisition_premium(trade: Trade, quantity: Decimal) -> Decimal:
    """Cost above the fill price for `quantity` shares of this fill.

    Capitalised into an open position's basis, because that is what the broker
    does. Preferring the broker's own `cost` over our commission column is not
    pedantry: IBKR Singapore charges 9% GST on commission, folds it into the
    basis, and reports it only as a separate "Sales Tax" line. Using commission
    alone left NFLX reading 94.5299 against a statement figure of 94.540340167.

    Taken from `broker_cost_basis` when the fill bought shares, since on a BUY
    IBKR's `cost` is the all-in acquisition cost -- notional plus commission
    plus tax -- and dividing it out needs no tax rate anywhere in this code. A
    hardcoded 9% would be wrong the next time Singapore moves it, and silently.

    Everything else falls back to raw commission: on a SELL, IBKR reports `cost`
    as the basis RELIEVED rather than proceeds, so it says nothing about what
    opening a short cost. Approximate by the tax on that path, and honest about
    which path it is.

    Scaled to `quantity` so a fill half-consumed by a closed round trip
    contributes half its cost here and half to that round trip's P&L.
    """
    size = trade.quantity or Decimal("0")
    if size == 0:
        return Decimal("0")

    share = quantity / size
    basis = trade.broker_cost_basis
    if (trade.direction or "").upper() == "BUY" and basis is not None and basis > 0:
        premium = Decimal(str(basis)) - size * (trade.actual_entry or Decimal("0"))
        return premium * share

    return (trade.commission or Decimal("0")) * share


def _unmatched_quantity(trade: Trade, consumed: dict[uuid.UUID, Decimal]) -> Decimal:
    """Shares of this execution that no closed round trip accounts for."""
    remaining = (trade.quantity or Decimal("0")) - consumed.get(
        trade.id, Decimal("0")
    )
    return remaining if remaining > 0 else Decimal("0")


def _is_fully_matched(trade: Trade, consumed: dict[uuid.UUID, Decimal]) -> bool:
    """Whether the whole execution has been folded into closed round trips.

    False while any part of it is still open, which is the honest answer for
    the sell that flipped a long into a short: it closed one trade and started
    another, and calling it "matched" hid the one that is still running.
    """
    return _unmatched_quantity(trade, consumed) == 0


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

    consumed = await _consumed_quantity_by_trade(session)

    return [
        TradeOut(
            **{c.name: getattr(trade, c.name) for c in Trade.__table__.columns
               if c.name in TradeOut.model_fields},
            is_matched=_is_fully_matched(trade, consumed),
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

    consumed = await _consumed_quantity_by_trade(session, [trade_id])
    return TradeOut(
        **{c.name: getattr(trade, c.name) for c in Trade.__table__.columns
           if c.name in TradeOut.model_fields},
        is_matched=_is_fully_matched(trade, consumed),
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
        lock_ticker,
        run_matching_for_ticker,
    )

    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    changes = params.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update.")

    ticker = trade.ticker

    # Before the position delete below, not just inside the rebuild. Holding
    # position rows while waiting for the ticker lock, against a sync holding
    # the ticker lock while waiting for those rows, is a lock-order inversion
    # Postgres breaks by killing one transaction outright. See lock_ticker.
    await lock_ticker(session, ticker)

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
        if when is None:
            # `trades.entry_date` is NOT NULL, and an explicit null here is
            # reachable: the field is Optional so it can be OMITTED, and
            # ExecutionUpdatePayload types it `string | null`. Assigning it
            # through reached the driver as a not-null violation and surfaced
            # as an opaque 500 -- after the round trips built on this fill had
            # already been deleted, since that happens further down in the same
            # transaction. Refused here, before anything is touched.
            raise HTTPException(
                status_code=422,
                detail=(
                    "execution_time cannot be cleared. Omit the field to leave "
                    "the fill's time unchanged, or send a new timestamp."
                ),
            )
        if when.tzinfo is None:
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
        reviews_discarded = sum(1 for p in affected if _position_has_review(p))
        await session.execute(delete(Position).where(Position.id.in_(affected_ids)))

    # No commit here. The edit above, the delete just issued, and the rebuild
    # below all land in run_matching_for_ticker's own commit -- one
    # transaction, so a crash before that commit leaves the fill and its
    # positions exactly as they were, instead of the fill edited and its
    # positions gone with nothing rebuilt to replace them.
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

    model_config = ConfigDict(from_attributes=True)


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
        lock_ticker,
        run_matching_for_ticker,
    )

    trade = await session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")

    ticker = trade.ticker

    # First lock this transaction takes, ahead of the position delete below.
    # See lock_ticker: acquiring it only inside the rebuild would invert the
    # lock order against a concurrent sync and deadlock.
    await lock_ticker(session, ticker)

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
        reviews_discarded = sum(1 for p in affected if _position_has_review(p))
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

    # No commit here. The delete above (trade, its positions, the suppression
    # tombstone) and the rebuild below share run_matching_for_ticker's commit,
    # so a crash before that commit leaves the trade in place with its
    # positions untouched, instead of gone with nothing rebuilt in their
    # place. Idempotent besides: untouched round trips on this ticker are not
    # duplicated.
    result = await run_matching_for_ticker(session, ticker, persist=True)

    return TradeDeleteResult(
        deleted_trade_id=trade_id,
        ticker=ticker,
        positions_removed=len(affected_ids),
        positions_rebuilt=len(result.positions),
        reviews_discarded=reviews_discarded,
        suppressed_from_future_syncs=suppressed,
    )


def _position_has_review(position: Position) -> bool:
    """Whether losing this round trip would lose work the user cannot redo.

    Re-matching rebuilds quantity, prices and P&L from the fills. It cannot
    rebuild a grade, a note or a post-mortem, so those are what "discarded"
    counts -- and every surface that reports a number counts it this way, so
    one figure means one thing across the app.
    """
    return position.review_status == ReviewStatus.reviewed.value or any(
        (
            position.review_went_well,
            position.review_went_wrong,
            position.review_lessons,
            position.notes,
        )
    )


async def _executions_behind(
    session: AsyncSession, position_id: uuid.UUID
) -> list[uuid.UUID]:
    """Every execution this round trip was built from."""
    return list(set((
        await session.execute(
            select(PositionFill.trade_id).where(PositionFill.position_id == position_id)
        )
    ).scalars().all()))


async def _round_trips_sharing_executions(
    session: AsyncSession, position_id: uuid.UUID, trade_ids: list[uuid.UUID]
) -> list[Position]:
    """Other round trips built on any of the same executions.

    One fill can belong to two positions. An oversell that flips long to short
    closes the long and opens the short with the SAME execution -- BUY 10,
    SELL 15 leaves that sell 5 shares open -- so it appears in position_fills
    twice, CLOSE of one round trip and OPEN of the next.

    That makes deleting a position by way of its executions reach further than
    the position. `position_fills.trade_id` is ON DELETE CASCADE, so the
    neighbour loses its fills, and `positions.open_trade_id` is only SET NULL,
    so the neighbour's row survives asserting a P&L with nothing underneath it.
    Callers need to know before they act, not after.
    """
    if not trade_ids:
        return []

    ids = (
        await session.execute(
            select(PositionFill.position_id)
            .where(
                PositionFill.trade_id.in_(trade_ids),
                PositionFill.position_id != position_id,
            )
            .distinct()
        )
    ).scalars().all()
    if not ids:
        return []

    return list((
        await session.execute(
            select(Position).where(Position.id.in_(ids)).order_by(Position.entry_time)
        )
    ).scalars().all())


class SharedRoundTrip(BaseModel):
    """A neighbouring round trip that would be destroyed as collateral."""

    position_id: uuid.UUID
    symbol: str
    quantity: Decimal
    realized_pnl: Decimal
    entry_time: datetime
    exit_time: datetime
    # Whether it carries a grade, notes or a post-mortem. Re-matching can
    # rebuild everything else about it; not this.
    has_review: bool


class PositionDeleteImpact(BaseModel):
    """What DELETE /api/positions/{id} would remove, without removing it."""

    position_id: uuid.UUID
    ticker: str
    executions_deleted: int
    shared_round_trips: list[SharedRoundTrip]
    reviews_at_risk: int


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
    # Round trips OTHER than the one asked for that no longer exist afterwards:
    # neighbours sharing an execution, plus anything the rebuild found stale.
    # Zero on the ordinary delete; non-zero is the case worth reading about.
    positions_removed: int = 0
    reviews_discarded: int = 0


@app.get(
    "/api/positions/{position_id}/delete-impact",
    response_model=PositionDeleteImpact,
    dependencies=[Depends(verify_clerk_token)],
)
async def position_delete_impact(
    position_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """What deleting this round trip would take with it.

    Read-only, and the whole point is that it runs BEFORE the confirmation
    prompt. A delete here removes executions, and an execution can be shared
    with the round trip on either side of it, so the honest version of "are you
    sure?" has to name what else goes. Asking afterwards is not asking.
    """
    position = await session.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")

    trade_ids = await _executions_behind(session, position_id)
    shared = await _round_trips_sharing_executions(session, position_id, trade_ids)

    return PositionDeleteImpact(
        position_id=position_id,
        ticker=position.symbol,
        executions_deleted=len(trade_ids),
        shared_round_trips=[
            SharedRoundTrip(
                position_id=p.id,
                symbol=p.symbol,
                quantity=p.quantity,
                realized_pnl=p.realized_pnl,
                entry_time=p.entry_time,
                exit_time=p.exit_time,
                has_review=_position_has_review(p),
            )
            for p in shared
        ],
        reviews_at_risk=sum(1 for p in shared if _position_has_review(p)),
    )


@app.delete(
    "/api/positions/{position_id}",
    response_model=PositionDeleteResult,
    dependencies=[Depends(verify_clerk_token)],
)
async def delete_position(
    position_id: uuid.UUID,
    reason: Optional[str] = None,
    include_shared: bool = False,
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

    One execution can belong to two round trips, so this can reach beyond the
    position asked for. It refuses to by default: `include_shared=true` is
    required, and the client is expected to have called
    GET /api/positions/{id}/delete-impact and shown the user what else goes.
    """
    from services.matching_engine import (  # noqa: PLC0415 - avoids import cycle
        lock_ticker,
        run_matching_for_ticker,
    )

    position = await session.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")

    ticker = position.symbol

    # Ahead of the position and trade deletes below, for the lock-ordering
    # reason in lock_ticker. This handler commits before it re-matches, so it
    # takes the lock twice; advisory locks are re-entrant and every level is
    # released at COMMIT, so that costs nothing.
    await lock_ticker(session, ticker)

    trade_ids = await _executions_behind(session, position_id)

    # An oversell that flips long to short closes one round trip and opens the
    # next with the same fill. Deleting that fill silently destroys the
    # neighbour -- and the neighbour's review, which nothing can rebuild -- so
    # it takes a deliberate second answer rather than happening by surprise.
    shared = await _round_trips_sharing_executions(session, position_id, trade_ids)
    if shared and not include_shared:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(shared)} other round trip"
                f"{'' if len(shared) == 1 else 's'} on {ticker} "
                f"{'shares' if len(shared) == 1 else 'share'} an execution with "
                "this one and would be deleted too. Call "
                f"/api/positions/{position_id}/delete-impact to see which, then "
                "retry with include_shared=true — or delete the individual "
                "fills from the journal instead."
            ),
        )

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

    # Counted before anything is deleted, while the rows are still readable.
    reviews_discarded = sum(1 for p in shared if _position_has_review(p))

    # Positions go first, this one and every neighbour that shares a fill with
    # it. `position_fills` cascades from both sides, so deleting the executions
    # while a position still referenced them would leave that position
    # asserting a size its fills no longer support -- and migration 019 now
    # refuses the SET NULL that would otherwise make it dangle quietly.
    await session.delete(position)
    if shared:
        await session.execute(
            delete(Position).where(Position.id.in_([p.id for p in shared]))
        )
        logger.warning(
            "Deleting round trip %s on %s also removed %d neighbouring round "
            "trip(s) and %d review(s): they were built on the same execution.",
            position_id,
            ticker,
            len(shared),
            reviews_discarded,
        )
    if trade_ids:
        await session.execute(delete(Trade).where(Trade.id.in_(trade_ids)))
    await session.commit()

    # Authoritative: removing executions re-partitions the ticker's FIFO queue,
    # so round trips built from what is left can differ from what was stored.
    result = await run_matching_for_ticker(session, ticker, persist=True)

    return PositionDeleteResult(
        position_id=position_id,
        ticker=ticker,
        executions_deleted=len(trade_ids),
        positions_rebuilt=len(result.positions),
        suppressed_from_future_syncs=suppressed,
        # The neighbours removed here, plus anything the rebuild found stale.
        # Both are round trips the user did not ask to delete.
        positions_removed=len(shared) + result.positions_removed,
        reviews_discarded=reviews_discarded + result.reviews_discarded,
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
    # NET of commission (migration 020), with the gross figure and the cost
    # beside it. A journal row that says "P&L" and means something different
    # from the broker statement is the reason the two never reconciled.
    realized_pnl: Optional[float] = None
    gross_pnl: Optional[float] = None
    commission: Optional[float] = None
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
    # Whether that plan has a chart screenshot, so the ledger renders the
    # image slot only where there is one to put in it.
    plan_has_chart: bool = False
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
    # Which of them carry a chart. Sent so the ledger can decide whether to
    # render the image slot at all -- without it every plan without a
    # screenshot would still fire a request for one and take a 404 to find out.
    plan_has_chart: dict[uuid.UUID, bool] = {}
    plan_ids = {t.plan_id for t in trades if t.plan_id is not None}
    if plan_ids:
        plan_rows = (
            await session.execute(
                select(
                    PlannedTrade.id,
                    PlannedTrade.created_at,
                    PlannedTrade.chart_path,
                ).where(PlannedTrade.id.in_(plan_ids))
            )
        ).all()
        plan_has_chart = {row[0]: row[2] is not None for row in plan_rows}
        plan_created_at = {row[0]: row[1] for row in plan_rows}
    disciplines_by_position = await _disciplines_by_position(
        session, [p.id for p in positions]
    )
    fills_by_position: dict[uuid.UUID, list[PositionFill]] = {}
    for fill in fills:
        fills_by_position.setdefault(fill.position_id, []).append(fill)

    # How much of each execution the closed round trips account for. Summed
    # from the fills already loaded above rather than re-queried -- an
    # execution can be partly consumed, and the remainder is live exposure.
    consumed_by_trade: dict[uuid.UUID, Decimal] = {}
    for fill in fills:
        consumed_by_trade[fill.trade_id] = consumed_by_trade.get(
            fill.trade_id, Decimal("0")
        ) + (fill.quantity or Decimal("0"))

    rows: list[RoundTripOut] = []

    # --- closed round trips ------------------------------------------------
    for position in positions:
        if ticker and position.symbol != ticker.strip().upper():
            continue
        opening = trade_by_id.get(position.open_trade_id)
        # Read, not reconstructed. `opening` is still needed below for the plan
        # it carries, but the direction no longer depends on finding it.
        direction = _position_side(position)
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
                plan_has_chart=plan_has_chart.get(plan.get("plan_id"), False),
                entry_slippage=slippage,
                has_hand_added_fills=hand_added,
                exit_price=float(position.exit_price) if position.exit_price is not None else None,
                entry_time=position.entry_time,
                exit_time=position.exit_time,
                realized_pnl=float(position.realized_pnl) if position.realized_pnl is not None else None,
                gross_pnl=float(position.gross_pnl) if position.gross_pnl is not None else None,
                commission=float(position.commission) if position.commission is not None else None,
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
    # Executions FIFO never paired off, in whole or in PART. Grouped per
    # ticker, because that is the unit of exposure: two unsold AAPL buys are
    # one open position, not two.
    #
    # Partly matters. This used to skip any execution present in
    # `position_fills` at all, on the reading that appearing there means
    # consumed. An oversell breaks that reading: the sell closes the long it
    # was aimed at AND opens a short with the remainder, so it is recorded as a
    # 10-share CLOSE while five shares of it are a live position. Being in the
    # table excluded it, and the short existed nowhere in the app -- not as a
    # row, not in exposure, not in the fill list. The matcher had it the whole
    # time in `MatchingResult.open_lots`; this endpoint just never asked.
    open_by_ticker: dict[str, list[tuple[Trade, Decimal]]] = {}
    for trade in trades:
        if ticker and trade.ticker != ticker.strip().upper():
            continue
        remaining = _unmatched_quantity(trade, consumed_by_trade)
        if remaining <= 0:
            continue
        open_by_ticker.setdefault(trade.ticker, []).append((trade, remaining))

    for symbol, group in open_by_ticker.items():
        group.sort(key=lambda pair: pair[0].entry_date)
        signed = sum(
            remaining
            * (Decimal("1") if (t.direction or "BUY").upper() == "BUY" else Decimal("-1"))
            for t, remaining in group
        )
        if signed == 0:
            # Nets flat without ever being matched -- a data oddity rather than
            # live exposure. Skipped rather than rendered as a zero-size row.
            continue

        net_direction = "BUY" if signed > 0 else "SELL"
        # Replayed on a FIFO basis, INCLUSIVE of the commission paid to acquire
        # the shares -- both because that is what the IBKR statement reports,
        # and so this row reconciles against it. Average-cost was measurably
        # wrong here: MSFT read 409.40 against a statement figure of 407.298903.
        # Its predecessor was worse, averaging all 17 shares ever bought for
        # 415.45 while only 2 remained.
        #
        # Replayed over the UNMATCHED remainder of each execution, not its full
        # size, or the flipping sell would contribute all 15 of its shares to a
        # position that is only 5. Commission is scaled to that same remainder,
        # so a fill half-consumed by a closed round trip contributes half its
        # cost here and half to that round trip's P&L.
        exposure = replay_open_exposure(
            (
                t.direction,
                remaining,
                t.actual_entry or Decimal("0"),
                _acquisition_premium(t, remaining),
            )
            for t, remaining in group
        )
        avg_entry = exposure.average_cost

        opening = group[0][0]
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
                plan_has_chart=plan_has_chart.get(plan.get("plan_id"), False),
                # Against the replayed cost basis, for the same reason the
                # closed branch uses the position's entry: what you are still
                # holding is what the plan should be judged against.
                entry_slippage=_entry_slippage(
                    net_direction, plan.get("planned_entry"), avg_entry
                ),
                has_hand_added_fills=any(
                    (t.ibkr_exec_id or "").startswith(EXEC_PREFIX_REPAIR)
                    for t, _ in group
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
                        # The part of the fill that is still open, not the size
                        # the broker filled. On the sell that flipped a long
                        # into a short, ten of those fifteen shares belong to
                        # the closed round trip listed separately -- showing 15
                        # here would count them in both places.
                        quantity=float(remaining),
                        price=float(t.actual_entry or 0),
                        executed_at=t.entry_date,
                    )
                    for t, remaining in group
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
    """Record the review checklist, and mark the position reviewed unless the
    caller opts out with `mark_reviewed=False`."""
    position = await session.get(Position, position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")

    updates = params.model_dump(exclude_unset=True)

    # Not a column -- it only decides whether the setattr loop's assignment
    # of review_status happens at all, applied further down.
    updates.pop("mark_reviewed", None)

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

    if params.mark_reviewed:
        position.review_status = ReviewStatus.reviewed.value

    await session.commit()
    await session.refresh(position)
    return await _position_out(session, position)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@app.get("/api/analytics/dashboard", dependencies=[Depends(verify_clerk_token)])
async def analytics_dashboard(
    preset: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    session: AsyncSession = Depends(get_session),
):
    """Core stats, the day/session heatmap and the equity curve, for one window.

    Three ways to ask, in descending precedence:

      * `start_date` / `end_date` -- an explicit range, inclusive, in market
        time. What a saved custom preset sends. Either bound may be omitted to
        leave that side open.
      * `preset` -- one of YTD, 1Y, ALL. Expanded server-side so the browser
        never holds a second definition of what "YTD" means.
      * neither -- defaults to 1Y, so a bare request is bounded rather than
        plotting the entire ledger.

    The window governs the WHOLE payload, not just the curve: a 1Y chart beside
    an all-time win rate on one screen, with nothing saying they cover
    different spans, is worse than either figure alone.

    Round trips are selected by when they CLOSED. `start_date` after
    `end_date`, or an unknown preset, is a 422 naming the problem rather than
    an empty dashboard the user has to diagnose.
    """
    from services.analytics import build_dashboard, resolve_window

    try:
        window = resolve_window(preset, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return await build_dashboard(session, window)


@app.get("/api/analytics/advanced", dependencies=[Depends(verify_clerk_token)])
async def analytics_advanced(session: AsyncSession = Depends(get_session)):
    """R-multiples, slippage, expectancy, and the per-mistake breakdown.

    Sourced from `trades` rather than `positions`: R-multiple and slippage
    need the plan (stop_loss, planned_entry), which only the ledger carries.
    """
    from services.analytics import build_advanced_analytics

    return await build_advanced_analytics(session)


# ===========================================================================
# THE INVESTMENT BOOK
# ===========================================================================
#
# Everything below this line concerns the long-term portfolio and NOTHING
# above it. That is deliberate and structural, not stylistic:
#
#   * No model, endpoint or helper here is referenced by anything earlier in
#     this file. The trading journal cannot call into this section, so a
#     failure here cannot reach it.
#   * The tables are the three created by migration 026, none of which has a
#     foreign key into `trades`, `positions`, `position_fills`,
#     `realized_legs` or `planned_trades`.
#   * The IBKR ingest is untouched. When a broker sync for this book arrives
#     it gets its own endpoint rather than a branch inside `ingest_ibkr`,
#     which is heavily tested and has been repaired more than once.
#
# WHAT IS DERIVED RATHER THAN STORED. Quantity, average cost and every figure
# built on them come from the transaction ledger on read. The same choice the
# trading side makes with fills, for the same reason: a stored aggregate is
# free to drift from the rows beneath it, and this codebase has already paid
# for that once. Intrinsic values are likewise computed on read -- the monthly
# cadence governs FETCHING inputs, not doing the arithmetic, which costs
# microseconds.

from services import valuation as valuation_engine  # noqa: E402

# `market_data` and `growth` reach the network, so they are imported inside
# the two refresh endpoints rather than here -- nothing else in this file
# should be able to fail at import time because a provider changed.


class InvestmentTransaction(Base):
    """One thing that happened: a purchase, a sale, a dividend, a transfer.

    The immutable ledger the whole book is derived from. See migration 026.
    """

    __tablename__ = "investment_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticker = Column(String(20), nullable=False)
    transaction_type = Column(String(12), nullable=False)
    quantity = Column(Numeric(18, 8), nullable=True)
    price = Column(Numeric(18, 4), nullable=True)
    # Signed from the account's point of view: negative when money left to buy
    # something, positive when it arrived. Fees are INCLUDED here -- this is
    # the cash movement, and `fees` below is the same money broken out for
    # cost analysis, not a second charge.
    total_amount = Column(Numeric(18, 4), nullable=False)
    fees = Column(Numeric(18, 4), nullable=False, default=0)
    transaction_date = Column(DateTime(timezone=True), nullable=False)
    listed_currency = Column(String(3), nullable=False, default="USD")
    exchange_rate = Column(Numeric(18, 8), nullable=False, default=1)
    source = Column(String(12), nullable=False, default="MANUAL")
    external_id = Column(Text, nullable=True, unique=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class InvestmentHolding(Base):
    """A ticker in the book: how it is classified, and its last quote."""

    __tablename__ = "investment_holdings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticker = Column(String(20), nullable=False, unique=True)
    name = Column(Text, nullable=True)
    sector = Column(Text, nullable=True)
    category = Column(String(20), nullable=True)
    holding_type = Column(String(24), nullable=True)
    country = Column(String(32), nullable=True)
    listed_currency = Column(String(3), nullable=False, default="USD")
    exchange_rate = Column(Numeric(18, 8), nullable=False, default=1)
    planned_allocation = Column(Numeric(18, 4), nullable=True)
    # Whether a discounted cash flow means anything here. False for an ETF,
    # which has no cash flows of its own.
    is_valuable = Column(Boolean, nullable=False, default=True)
    current_price = Column(Numeric(18, 4), nullable=True)
    price_updated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class InvestmentValuationInput(Base):
    """What the DCF was fed, in two variants that never merge in storage.

    'auto' is replaced wholesale by each refresh; 'override' is whatever the
    user typed and is never touched by one. They are combined only on read.
    """

    __tablename__ = "investment_valuation_inputs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticker = Column(String(20), nullable=False)
    variant = Column(String(10), nullable=False)
    base_flow = Column(Numeric(20, 4), nullable=True)
    metric = Column(String(24), nullable=True)
    shares_outstanding = Column(Numeric(20, 4), nullable=True)
    total_debt = Column(Numeric(20, 4), nullable=True)
    cash_and_st = Column(Numeric(20, 4), nullable=True)
    beta = Column(Numeric(10, 4), nullable=True)
    growth_1_5 = Column(Numeric(10, 6), nullable=True)
    discount_rate = Column(Numeric(10, 6), nullable=True)
    region = Column(String(4), nullable=False, default="US")
    source = Column(String(24), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class InvestmentSectorColor(Base):
    """A manual color pick for one sector's treemap tile (migration 027).

    Sparse by design -- a row exists only for a sector the trader has
    deliberately recolored. Anything absent falls back to the frontend's
    built-in palette, so this table never needs seeding or backfilling.
    """

    __tablename__ = "investment_sector_colors"

    sector = Column(Text, primary_key=True)
    color = Column(Text, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


VARIANT_AUTO = "auto"
VARIANT_OVERRIDE = "override"

TX_BUY = "BUY"
TX_SELL = "SELL"
TX_DIVIDEND = "DIVIDEND"
TX_TRANSFER = "TRANSFER"
TX_TYPES = (TX_BUY, TX_SELL, TX_DIVIDEND, TX_TRANSFER)

# Transactions that add shares at a cost. TRANSFER is here because a holding
# arriving from another broker keeps its basis -- it is a purchase whose cash
# left the account somewhere this book cannot see.
TX_ADDS_SHARES = (TX_BUY, TX_TRANSFER)

# The columns a valuation input row actually carries, in one place so the
# merge, the upsert and the override endpoint cannot drift apart.
VALUATION_FIELDS = (
    "base_flow", "metric", "shares_outstanding", "total_debt",
    "cash_and_st", "beta", "growth_1_5", "discount_rate", "region",
)

# How stale an 'auto' row may be before a refresh will replace it. Twenty-five
# rather than thirty so a monthly job cannot drift into skipping a month: run
# on the 1st, a 30-day guard makes the next run on the 1st a no-op.
REFRESH_MAX_AGE = timedelta(days=25)


def _f(value) -> Optional[float]:
    """Decimal -> float at the boundary. NULL stays None, never 0.0."""
    return float(value) if value is not None else None


# ---------------------------------------------------------------------------
# Deriving a position from the ledger
# ---------------------------------------------------------------------------


class DerivedPosition(BaseModel):
    """Quantity and cost, reconstructed from transactions.

    AVERAGE COST, not FIFO. The trading side is FIFO because a round trip is
    the unit there and the broker reports it that way. Here the unit is a
    holding accumulated over years, often with a monthly purchase; average
    cost is what the portfolio sheet this replaces uses, and it is the figure
    that answers "what did my position cost me" without inventing lots.
    """

    ticker: str
    quantity: float = 0.0
    average_cost: Optional[float] = None
    cost_basis: float = 0.0
    realized_pnl: float = 0.0
    dividends: float = 0.0
    transaction_count: int = 0
    first_acquired: Optional[datetime] = None
    last_activity: Optional[datetime] = None


def _derive_position(ticker: str, rows: Sequence[InvestmentTransaction]) -> DerivedPosition:
    """Walk one ticker's transactions in time order into a position.

    A SELL relieves cost at the CURRENT average, which is what makes this
    average-cost rather than FIFO: the remaining basis per share is unchanged
    by a partial sale, so selling half a position does not silently reprice
    the half still held.
    """
    quantity = 0.0
    cost = 0.0
    realized = 0.0
    dividends = 0.0
    first_acquired: Optional[datetime] = None
    last_activity: Optional[datetime] = None

    for tx in sorted(rows, key=lambda r: (r.transaction_date, r.created_at or r.transaction_date)):
        kind = tx.transaction_type
        amount = _f(tx.total_amount) or 0.0
        qty = _f(tx.quantity) or 0.0
        last_activity = tx.transaction_date

        if kind in TX_ADDS_SHARES:
            quantity += qty
            # abs(): total_amount is negative for money leaving, and cost is
            # a magnitude. Taking it as-signed would make every purchase
            # reduce the basis.
            cost += abs(amount)
            if first_acquired is None:
                first_acquired = tx.transaction_date
        elif kind == TX_SELL:
            average = cost / quantity if quantity > 0 else 0.0
            relieved = average * qty
            realized += amount - relieved
            cost = max(0.0, cost - relieved)
            quantity -= qty
            # A rounding residue on a full exit would otherwise leave a
            # basis attached to nothing.
            if quantity <= 1e-9:
                quantity = 0.0
                cost = 0.0
        elif kind == TX_DIVIDEND:
            dividends += amount

    return DerivedPosition(
        ticker=ticker,
        quantity=quantity,
        average_cost=(cost / quantity) if quantity > 0 else None,
        cost_basis=cost,
        realized_pnl=realized,
        dividends=dividends,
        transaction_count=len(rows),
        first_acquired=first_acquired,
        last_activity=last_activity,
    )


async def _derive_all_positions(
    session: AsyncSession, tickers: Optional[Sequence[str]] = None
) -> dict[str, DerivedPosition]:
    """Every ticker's position in one query rather than one query per ticker.

    This API container sits far from its database, so cost here is paid per
    ROUND TRIP; fourteen queries would cost fourteen times as much as one,
    entirely in waiting.
    """
    stmt = select(InvestmentTransaction)
    if tickers is not None:
        if not tickers:
            return {}
        stmt = stmt.where(InvestmentTransaction.ticker.in_(list(tickers)))

    rows = (await session.execute(stmt)).scalars().all()

    grouped: dict[str, list[InvestmentTransaction]] = {}
    for row in rows:
        grouped.setdefault(row.ticker, []).append(row)

    return {t: _derive_position(t, rs) for t, rs in grouped.items()}


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------


class HoldingCreate(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20)
    name: Optional[str] = None
    sector: Optional[str] = None
    category: Optional[str] = Field(None, description="Growth, Predictable or ETF")
    holding_type: Optional[str] = None
    country: Optional[str] = None
    listed_currency: str = Field("USD", min_length=3, max_length=3)
    exchange_rate: float = Field(1.0, gt=0)
    planned_allocation: Optional[float] = Field(None, ge=0)
    # Defaults to True, and is set False for a fund. Explicit rather than
    # inferred from `category` so a miscategorised ETF does not quietly
    # acquire an intrinsic value.
    is_valuable: bool = True

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("listed_currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("category")
    @classmethod
    def _valid_category(cls, value: Optional[str]) -> Optional[str]:
        """Mirrors the CHECK in migration 026 so a typo is a 422 naming the
        allowed values rather than a 500 from the driver."""
        if value is None or not value.strip():
            return None
        allowed = {"Growth", "Predictable", "ETF"}
        if value.strip() not in allowed:
            raise ValueError(f"category must be one of {', '.join(sorted(allowed))}")
        return value.strip()


class HoldingUpdate(BaseModel):
    """Partial edit. Only keys present in the body are applied."""

    name: Optional[str] = None
    sector: Optional[str] = None
    category: Optional[str] = None
    holding_type: Optional[str] = None
    country: Optional[str] = None
    listed_currency: Optional[str] = Field(None, min_length=3, max_length=3)
    exchange_rate: Optional[float] = Field(None, gt=0)
    planned_allocation: Optional[float] = Field(None, ge=0)
    is_valuable: Optional[bool] = None

    _valid_category = field_validator("category")(HoldingCreate._valid_category.__func__)

    @field_validator("listed_currency")
    @classmethod
    def _upper_currency(cls, value: Optional[str]) -> Optional[str]:
        return value.strip().upper() if value else value


async def _get_holding(session: AsyncSession, ticker: str) -> InvestmentHolding:
    holding = (
        await session.execute(
            select(InvestmentHolding).where(InvestmentHolding.ticker == ticker)
        )
    ).scalar_one_or_none()
    if holding is None:
        raise HTTPException(status_code=404, detail=f"No holding for {ticker}.")
    return holding


@app.post(
    "/api/investments/holdings",
    status_code=201,
    dependencies=[Depends(verify_clerk_token)],
)
async def create_holding(
    params: HoldingCreate, session: AsyncSession = Depends(get_session)
):
    holding = InvestmentHolding(**params.model_dump())
    session.add(holding)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"{params.ticker} is already in the book."
        )
    await session.refresh(holding)
    return _holding_row(holding)


@app.patch(
    "/api/investments/holdings/{ticker}",
    dependencies=[Depends(verify_clerk_token)],
)
async def update_holding(
    ticker: str, params: HoldingUpdate, session: AsyncSession = Depends(get_session)
):
    holding = await _get_holding(session, ticker.strip().upper())
    for field, value in params.model_dump(exclude_unset=True).items():
        setattr(holding, field, value)
    holding.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(holding)
    return _holding_row(holding)


@app.delete(
    "/api/investments/holdings/{ticker}",
    dependencies=[Depends(verify_clerk_token)],
)
async def delete_holding(ticker: str, session: AsyncSession = Depends(get_session)):
    """Remove a ticker from the book.

    Refused while transactions remain. The ledger is history, and deleting
    the holding would leave rows describing purchases of something the book
    no longer admits owning -- recoverable, but only by reading raw tables.
    Delete the transactions first if that is genuinely the intent.
    """
    ticker = ticker.strip().upper()
    holding = await _get_holding(session, ticker)

    count = (
        await session.execute(
            select(func.count())
            .select_from(InvestmentTransaction)
            .where(InvestmentTransaction.ticker == ticker)
        )
    ).scalar() or 0
    if count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{ticker} still has {count} transaction(s). Delete those first "
                "if you really mean to remove it."
            ),
        )

    await session.execute(
        delete(InvestmentValuationInput).where(InvestmentValuationInput.ticker == ticker)
    )
    await session.delete(holding)
    await session.commit()
    return {"ticker": ticker, "deleted": True}


# ---------------------------------------------------------------------------
# Sector colors
# ---------------------------------------------------------------------------


class SectorColorIn(BaseModel):
    color: str

    @field_validator("color")
    @classmethod
    def _valid_hex(cls, value: str) -> str:
        """Mirrors investment_sector_colors_hex so a bad value is a 422 naming
        the expected format rather than a 500 from the CHECK constraint."""
        value = value.strip()
        if len(value) != 7 or value[0] != "#":
            raise ValueError("color must be a hex value like #3B5978")
        try:
            int(value[1:], 16)
        except ValueError:
            raise ValueError("color must be a hex value like #3B5978")
        return value.upper()


class SectorColorOut(BaseModel):
    sector: str
    color: str
    updated_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


@app.get(
    "/api/investments/sector-colors",
    response_model=list[SectorColorOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_sector_colors(session: AsyncSession = Depends(get_session)):
    """Every sector the trader has manually recolored. Anything absent from
    this list uses the frontend's built-in palette."""
    rows = (
        (await session.execute(select(InvestmentSectorColor).order_by(InvestmentSectorColor.sector)))
        .scalars()
        .all()
    )
    return [SectorColorOut.model_validate(row) for row in rows]


@app.put(
    "/api/investments/sector-colors/{sector}",
    response_model=SectorColorOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def set_sector_color(
    sector: str, params: SectorColorIn, session: AsyncSession = Depends(get_session)
):
    """Create or replace the color for one sector name.

    Upsert on the name itself -- there is no id to look up first, and a
    second pick for the same sector is a correction, not a new row.
    """
    sector = sector.strip()
    if not sector:
        raise HTTPException(status_code=422, detail="Sector name cannot be blank.")

    await session.execute(
        pg_insert(InvestmentSectorColor)
        .values(sector=sector, color=params.color, updated_at=datetime.now(timezone.utc))
        .on_conflict_do_update(
            index_elements=["sector"],
            set_={"color": params.color, "updated_at": datetime.now(timezone.utc)},
        )
    )
    await session.commit()

    row = await session.get(InvestmentSectorColor, sector)
    return SectorColorOut.model_validate(row)


@app.delete(
    "/api/investments/sector-colors/{sector}",
    dependencies=[Depends(verify_clerk_token)],
)
async def delete_sector_color(sector: str, session: AsyncSession = Depends(get_session)):
    """Drop the override, returning this sector to the built-in palette."""
    result = await session.execute(
        delete(InvestmentSectorColor).where(InvestmentSectorColor.sector == sector.strip())
    )
    await session.commit()
    return {"sector": sector, "deleted": bool(result.rowcount)}


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TransactionCreate(BaseModel):
    """One entry in the ledger.

    `total_amount` may be omitted and is then derived from quantity, price and
    fees -- which is the common case for hand entry, and removes the question
    of whether fees belong inside it. Supply it explicitly when importing from
    a broker, where the cash movement is authoritative and the arithmetic may
    not reproduce it to the cent.
    """

    ticker: str = Field(..., min_length=1, max_length=20)
    transaction_type: str
    quantity: Optional[float] = Field(None, gt=0)
    price: Optional[float] = Field(None, ge=0)
    total_amount: Optional[float] = None
    fees: float = Field(0.0, ge=0)
    transaction_date: datetime
    listed_currency: str = Field("USD", min_length=3, max_length=3)
    exchange_rate: float = Field(1.0, gt=0)
    note: Optional[str] = None

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("transaction_type")
    @classmethod
    def _valid_type(cls, value: str) -> str:
        kind = value.strip().upper()
        if kind not in TX_TYPES:
            raise ValueError(f"transaction_type must be one of {', '.join(TX_TYPES)}")
        return kind

    @model_validator(mode="after")
    def _shape_matches_type(self):
        """Mirrors migration 026's CHECK, so a bad shape is a 422 that names
        the problem rather than an IntegrityError the client cannot read."""
        if self.transaction_type == TX_DIVIDEND:
            if self.quantity is not None:
                raise ValueError(
                    "a DIVIDEND carries no quantity (a share dividend is not "
                    "modelled yet, and counting it as a purchase would be wrong)"
                )
            if self.total_amount is None:
                raise ValueError("a DIVIDEND needs total_amount -- what was received")
        else:
            if self.quantity is None:
                raise ValueError(f"a {self.transaction_type} needs a quantity")
            if self.price is None and self.total_amount is None:
                raise ValueError(
                    f"a {self.transaction_type} needs a price, or a total_amount"
                )
        return self

    def resolved_total(self) -> float:
        """The signed cash movement, derived when the client did not send one.

        Negative when money left the account. Fees increase what a purchase
        cost and decrease what a sale returned, which is the same sign
        convention on both sides once the direction is applied.
        """
        if self.total_amount is not None:
            return self.total_amount
        gross = (self.quantity or 0.0) * (self.price or 0.0)
        if self.transaction_type == TX_SELL:
            return gross - self.fees
        return -(gross + self.fees)


class TransactionUpdate(BaseModel):
    """Partial edit of a ledger row. Type and ticker are not editable --
    changing either makes it a different transaction, which is a delete and a
    create, and doing it in place would silently rewrite a derived position."""

    quantity: Optional[float] = Field(None, gt=0)
    price: Optional[float] = Field(None, ge=0)
    total_amount: Optional[float] = None
    fees: Optional[float] = Field(None, ge=0)
    transaction_date: Optional[datetime] = None
    note: Optional[str] = None


def _transaction_row(tx: InvestmentTransaction) -> dict:
    return {
        "id": tx.id,
        "ticker": tx.ticker,
        "transaction_type": tx.transaction_type,
        "quantity": _f(tx.quantity),
        "price": _f(tx.price),
        "total_amount": _f(tx.total_amount),
        "fees": _f(tx.fees),
        "transaction_date": tx.transaction_date,
        "listed_currency": tx.listed_currency,
        "exchange_rate": _f(tx.exchange_rate),
        "source": tx.source,
        "note": tx.note,
        "created_at": tx.created_at,
    }


@app.get(
    "/api/investments/transactions",
    dependencies=[Depends(verify_clerk_token)],
)
async def list_transactions(
    ticker: Optional[str] = None,
    limit: int = 500,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(InvestmentTransaction).order_by(
        InvestmentTransaction.transaction_date.desc()
    )
    if ticker:
        stmt = stmt.where(InvestmentTransaction.ticker == ticker.strip().upper())
    rows = (await session.execute(stmt.limit(min(limit, 2000)))).scalars().all()
    return [_transaction_row(r) for r in rows]


@app.post(
    "/api/investments/transactions",
    status_code=201,
    dependencies=[Depends(verify_clerk_token)],
)
async def create_transaction(
    params: TransactionCreate, session: AsyncSession = Depends(get_session)
):
    """Record a purchase, sale, dividend or transfer.

    Creates the holding row too if this is the first the book has heard of the
    ticker. A transaction implies a holding, and refusing here would mean
    every new position took two calls in a fixed order -- with the ledger left
    holding orphans if the second one failed.
    """
    ticker = params.ticker

    existing = (
        await session.execute(
            select(InvestmentHolding).where(InvestmentHolding.ticker == ticker)
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            InvestmentHolding(
                ticker=ticker,
                listed_currency=params.listed_currency,
                exchange_rate=params.exchange_rate,
            )
        )

    tx = InvestmentTransaction(
        ticker=ticker,
        transaction_type=params.transaction_type,
        quantity=params.quantity,
        price=params.price,
        total_amount=params.resolved_total(),
        fees=params.fees,
        transaction_date=params.transaction_date,
        listed_currency=params.listed_currency,
        exchange_rate=params.exchange_rate,
        source="MANUAL",
        note=params.note,
    )
    session.add(tx)
    await session.commit()
    await session.refresh(tx)
    return _transaction_row(tx)


@app.patch(
    "/api/investments/transactions/{transaction_id}",
    dependencies=[Depends(verify_clerk_token)],
)
async def update_transaction(
    transaction_id: uuid.UUID,
    params: TransactionUpdate,
    session: AsyncSession = Depends(get_session),
):
    tx = await session.get(InvestmentTransaction, transaction_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found.")

    patch = params.model_dump(exclude_unset=True)
    for field, value in patch.items():
        setattr(tx, field, value)

    # Recomputed when the parts moved but the total was not itself restated,
    # so editing a price cannot leave a cash figure describing the old one.
    if "total_amount" not in patch and {"quantity", "price", "fees"} & set(patch):
        gross = (_f(tx.quantity) or 0.0) * (_f(tx.price) or 0.0)
        fees = _f(tx.fees) or 0.0
        tx.total_amount = (
            gross - fees if tx.transaction_type == TX_SELL else -(gross + fees)
        )

    await session.commit()
    await session.refresh(tx)
    return _transaction_row(tx)


@app.delete(
    "/api/investments/transactions/{transaction_id}",
    dependencies=[Depends(verify_clerk_token)],
)
async def delete_transaction(
    transaction_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    tx = await session.get(InvestmentTransaction, transaction_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    ticker = tx.ticker
    await session.delete(tx)
    await session.commit()
    return {"id": transaction_id, "ticker": ticker, "deleted": True}


@app.post(
    "/api/investments/sync",
    dependencies=[Depends(verify_clerk_token)],
)
async def sync_investment_transactions(session: AsyncSession = Depends(get_session)):
    """Import fills from the long-term book's own IBKR account.

    A SEPARATE endpoint from /api/ingest/ibkr, and deliberately so. That
    function carries every repair the trading ledger has needed -- resumable
    staging, suppressed fills, undated and unpriced fills, FIFO rematching --
    and none of it applies to a holding bought monthly and kept for a decade.
    Forking inside it would put a working ledger of 337 rows at risk to serve
    a book that wants none of that machinery.

    Reads IBKR_INVESTMENT_QUERY_ID, which must point at a Flex query scoped to
    the ACCOUNT the long-term book is held in. That separation is not a
    convenience: seven of fourteen holdings here are also swing-traded in the
    journal, so a fill's ticker cannot say which book it belongs to. The
    broker has to answer that question, and it answers it by account.

    Idempotent through `external_id`, which is UNIQUE -- re-running imports
    nothing twice, and a hand-entered row (external_id NULL) is never touched,
    since Postgres treats NULLs as distinct.

    Trades only. Dividends stay manual: the Flex queries emit no cash
    transaction nodes and the parser reads none.
    """
    from services import ibkr_client, ibkr_parser  # noqa: PLC0415 - import cycle
    from services import investment_sync  # noqa: PLC0415

    query_id = (os.environ.get("IBKR_INVESTMENT_QUERY_ID") or "").strip()
    if not query_id:
        raise HTTPException(
            status_code=503,
            detail=(
                "IBKR_INVESTMENT_QUERY_ID is not set. It must be a Flex query "
                "scoped to the account the long-term book is held in -- "
                "pointing it at the trading account would import swing trades "
                "as long-term holdings."
            ),
        )

    try:
        # token is left unset on purpose -- fetch_statements resolves it from
        # IBKR_TOKEN independently of query_ids, so the shared Flex token is
        # used with THIS query id rather than the trading queries.
        statements, query_failures = await ibkr_client.fetch_statements(
            query_ids=[query_id]
        )
    except ibkr_client.IBKRError as exc:
        raise HTTPException(status_code=503 if exc.retryable else 502, detail=str(exc))

    executions = []
    for _query_id, root in statements:
        executions.extend(ibkr_parser.parse_statement(root))

    rows = investment_sync.to_investment_transactions(executions)
    skipped = len(executions) - len(rows)

    if not rows:
        return {
            "fills_parsed": len(executions),
            "imported": 0,
            "duplicates": 0,
            "skipped": skipped,
            "holdings_created": [],
            "queries_failed": query_failures,
        }

    # Holdings first: a transaction references a ticker the book may not carry
    # yet, and the manual path (create_transaction) creates one alongside the
    # first fill for exactly this reason. Same behaviour here so a synced
    # position does not arrive as an orphan.
    tickers = {row["ticker"] for row in rows}
    existing = {
        t for (t,) in (
            await session.execute(
                select(InvestmentHolding.ticker).where(
                    InvestmentHolding.ticker.in_(tickers)
                )
            )
        ).all()
    }
    created = sorted(tickers - existing)
    for ticker in created:
        session.add(InvestmentHolding(ticker=ticker))

    # ON CONFLICT on external_id is the whole dedup story -- no staging table,
    # unlike the trading side. `ibkr_executions` exists there because a partial
    # ingest has to be resumable across a FIFO rebuild; nothing here rebuilds
    # anything, so a second copy of the broker's rows would be a table that can
    # only drift from the one beneath it.
    result = await session.execute(
        pg_insert(InvestmentTransaction)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["external_id"])
        .returning(InvestmentTransaction.external_id)
    )
    imported = len(result.fetchall())

    await session.commit()

    return {
        "fills_parsed": len(executions),
        "imported": imported,
        "duplicates": len(rows) - imported,
        "skipped": skipped,
        "holdings_created": created,
        "queries_failed": query_failures,
    }


# ---------------------------------------------------------------------------
# Valuation inputs: what the model was fed, and what the user changed
# ---------------------------------------------------------------------------


class ValuationOverride(BaseModel):
    """A deliberate correction to one or more fetched inputs.

    Every field is optional and NULL means "fall back to auto for this one" --
    an override sets the figures you disagree with, not all of them. Sending
    null explicitly clears that single field back to the fetched value.
    """

    base_flow: Optional[float] = None
    metric: Optional[str] = Field(None, max_length=24)
    shares_outstanding: Optional[float] = Field(None, gt=0)
    total_debt: Optional[float] = Field(None, ge=0)
    cash_and_st: Optional[float] = Field(None, ge=0)
    beta: Optional[float] = Field(None, ge=0)
    growth_1_5: Optional[float] = None
    discount_rate: Optional[float] = Field(None, gt=0)
    region: Optional[str] = None

    @field_validator("region")
    @classmethod
    def _valid_region(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        region = value.strip().upper()
        if region not in {"US", "HK"}:
            raise ValueError("region must be US or HK")
        return region


def _input_row(row: Optional[InvestmentValuationInput]) -> Optional[dict]:
    if row is None:
        return None
    return {
        "variant": row.variant,
        "base_flow": _f(row.base_flow),
        "metric": row.metric,
        "shares_outstanding": _f(row.shares_outstanding),
        "total_debt": _f(row.total_debt),
        "cash_and_st": _f(row.cash_and_st),
        "beta": _f(row.beta),
        "growth_1_5": _f(row.growth_1_5),
        "discount_rate": _f(row.discount_rate),
        "region": row.region,
        "source": row.source,
        "updated_at": row.updated_at,
    }


def _merge_inputs(
    auto: Optional[InvestmentValuationInput],
    override: Optional[InvestmentValuationInput],
) -> tuple[dict, list[str]]:
    """Override's non-NULL fields win; everything else falls through to auto.

    Returns the merged values and the names of the fields the override
    actually supplied, so the UI can mark them rather than leaving a reader to
    diff two columns by eye.
    """
    merged: dict = {}
    overridden: list[str] = []

    for field in VALUATION_FIELDS:
        auto_value = getattr(auto, field, None) if auto else None
        over_value = getattr(override, field, None) if override else None
        if over_value is not None:
            merged[field] = over_value
            overridden.append(field)
        else:
            merged[field] = auto_value

    # `region` is NOT NULL in the table, so an override row always carries one
    # and would otherwise look overridden on every ticker.
    if override is not None and auto is not None and "region" in overridden:
        if override.region == auto.region:
            overridden.remove("region")

    return merged, overridden


@app.put(
    "/api/investments/holdings/{ticker}/valuation-override",
    dependencies=[Depends(verify_clerk_token)],
)
async def set_valuation_override(
    ticker: str,
    params: ValuationOverride,
    session: AsyncSession = Depends(get_session),
):
    """Create or replace the override row for one ticker.

    A PUT rather than a PATCH: the modal edits the whole set of inputs at
    once, and a partial merge would make "I cleared this field" and "I did not
    touch this field" the same request.
    """
    ticker = ticker.strip().upper()
    await _get_holding(session, ticker)

    values = params.model_dump()
    values["region"] = values.get("region") or "US"

    await session.execute(
        pg_insert(InvestmentValuationInput)
        .values(ticker=ticker, variant=VARIANT_OVERRIDE, source="manual",
                updated_at=datetime.now(timezone.utc), **values)
        .on_conflict_do_update(
            index_elements=["ticker", "variant"],
            set_={**values, "source": "manual",
                  "updated_at": datetime.now(timezone.utc)},
        )
    )
    await session.commit()

    row = (
        await session.execute(
            select(InvestmentValuationInput).where(
                InvestmentValuationInput.ticker == ticker,
                InvestmentValuationInput.variant == VARIANT_OVERRIDE,
            )
        )
    ).scalar_one_or_none()
    return _input_row(row)


@app.delete(
    "/api/investments/holdings/{ticker}/valuation-override",
    dependencies=[Depends(verify_clerk_token)],
)
async def clear_valuation_override(
    ticker: str, session: AsyncSession = Depends(get_session)
):
    """Discard every override for a ticker, returning it to fetched inputs.

    The 'auto' row is untouched, which is the point of storing the two
    separately: reverting a judgement cannot cost you the baseline.
    """
    ticker = ticker.strip().upper()
    result = await session.execute(
        delete(InvestmentValuationInput).where(
            InvestmentValuationInput.ticker == ticker,
            InvestmentValuationInput.variant == VARIANT_OVERRIDE,
        )
    )
    await session.commit()
    return {"ticker": ticker, "cleared": result.rowcount or 0}


# ---------------------------------------------------------------------------
# Refreshing what the model is fed
# ---------------------------------------------------------------------------


@app.post(
    "/api/investments/refresh-prices",
    dependencies=[Depends(verify_clerk_token)],
)
async def refresh_prices(session: AsyncSession = Depends(get_session)):
    """Latest quote for every holding. One FMP call each -- the daily path.

    Separate from the fundamentals refresh because the two go stale at
    completely different rates, and folding them together would spend four
    calls per holding to learn a price.
    """
    from services import market_data  # noqa: PLC0415 - network at request time

    holdings = (
        await session.execute(select(InvestmentHolding).order_by(InvestmentHolding.ticker))
    ).scalars().all()
    if not holdings:
        return {"updated": 0, "failed": 0, "failures": []}

    updated = 0
    failures: list[dict] = []
    now = datetime.now(timezone.utc)

    import httpx  # noqa: PLC0415

    async with httpx.AsyncClient(timeout=market_data.TIMEOUT) as client:
        for holding in holdings:
            try:
                quote = await market_data.fetch_quote(holding.ticker, client=client)
            except market_data.MarketDataError as exc:
                # One dead symbol must not cost the other thirteen their
                # prices, so this is collected rather than raised.
                failures.append({"ticker": holding.ticker, "detail": str(exc)})
                if exc.status == 429:
                    break
                continue
            holding.current_price = quote.price
            holding.price_updated_at = now
            updated += 1

    await session.commit()
    return {"updated": updated, "failed": len(failures), "failures": failures}


@app.post(
    "/api/investments/refresh",
    dependencies=[Depends(verify_clerk_or_cron_token)],
)
async def refresh_valuation_inputs(
    force: bool = False,
    ticker: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """Re-fetch fundamentals and growth, and upsert the 'auto' rows.

    Runs on a Northflank Cron Job now rather than a button -- see
    verify_clerk_or_cron_token for how it authenticates without a browser.
    REFRESH_MAX_AGE (25 days) is what makes a monthly schedule idempotent
    without `force`: a run on the 1st always finds everything from the
    previous 1st older than the guard, and a re-run the same day is a no-op.

    THE MONTHLY PATH, and it is slow on purpose: roughly three FMP calls per
    holding plus one throttled Finviz request every five seconds, so a
    thirteen-name book takes about ninety seconds. Both budgets are the
    reason -- FMP allows 250 calls a day, and Finviz has no API at all and
    starts refusing after about three rapid requests.

    Rows refreshed within REFRESH_MAX_AGE are skipped unless `force`, which
    makes an accidental second press cost nothing. Override rows are never
    touched by any of this.
    """
    from services import growth as growth_service  # noqa: PLC0415
    from services import market_data  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    stmt = select(InvestmentHolding).order_by(InvestmentHolding.ticker)
    if ticker:
        stmt = stmt.where(InvestmentHolding.ticker == ticker.strip().upper())
    holdings = (await session.execute(stmt)).scalars().all()

    # An ETF has no cash flows of its own, so there is nothing here to fetch
    # and a throttled request spent on one buys nothing.
    valuable = [h for h in holdings if h.is_valuable]

    existing = {
        row.ticker: row
        for row in (
            await session.execute(
                select(InvestmentValuationInput).where(
                    InvestmentValuationInput.variant == VARIANT_AUTO
                )
            )
        ).scalars().all()
    }

    now = datetime.now(timezone.utc)
    outcomes: list[dict] = []
    due: list[InvestmentHolding] = []

    for holding in valuable:
        row = existing.get(holding.ticker)
        fresh_enough = (
            row is not None
            and row.updated_at is not None
            and now - row.updated_at < REFRESH_MAX_AGE
        )
        if fresh_enough and not force:
            outcomes.append({
                "ticker": holding.ticker, "status": "skipped",
                "detail": f"refreshed {(now - row.updated_at).days}d ago",
            })
        else:
            due.append(holding)

    if not due:
        return {"refreshed": 0, "skipped": len(outcomes), "failed": 0,
                "outcomes": outcomes}

    # One throttled pass for the whole batch rather than a call per ticker:
    # the pacing and the give-up-once-blocked behaviour both live in there.
    growth_by_ticker = await growth_service.fetch_growth_many(
        [h.ticker for h in due]
    )

    refreshed = failed = 0
    async with httpx.AsyncClient(timeout=market_data.TIMEOUT) as client:
        for holding in due:
            try:
                fundamentals = await market_data.fetch_fundamentals(
                    holding.ticker, client=client
                )
            except market_data.MarketDataError as exc:
                failed += 1
                outcomes.append({"ticker": holding.ticker, "status": "failed",
                                 "detail": str(exc)})
                if exc.status == 429:
                    # The daily allowance is spent; every remaining fetch
                    # would fail the same way.
                    break
                continue

            estimate = growth_by_ticker.get(holding.ticker)
            region = market_data.region_for(
                fundamentals.country or holding.country, fundamentals.currency
            )

            values = {
                # The workbook's definition, not FMP's headline: its input is
                # labelled "Total Debt (excl. Lease Obligations)" and FMP's
                # totalDebt includes them.
                "base_flow": fundamentals.free_cash_flow_m,
                "metric": "free_cash_flow",
                "shares_outstanding": fundamentals.shares_outstanding_m,
                "total_debt": fundamentals.total_debt_ex_leases_m,
                "cash_and_st": fundamentals.cash_and_st_m,
                "beta": fundamentals.beta,
                "growth_1_5": estimate.growth_1_5 if estimate else None,
                "region": region,
                # Left NULL so the engine derives it from beta and region.
                # Only a user pinning a rate by hand fills this in.
                "discount_rate": None,
                "source": (estimate.source if estimate else "fmp")[:24],
                "updated_at": now,
            }

            await session.execute(
                pg_insert(InvestmentValuationInput)
                .values(ticker=holding.ticker, variant=VARIANT_AUTO, **values)
                .on_conflict_do_update(
                    index_elements=["ticker", "variant"], set_=values
                )
            )

            # Classification the user has not set by hand, filled in from the
            # same response rather than spending another call later.
            holding.name = holding.name or fundamentals.name
            holding.sector = holding.sector or fundamentals.sector
            holding.country = holding.country or fundamentals.country
            if fundamentals.price is not None:
                holding.current_price = fundamentals.price
                holding.price_updated_at = now

            refreshed += 1
            outcomes.append({
                "ticker": holding.ticker,
                "status": "refreshed",
                "growth_source": estimate.source if estimate else None,
                "growth_is_forward": estimate.is_forward if estimate else None,
                "growth_clamped": estimate.clamped if estimate else None,
                "detail": None if estimate else "no growth estimate; set one by hand",
            })

    await session.commit()
    return {
        "refreshed": refreshed,
        "skipped": sum(1 for o in outcomes if o["status"] == "skipped"),
        "failed": failed,
        "outcomes": outcomes,
    }


# ---------------------------------------------------------------------------
# The portfolio, valued
# ---------------------------------------------------------------------------


def _holding_row(holding: InvestmentHolding) -> dict:
    return {
        "ticker": holding.ticker,
        "name": holding.name,
        "sector": holding.sector,
        "category": holding.category,
        "holding_type": holding.holding_type,
        "country": holding.country,
        "listed_currency": holding.listed_currency,
        "exchange_rate": _f(holding.exchange_rate),
        "planned_allocation": _f(holding.planned_allocation),
        "is_valuable": holding.is_valuable,
        "current_price": _f(holding.current_price),
        "price_updated_at": holding.price_updated_at,
    }


def _value_holding(
    holding: InvestmentHolding, merged: dict, overridden: Sequence[str]
) -> Optional[dict]:
    """Run the DCF for one holding, or return None when it cannot be run.

    None rather than a zero: a missing input means the model has nothing to
    say, and a zero intrinsic value would render as "worth nothing" -- an
    assertion, where silence is the truth.
    """
    base_flow = merged.get("base_flow")
    shares = merged.get("shares_outstanding")
    growth = merged.get("growth_1_5")
    if base_flow is None or not shares or growth is None:
        missing = [
            name for name, value in (
                ("base_flow", base_flow),
                ("shares_outstanding", shares),
                ("growth_1_5", growth),
            ) if value is None or value == 0
        ]
        return {"available": False, "missing": missing}

    inputs = valuation_engine.ValuationInputs(
        ticker=holding.ticker,
        base_flow=float(base_flow),
        shares_outstanding=float(shares),
        growth_1_5=float(growth),
        beta=_f(merged.get("beta")),
        total_debt=float(merged.get("total_debt") or 0.0),
        cash_and_st_investments=float(merged.get("cash_and_st") or 0.0),
        region=merged.get("region") or "US",
        exchange_rate=_f(holding.exchange_rate) or 1.0,
        discount_rate_override=_f(merged.get("discount_rate")),
    )
    result = valuation_engine.value(inputs)
    price = _f(holding.current_price)

    def scenario(s) -> dict:
        return {
            "scenario": s.scenario,
            "intrinsic_value": s.intrinsic_value,
            "growth_1_5": s.growth_1_5,
            "growth_6_10": s.growth_6_10,
            "growth_11_20": s.growth_11_20,
        }

    return {
        "available": True,
        "discount_rate": result.discount_rate,
        "base": scenario(result.base),
        "conservative": scenario(result.conservative),
        "average_intrinsic_value": result.average_intrinsic_value,
        # Positive means the market is asking more than the model says it is
        # worth. None when there is no price to compare against, rather than
        # a 0% that would read as "fairly priced".
        "premium_pct": result.premium_pct(price) if price else None,
        "overridden_fields": list(overridden),
    }


@app.get(
    "/api/investments/portfolio",
    dependencies=[Depends(verify_clerk_token)],
)
async def get_portfolio(session: AsyncSession = Depends(get_session)):
    """The whole book: position, price, valuation and weight, per holding.

    One endpoint rather than several because portfolio weight is the reason:
    it cannot be computed for a row without the total across every other row,
    and returning rows that each needed a second call to become meaningful
    would just move the join into the browser.
    """
    holdings = (
        await session.execute(select(InvestmentHolding).order_by(InvestmentHolding.ticker))
    ).scalars().all()
    if not holdings:
        return {
            "holdings": [], "total_market_value": 0.0, "total_cost_basis": 0.0,
            "total_unrealized_pnl": 0.0, "total_realized_pnl": 0.0,
            "total_dividends": 0.0, "as_of": datetime.now(timezone.utc),
        }

    positions = await _derive_all_positions(session, [h.ticker for h in holdings])

    inputs_by_ticker: dict[str, dict[str, InvestmentValuationInput]] = {}
    for row in (
        await session.execute(select(InvestmentValuationInput))
    ).scalars().all():
        inputs_by_ticker.setdefault(row.ticker, {})[row.variant] = row

    rows: list[dict] = []
    total_market_value = 0.0

    for holding in holdings:
        position = positions.get(holding.ticker) or DerivedPosition(ticker=holding.ticker)
        price = _f(holding.current_price)
        # Nothing held is not the same as held and worth zero. A watchlist
        # entry, or a position fully exited, has no market value and no
        # unrealised P&L -- and rendering those as 0.00 and +0.00 would put a
        # green gain of nothing against every ticker being tracked but not
        # owned.
        market_value = (price * position.quantity) if price and position.quantity else None
        if market_value:
            total_market_value += market_value

        variants = inputs_by_ticker.get(holding.ticker, {})
        merged, overridden = _merge_inputs(
            variants.get(VARIANT_AUTO), variants.get(VARIANT_OVERRIDE)
        )

        rows.append({
            **_holding_row(holding),
            "quantity": position.quantity,
            "average_cost": position.average_cost,
            "cost_basis": position.cost_basis,
            "market_value": market_value,
            "unrealized_pnl": (
                market_value - position.cost_basis if market_value is not None else None
            ),
            "unrealized_pnl_pct": (
                (market_value / position.cost_basis - 1.0) * 100.0
                if market_value is not None and position.cost_basis > 0 else None
            ),
            "realized_pnl": position.realized_pnl,
            "dividends": position.dividends,
            "transaction_count": position.transaction_count,
            "first_acquired": position.first_acquired,
            "valuation": (
                _value_holding(holding, merged, overridden)
                if holding.is_valuable else None
            ),
            "inputs": {
                "auto": _input_row(variants.get(VARIANT_AUTO)),
                "override": _input_row(variants.get(VARIANT_OVERRIDE)),
                "merged": {k: _f(v) if isinstance(v, Decimal) else v
                           for k, v in merged.items()},
            },
            # Filled in below, once the total it divides by is known.
            "portfolio_weight_pct": None,
        })

    for row in rows:
        if row["market_value"] and total_market_value > 0:
            row["portfolio_weight_pct"] = row["market_value"] / total_market_value * 100.0

    return {
        "holdings": rows,
        "total_market_value": total_market_value,
        "total_cost_basis": sum(r["cost_basis"] for r in rows),
        "total_unrealized_pnl": sum(
            r["unrealized_pnl"] for r in rows if r["unrealized_pnl"] is not None
        ),
        "total_realized_pnl": sum(r["realized_pnl"] for r in rows),
        "total_dividends": sum(r["dividends"] for r in rows),
        "as_of": datetime.now(timezone.utc),
    }
