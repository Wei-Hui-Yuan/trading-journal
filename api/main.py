import asyncio
import hashlib
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
    BigInteger,
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
    """A rule the trader holds themselves to. User-editable (migration 013).

    NULL `strategy_id` is a general rule, checked on every reviewed trade.
    Set, it scopes the rule to one playbook entry (migration 032) -- "Price
    reclaimed the prior day high on volume" only means something on a trade
    actually following that strategy, and only the review checklist for a
    trade tagged with it should ever show it.

    Uniqueness on `name` is NOT declared here as `unique=True`: it needs to
    hold separately within each strategy and separately among general rules,
    which a single-column constraint cannot express. Migration 032 enforces
    it with two partial indexes instead -- see that file for why one
    composite UNIQUE(strategy_id, name) does not work (Postgres does not
    treat two NULLs as equal, so it would silently permit the same general
    rule name twice).
    """

    __tablename__ = "disciplines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(Text, nullable=False)
    # ON DELETE CASCADE, not SET NULL: a strategy-specific rule has no
    # meaning once its strategy is gone, and SET NULL would silently turn it
    # into a general rule asked on every OTHER trade instead.
    strategy_id = Column(
        UUID(as_uuid=True),
        ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=True,
    )
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


class PlanDiscipline(Base):
    """Whether one pre-trade plan's checklist rule was ticked (migration 033).

    Shaped exactly like PositionDiscipline, but answers a different question
    at a different time: this is what you expected to do before the trade,
    not what you actually did. list_positions consults it only to seed a
    starting value for a pending position's review checklist -- see
    _plan_defaults_by_position. Nothing in services/analytics.py reads this
    table; a plan-time answer counts towards a discipline score only once it
    has been saved for real through review_position.
    """

    __tablename__ = "plan_disciplines"

    plan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("planned_trades.id", ondelete="CASCADE"),
        primary_key=True,
    )
    discipline_id = Column(
        UUID(as_uuid=True),
        ForeignKey("disciplines.id", ondelete="CASCADE"),
        primary_key=True,
    )
    followed = Column(Boolean, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PositionDisciplineOut(BaseModel):
    """One rule's answer, for one round trip or one plan.

    Carries `name` alongside the id so a caller can render the checklist
    without a second lookup, and so a historical answer stays readable if the
    rule is later renamed. Shared between positions and plans -- the shape is
    identical either way, only which join table it came from differs.
    """

    discipline_id: uuid.UUID
    name: str
    followed: bool


async def _upsert_discipline_answers(
    session: AsyncSession,
    model: type,
    owner_column: str,
    owner_id: uuid.UUID,
    answers: dict[uuid.UUID, bool],
) -> None:
    """Upsert discipline answers into a position_disciplines-shaped table.

    Shared by the post-trade review (PositionDiscipline) and the pre-trade
    plan checklist (PlanDiscipline) -- both are (owner_id, discipline_id) ->
    followed, differing only in which table and which column names the owner.
    Upsert rather than delete-and-reinsert so a corrected answer keeps its
    original created_at instead of looking freshly reviewed.
    """
    known = set(
        (
            await session.execute(
                select(Discipline.id).where(Discipline.id.in_(list(answers)))
            )
        ).scalars().all()
    )
    unknown = set(answers) - known
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown discipline rule(s): {sorted(str(u) for u in unknown)}",
        )

    for discipline_id, followed in answers.items():
        stmt = (
            pg_insert(model)
            .values(
                **{owner_column: owner_id},
                discipline_id=discipline_id,
                followed=bool(followed),
            )
            .on_conflict_do_update(
                index_elements=[owner_column, "discipline_id"],
                set_={"followed": bool(followed)},
            )
        )
        await session.execute(stmt)


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


# What set a sync off. Constrained in the database (migration 034) rather than
# left as free text, so a typo is a write-time error instead of a value that
# quietly matches no filter.
SYNC_TRIGGER_MANUAL = "manual"
SYNC_TRIGGER_CRON = "cron"

# How it ended. `partial` is deliberately its own outcome: some Flex queries
# did not return, so the ledger is short of fills that exist at the broker.
# Folding it into `success` is how a half-empty sync comes to look complete.
SYNC_OUTCOME_SUCCESS = "success"
SYNC_OUTCOME_PARTIAL = "partial"
SYNC_OUTCOME_ERROR = "error"

# Not an ending at all (migration 035). Written when a run starts so an
# in-flight sync is visible rather than inferred, and replaced when it ends.
#
# Neither a success nor a failure, and nothing that measures the ledger may
# read it as either. The staleness warning looks at the most recent SUCCESSFUL
# run, so a run stuck here can never make the journal look fresher than it is.
SYNC_OUTCOME_RUNNING = "running"

# How long a `running` row is believed before it is treated as abandoned.
#
# A worker recycled mid-ingest -- a redeploy, an OOM, a dropped container --
# leaves `running` behind with nobody left to finish it, and that row would
# otherwise refuse every future sync forever via the in-flight guard below.
#
# Sized off the ingest's own ceiling rather than picked. services.ibkr_client's
# TOTAL_BUDGET_SECONDS (240) bounds the Flex handshake and the pipeline's
# remaining DB work is seconds on top, so a run still 'running' six minutes in
# is not slow, it is gone.
#
# Written as a literal rather than derived, because everything in `services` is
# imported locally inside functions here to keep this module out of an import
# cycle, and a module-level constant cannot honour that. The relationship is
# enforced by test_the_reaper_waits_longer_than_the_fetch_budget instead, which
# reads the real value -- the same approach
# test_the_budget_is_below_the_worker_timeout takes to the Dockerfile.
#
# The margin is generous because the cost of being wrong is asymmetric: reaping
# too early marks a LIVE run failed and lets a second start beside it, while
# reaping too late only delays the next manual sync by a few minutes.
SYNC_RUN_ABANDONED_AFTER = timedelta(seconds=360)

# Strong references to in-flight background syncs.
#
# asyncio keeps only a WEAK reference to a task created by `create_task`, so a
# bare call can be garbage collected mid-run. It then stops silently, leaving a
# `running` row that nothing will ever finish and no traceback to explain it --
# the hardest possible version of this bug. Holding the task here until its own
# done-callback removes it is the documented way to avoid that.
_BACKGROUND_SYNCS: set[asyncio.Task] = set()


class SyncRun(Base):
    """One broker sync, recorded whether it worked or not (migration 034).

    The header badge used to read `LastSyncState` out of the browser's query
    cache, which meant it could only ever describe a sync that THIS tab
    performed. A scheduled run was therefore invisible by construction, and an
    unattended failure -- an expired token, say -- produced exactly what a
    quiet market produces: no new fills and no signal.

    Rows are written on every path including the failure ones, and in their own
    transaction. A row written inside the ingest transaction is lost when that
    transaction rolls back, which is the case it exists to document.
    """

    __tablename__ = "sync_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    started_at = Column(DateTime(timezone=True), nullable=False)
    # NULL means the run never reached its own recording step -- the process
    # died mid-flight, which is not the same as a run that failed and said so.
    finished_at = Column(DateTime(timezone=True), nullable=True)
    trigger = Column(Text, nullable=False)
    outcome = Column(Text, nullable=False)
    executions_parsed = Column(Integer, nullable=False, default=0)
    trades_created = Column(Integer, nullable=False, default=0)
    positions_matched = Column(Integer, nullable=False, default=0)
    plans_attached = Column(Integer, nullable=False, default=0)
    # The whole IngestResult as the browser received it. JSONB rather than a
    # column per figure: this is a record of what was reported, not something
    # anything aggregates -- and the shape has already changed twice.
    result = Column(JSONB, nullable=True)
    # Only when the run raised. The detail the user would have seen, kept so a
    # failure is still diagnosable days later.
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


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

# How long AFTER a fill a plan may still be written and claim it. A plan is
# supposed to precede its trade, and for a long time this was absolute -- but
# it caught the ordinary case of clicking buy and then writing the setup up a
# minute later (a real fill that trailed its plan by 61 seconds) and orphaned
# the plan permanently, since auto-attach never retries.
#
# The bound this trades against is hindsight: a plan written once the trade has
# resolved is a reconstruction presented as a prior commitment. Two hours does
# not protect against that on its own -- a five-minute scalp resolves well
# inside it -- which is why `_plan_can_claim` also refuses to reach past the
# position's exit. The window covers logging lag; the exit is what covers
# hindsight.
PLAN_ATTACH_GRACE = timedelta(hours=2)


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
    # The chart bucket's HTTP client is shared for the life of the process, for
    # the same reason the database pool is: a fresh TLS handshake to Supabase per
    # call is the dominant cost of serving a chart from us-central1. Imported
    # here rather than at module scope so a deployment with no Storage
    # credentials still boots -- services.storage reads them per call, never at
    # import, and closing a client that was never built is a no-op.
    from services import storage  # noqa: PLC0415

    await storage.aclose()


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
    # The browser's own HTTP cache revalidates without this -- If-None-Match is
    # set by the browser, not by fetch, and is not subject to CORS. Exposing
    # ETag is for JavaScript that wants to read it: without it the header is
    # present on the wire and invisible to the page, which turns any attempt to
    # debug a caching problem from the client into guesswork.
    #
    # Content-Disposition carries the filename of a CSV export. It is not a
    # CORS-safelisted response header either, so without this line the browser
    # strips it and the download lands under a name the page had to guess --
    # which is how an export of the trading book saves itself as "blob".
    expose_headers=["ETag", "Content-Disposition"],
    # Starlette's default is 600, and this deployment cannot afford it. Every
    # request the app makes carries Authorization and a JSON content type, both
    # non-simple, so EVERY endpoint URL is preflighted -- and the preflight is a
    # full round trip that must complete before the real request is even sent.
    # The API runs in us-central1 against a browser in Singapore, measured at
    # ~218ms one way, so each expiry costs roughly half a second of nothing
    # happening, per endpoint, on a tab left open for ten minutes.
    #
    # A day is the ceiling Firefox honours; Chrome clamps to 7200 and Safari
    # lower still. Asking for more than a browser allows is not an error, it is
    # simply clamped -- so this reads as "cache it for as long as you are
    # willing" rather than as a literal 24 hours.
    #
    # What staleness buys: a preflight decision, not a response and not an
    # authorisation. An origin dropped from CORS_ALLOW_ORIGINS keeps working in
    # an already-open tab until its cached decision expires. Every route still
    # demands a valid Clerk JWT on the actual request, which no preflight cache
    # touches, so the exposure is one already-loaded page continuing to reach an
    # API that would still refuse it without credentials.
    max_age=86400,
)


# ---------------------------------------------------------------------------
# IBKR automated ingestion
# ---------------------------------------------------------------------------


class FlexFailureOut(BaseModel):
    """One query that did not return, and what would make it return.

    Exists because "the sync failed" is not actionable and every failure used
    to read the same. A statement IBKR has not compiled yet, an expired token
    and a deleted query all arrive as a failed query, and the three want
    completely different responses -- wait, re-issue a credential, fix the
    query. `category` is that difference, carried to the UI so it can say which
    one this is instead of always advising a retry.
    """

    # IBKR's own words, unmodified: the interpretation below is ours, and the
    # source of truth travels beside it so the two can be compared.
    message: str
    # None when the failure carried no IBKR code -- a network fault rather than
    # a refusal.
    code: Optional[str] = None
    label: str
    # wait | query | token | request -- see services/ibkr_client.FLEX_CODES.
    category: str
    # Empty when the label already says everything useful.
    guidance: str = ""


def _read_flex_failures(failures: Sequence[str]) -> list[FlexFailureOut]:
    """Attach a reading to each raw failure message.

    Pure string work over a list that is at most one entry per configured
    query, so this costs nothing worth measuring and runs on the failure path
    only.
    """
    from services import ibkr_client  # noqa: PLC0415 - import cycle

    read = []
    for message in failures:
        diagnosis = ibkr_client.classify_failure(message)
        read.append(
            FlexFailureOut(
                message=message,
                code=diagnosis.code,
                label=diagnosis.label,
                category=diagnosis.category,
                guidance=diagnosis.guidance,
            )
        )
    return read


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
    #
    # SUPERSEDED by `flex_failures` below, which says which KIND of failure
    # each query hit rather than collapsing every transient code into one
    # boolean. Kept because the frontend and this API deploy independently --
    # a browser running the previous bundle still reads this field.
    rate_limited: bool = False
    # Each failed query, read: the IBKR code, what it means, and whether
    # waiting can fix it. Sent alongside `queries_failed` rather than replacing
    # it for the same deploy-ordering reason.
    flex_failures: list[FlexFailureOut] = []
    # Pre-trade plans this sync matched to the fills that finally arrived.
    # Worth its own line because it is the moment the two halves of the
    # journal meet: the plan you wrote, and what the broker actually did.
    plans_attached: int = 0
    # True when step 3b actually wrote refreshed broker figures onto the
    # ledger. Reported because it is the ONE write in this pipeline that can
    # move a displayed number while every fill counter stays zero and
    # `symbols_touched` stays empty: `broker_cost_basis` feeds
    # `_acquisition_premium`, and list_round_trips uses that for open exposure.
    #
    # A caller deciding whether a sync changed anything -- the browser's cache
    # invalidation does exactly this -- would otherwise be right on every run
    # except the one where IBKR re-lots, which is precisely the run where being
    # wrong shows a stale figure.
    broker_figures_refreshed: bool = False
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


async def _run_ibkr_ingest(session: AsyncSession) -> IngestResult:
    """Fetch, stage, promote, and match IBKR executions.

    Pipeline, each step idempotent:
      1. Flex handshake -> statement XML
      2. Parse into normalized executions
      3. Batch upsert into `ibkr_executions` (ON CONFLICT DO NOTHING on
         transaction_id) -- the absolute duplicate guard
      4. Promote only genuinely new rows into `trades`
      5. Re-run FIFO matching for each affected symbol

    Not the endpoint itself: `ingest_ibkr` below wraps this so that every run,
    including the ones that raise, leaves a `sync_runs` row behind. Keeping the
    pipeline a plain function means the recording cannot accidentally swallow
    an exception the endpoint is supposed to return as a status code.
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
            flex_failures=_read_flex_failures(query_failures),
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
    # Reported on the result so the browser can tell a sync that changed
    # something from one that did not. This is the ONLY write in the pipeline
    # that can move a displayed figure while leaving `symbols_touched` empty:
    # `broker_cost_basis` feeds `_acquisition_premium`, which list_round_trips
    # uses for open exposure. Inferring "nothing changed" from the fill counts
    # alone would therefore be wrong on exactly the run where IBKR re-lots.
    broker_figures_refreshed = False

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
        #
        # Issued only when it would actually change something, and the check is
        # a SELECT rather than a narrower UPDATE because of how the invalidation
        # it triggers works. `trades` carries the statement-level
        # trades_bump_data_version trigger from migration 030, and a
        # statement-level trigger in Postgres fires once per STATEMENT whether
        # it touched a thousand rows or none -- verified against this database:
        # `UPDATE trades ... WHERE 1=0` reports `UPDATE 0` and still increments
        # the counter. So adding the IS DISTINCT FROM guard below to the UPDATE
        # alone would write fewer rows and invalidate exactly as much; the
        # statement has to not be ISSUED. Reads fire no trigger, hence the
        # SELECT.
        #
        # What that counter costs when it moves: it is the ETag validator for
        # list_round_trips, /api/analytics/dashboard and /api/analytics/advanced,
        # so bumping it throws away every browser-cached analytics payload and
        # forces the next request to rebuild from a full table scan. An
        # unchanged sync -- the common case, and every scheduled one on a quiet
        # day -- was doing exactly that, daily.
        #
        # The three predicates are built once and shared by both statements on
        # purpose. If the SELECT and the UPDATE could drift apart, the guard
        # would start answering a different question than the one the write
        # asks, and the failure would be silent in both directions.
        matches_its_fill = (
            Trade.__table__.c.ibkr_exec_id
            == "IBKR-" + IBKRExecution.__table__.c.transaction_id
        )
        broker_sent_a_figure = or_(
            IBKRExecution.__table__.c.fifo_pnl_realized.is_not(None),
            IBKRExecution.__table__.c.broker_cost.is_not(None),
        )
        # IS DISTINCT FROM rather than != because both sides are nullable, and
        # `NULL != NULL` is NULL, not true -- a plain inequality would treat
        # "both absent" as a difference and rewrite the row forever.
        #
        # Compared at the LEDGER's scale, which is the part that is easy to get
        # wrong and silently ineffective. The two sides are not the same type:
        # fifo_pnl_realized is NUMERIC(14,6) and broker_realized_pnl is
        # NUMERIC(12,4), so assigning one to the other ROUNDS it. Comparing the
        # raw values therefore reports a difference on every row that has ever
        # been written -- the ledger holds -7.4052 because it rounded staging's
        # -7.405154, and will round it again to the same value next time. On
        # this database that was 175 of 348 rows disagreeing permanently, which
        # would have made the guard below fire on every single sync while
        # looking entirely correct.
        #
        # Rounding to the destination's own scale asks the question that
        # actually matters: would writing this change what is stored? The scale
        # is read off the column rather than written as a literal, so a schema
        # change cannot leave this comparison behind.
        def _as_stored(source, destination):
            scale = destination.type.scale
            return source if scale is None else func.round(source, scale)

        ledger_disagrees = or_(
            Trade.__table__.c.broker_realized_pnl.is_distinct_from(
                _as_stored(
                    IBKRExecution.__table__.c.fifo_pnl_realized,
                    Trade.__table__.c.broker_realized_pnl,
                )
            ),
            Trade.__table__.c.broker_cost_basis.is_distinct_from(
                _as_stored(
                    IBKRExecution.__table__.c.broker_cost,
                    Trade.__table__.c.broker_cost_basis,
                )
            ),
        )

        stale_row_exists = (
            await session.execute(
                select(1)
                .select_from(Trade.__table__)
                .join(IBKRExecution.__table__, matches_its_fill)
                .where(broker_sent_a_figure, ledger_disagrees)
                .limit(1)
            )
        ).first() is not None

        if stale_row_exists:
            broker_figures_refreshed = True
            await session.execute(
                update(Trade.__table__)
                .where(matches_its_fill, broker_sent_a_figure, ledger_disagrees)
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
        flex_failures=_read_flex_failures(query_failures),
        suppressed_skipped=resurrected,
        plans_attached=plans_attached,
        broker_figures_refreshed=broker_figures_refreshed,
        positions_removed=positions_removed,
        reviews_discarded=reviews_discarded,
        symbols_recovered=recovered,
    )


async def _finish_sync_run(
    *,
    run_id: Optional[uuid.UUID],
    started_at: datetime,
    trigger: str,
    outcome: str,
    result: Optional[IngestResult] = None,
    error: Optional[str] = None,
) -> None:
    """Record how a run ended, in a session of its own.

    Updates the row `_claim_sync_slot` opened. Falls back to INSERTing one when
    `run_id` is None or the row has gone -- a record of the outcome matters more
    than which statement produced it, and an outcome with no row at all is the
    one thing this function exists to prevent.

    ITS OWN SESSION, deliberately. The request's session is the one the ingest
    ran in, and the row most worth keeping is the one describing a run that
    failed -- which is exactly the run whose transaction is about to roll back
    and take the record with it. A separate session commits independently of
    whatever the pipeline did.

    NEVER RAISES. A sync that worked must not be reported as a failure because
    the bookkeeping afterwards hit a problem, and a sync that failed must
    surface its own error rather than this one. A recording failure is logged
    and swallowed; the worst case is a missing row, which is strictly better
    than a misreported outcome. This holds doubly now that the caller may be a
    background task with nobody left to return an error to.
    """
    finished_at = datetime.now(timezone.utc)
    counters = {
        "executions_parsed": result.executions_parsed if result else 0,
        "trades_created": result.trades_created if result else 0,
        "positions_matched": result.positions_matched if result else 0,
        "plans_attached": (result.plans_attached or 0) if result else 0,
        # mode="json" so datetimes and UUIDs inside the payload survive the trip
        # into JSONB; the default mode leaves objects asyncpg cannot serialise.
        "result": result.model_dump(mode="json") if result else None,
        "error": error,
    }
    try:
        async with SessionLocal() as session:
            updated = 0
            if run_id is not None:
                updated = (
                    await session.execute(
                        update(SyncRun.__table__)
                        .where(SyncRun.__table__.c.id == run_id)
                        .values(
                            finished_at=finished_at,
                            outcome=outcome,
                            **counters,
                        )
                    )
                ).rowcount or 0

            if not updated:
                session.add(
                    SyncRun(
                        id=run_id or uuid.uuid4(),
                        started_at=started_at,
                        finished_at=finished_at,
                        trigger=trigger,
                        outcome=outcome,
                        **counters,
                    )
                )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - see NEVER RAISES above
        logger.error("Could not record the sync run: %s", exc)


async def _claim_sync_slot(
    *, started_at: datetime, trigger: str
) -> tuple[Optional[uuid.UUID], Optional[SyncRun]]:
    """Reap abandoned runs, then take the slot if nothing live holds it.

    Returns `(run_id, None)` when the slot was taken, or `(None, blocker)` when
    a genuine run is already in flight -- the endpoint turns the second into a
    409 rather than starting a second ingest beside the first.

    THE REAP HAS TO HAPPEN FIRST, and it is the whole reason this is one
    function rather than two. A worker recycled mid-ingest leaves 'running'
    behind with nobody to finish it, and a guard that trusted that row would
    refuse every future sync forever -- turning a transient container restart
    into a permanently broken Sync button that only a database edit could clear.

    Concurrency here is not fully closed, and deliberately so. Two requests
    arriving in the same instant could both see an empty slot; the ingest itself
    is safe under that (ticker advisory locks, ON CONFLICT on transaction_id --
    see run_matching_for_ticker), so the cost is a duplicated fetch rather than
    a corrupted ledger. Closing it properly means an advisory lock or a unique
    partial index held across the whole run, which is a lot of machinery for a
    single-user journal whose realistic double-press is two clicks a second
    apart -- and those the guard does catch.
    """
    cutoff = datetime.now(timezone.utc) - SYNC_RUN_ABANDONED_AFTER
    try:
        async with SessionLocal() as session:
            abandoned = (
                await session.execute(
                    update(SyncRun.__table__)
                    .where(
                        SyncRun.__table__.c.outcome == SYNC_OUTCOME_RUNNING,
                        SyncRun.__table__.c.started_at < cutoff,
                    )
                    .values(
                        outcome=SYNC_OUTCOME_ERROR,
                        finished_at=datetime.now(timezone.utc),
                        error=(
                            "The run stopped reporting and was assumed "
                            "abandoned -- most likely the container was "
                            "restarted mid-sync. Any fills it had already "
                            "promoted are picked up by the next run."
                        ),
                    )
                )
            ).rowcount or 0
            if abandoned:
                logger.warning(
                    "Reaped %d abandoned sync run(s) older than %s",
                    abandoned,
                    SYNC_RUN_ABANDONED_AFTER,
                )

            blocker = (
                await session.execute(
                    select(SyncRun)
                    .where(SyncRun.outcome == SYNC_OUTCOME_RUNNING)
                    .order_by(SyncRun.started_at.desc())
                    .limit(1)
                )
            ).scalars().first()

            if blocker is not None:
                await session.commit()  # keep the reap even though we refuse
                return None, blocker

            run_id = uuid.uuid4()
            session.add(
                SyncRun(
                    id=run_id,
                    started_at=started_at,
                    # Left NULL on purpose. `finished_at IS NULL` and
                    # `outcome = 'running'` say the same thing, and the one that
                    # is constrained is the one worth reading.
                    finished_at=None,
                    trigger=trigger,
                    outcome=SYNC_OUTCOME_RUNNING,
                )
            )
            await session.commit()
            return run_id, None
    except Exception as exc:  # noqa: BLE001 - degrade to "no slot claimed"
        # Reported as "took the slot with no id". The endpoint then runs inline
        # and `_finish_sync_run` INSERTs the outcome, so a database hiccup here
        # costs the 202 and the guard, not the sync.
        logger.error("Could not claim a sync run slot: %s", exc)
        return None, None


async def _ingest_and_record(
    *,
    run_id: Optional[uuid.UUID],
    started_at: datetime,
    trigger: str,
    session: Optional[AsyncSession] = None,
) -> IngestResult:
    """Run the pipeline and record how it ended, whatever that turns out to be.

    Pass `session` to borrow the request's; omit it and one is opened here, which
    is what the background path needs -- a request's session is closed the moment
    its response is returned, so a task that outlived the response and kept using
    it would fail on its first query.

    Both failure paths record and then re-raise untouched, so an inline caller
    still gets its 502/503 with the same detail: the recording observes the
    outcome, it does not change it. The background caller has nobody to raise to,
    which is exactly why the row is the point.
    """
    async def run(active: AsyncSession) -> IngestResult:
        try:
            return await _run_ibkr_ingest(active)
        except HTTPException as exc:
            await _finish_sync_run(
                run_id=run_id,
                started_at=started_at,
                trigger=trigger,
                outcome=SYNC_OUTCOME_ERROR,
                error=f"HTTP {exc.status_code}: {exc.detail}",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised as-is
            await _finish_sync_run(
                run_id=run_id,
                started_at=started_at,
                trigger=trigger,
                outcome=SYNC_OUTCOME_ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    if session is not None:
        result = await run(session)
    else:
        async with SessionLocal() as owned:
            result = await run(owned)

    # A run that could not reach every query is NOT a success. It leaves the
    # ledger short of fills that exist at the broker, and the staleness check
    # reads this column to decide whether the data can be trusted as current.
    await _finish_sync_run(
        run_id=run_id,
        started_at=started_at,
        trigger=trigger,
        outcome=(
            SYNC_OUTCOME_PARTIAL if result.queries_failed else SYNC_OUTCOME_SUCCESS
        ),
        result=result,
    )
    return result


class IngestAccepted(BaseModel):
    """A run that has been started, for a caller that is not going to wait."""

    run_id: str
    outcome: str = SYNC_OUTCOME_RUNNING
    detail: str = (
        "The sync is running. Its outcome will appear on the header badge."
    )


@app.post("/api/ingest/ibkr", response_model=None)
async def ingest_ibkr(
    session: AsyncSession = Depends(get_session),
    auth: dict = Depends(verify_clerk_or_cron_token),
):
    """Start a broker sync. Waits for it if the scheduler asked, not if a browser did.

    THE SPLIT, because it looks like an inconsistency and is the opposite:

      * A BROWSER gets 202 and a run id. The IBKR Flex handshake takes 15 to 240
        seconds -- the broker compiles the statement on its own schedule and the
        client polls for it -- and freezing the button for that long, with no
        progress and no way to navigate away, was the worst interaction in the
        app. The browser follows the run through /api/sync/runs/latest instead.

      * The SCHEDULER waits, and gets the full IngestResult exactly as before.
        Nobody is watching a spinner on a cron run, and its exit code is the only
        signal Northflank has: answering 202 would make every scheduled run look
        successful the moment it could reach the API, which is precisely the
        confusion `sync_runs` was added to end. It also means the Northflank job
        command needs no change.

    Both paths write the same rows through the same code. The only difference is
    who awaits `_ingest_and_record`.

    A second Sync while one is in flight is a 409 rather than a second ingest.
    Two concurrent runs are safe at the data layer -- ticker advisory locks and
    ON CONFLICT on transaction_id -- so this is about not showing two runs in a
    UI with room for one, and about not spending IBKR's per-token rate limit
    twice for the same fills.

    `verify_clerk_or_cron_token` accepts a Clerk session OR the scheduler's
    shared secret, which is what made a Northflank Cron Job possible without
    also leaving this open to an unauthenticated caller: unset CRON_SECRET
    behaves exactly like the plain Clerk check it replaced. Its return value is
    captured here rather than left as a bare `dependencies=[]` entry specifically
    so `trigger` does not have to guess which path authenticated the request --
    and now so this handler knows whether anyone is waiting.
    """
    started_at = datetime.now(timezone.utc)
    is_cron = bool(auth.get("cron"))
    trigger = SYNC_TRIGGER_CRON if is_cron else SYNC_TRIGGER_MANUAL

    run_id, blocker = await _claim_sync_slot(started_at=started_at, trigger=trigger)

    if blocker is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "A sync started "
                f"{blocker.started_at.isoformat()} is still running. Wait for "
                "it to finish -- its outcome will appear on the header badge."
            ),
        )

    # The scheduler waits. So does a browser whose slot could not be recorded:
    # `_claim_sync_slot` returning no id means the database would not take the
    # row, and handing out a 202 pointing at a run nobody can observe is worse
    # than making the caller wait. Degrading to the old behaviour is the safe
    # direction.
    if is_cron or run_id is None:
        return await _ingest_and_record(
            run_id=run_id,
            started_at=started_at,
            trigger=trigger,
            session=session,
        )

    # Deliberately NOT given the request's session: it is closed as soon as this
    # response is returned, and the task outlives that. `_ingest_and_record`
    # opens its own.
    #
    # The reference is kept so the task is not garbage collected mid-flight --
    # asyncio holds only a weak reference to a bare create_task, and a collected
    # task stops silently, which is the hardest possible version of this bug.
    task = asyncio.create_task(
        _ingest_and_record(run_id=run_id, started_at=started_at, trigger=trigger)
    )
    _BACKGROUND_SYNCS.add(task)
    task.add_done_callback(_BACKGROUND_SYNCS.discard)

    return JSONResponse(
        status_code=202,
        content=IngestAccepted(run_id=str(run_id)).model_dump(),
    )


class SyncRunOut(BaseModel):
    """One recorded sync, as the header badge reads it."""

    id: uuid.UUID
    started_at: datetime
    finished_at: Optional[datetime] = None
    trigger: str
    outcome: str
    executions_parsed: int
    trades_created: int
    positions_matched: int
    plans_attached: int
    error: Optional[str] = None
    # The full IngestResult this run produced, as stored in JSONB.
    #
    # Carried so the toast can be rendered from a run the tab did not perform.
    # A browser now hands the sync off and follows the row, so the result is no
    # longer available as a mutation's return value -- and a run started in
    # another tab, or by the scheduler, never was.
    #
    # Typed loosely on purpose: it is a snapshot of whatever IngestResult looked
    # like when the row was written, and re-validating an old row against
    # today's model would fail on a run recorded before a field was added. The
    # frontend reads it as a partial and tolerates absence.
    result: Optional[dict] = None


class SyncStatusOut(BaseModel):
    """Everything the badge needs, in one request.

    `last_success_at` is separate from `latest` on purpose. The newest run and
    the newest run that WORKED are different questions, and only the second one
    answers "is the ledger current". A week of nightly failures has a very
    recent `latest` and a very old `last_success_at`, and reporting only the
    first is how a broken schedule keeps looking busy.
    """

    latest: Optional[SyncRunOut] = None
    last_success_at: Optional[datetime] = None
    # Server-computed so the client is not doing arithmetic against a clock
    # that may not agree with this one. Null when nothing has ever succeeded.
    seconds_since_success: Optional[float] = None
    # True when `latest` is still 'running' but has been for longer than a run
    # can legitimately take.
    #
    # The reaper only runs when a sync is STARTED, because a GET has no business
    # writing rows. Without this flag the browser would poll a stranded run
    # forever and the badge would read "syncing" until someone happened to press
    # Sync. Reported rather than left to the client so the threshold lives in one
    # place -- SYNC_RUN_ABANDONED_AFTER -- instead of being duplicated in
    # TypeScript and left to drift.
    latest_looks_abandoned: bool = False


@app.get(
    "/api/sync/runs/latest",
    response_model=SyncStatusOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def latest_sync_run(session: AsyncSession = Depends(get_session)):
    """The last sync, and the last one that succeeded.

    Browser-only: the scheduler writes runs, it has no reason to read them.
    """
    latest = (
        await session.execute(
            select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)
        )
    ).scalars().first()

    # A partial run is deliberately NOT a success here: it means some Flex
    # queries did not return, so the ledger is knowingly short of fills.
    last_success = (
        await session.execute(
            select(SyncRun.started_at)
            .where(SyncRun.outcome == SYNC_OUTCOME_SUCCESS)
            .order_by(SyncRun.started_at.desc())
            .limit(1)
        )
    ).scalars().first()

    now = datetime.now(timezone.utc)
    return SyncStatusOut(
        latest=SyncRunOut.model_validate(latest, from_attributes=True) if latest else None,
        last_success_at=last_success,
        seconds_since_success=(
            (now - last_success).total_seconds() if last_success else None
        ),
        latest_looks_abandoned=(
            latest is not None
            and latest.outcome == SYNC_OUTCOME_RUNNING
            and latest.started_at < now - SYNC_RUN_ABANDONED_AFTER
        ),
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


@app.post("/api/audit", dependencies=[Depends(verify_clerk_token)])
async def run_data_audit(session: AsyncSession = Depends(get_session)):
    """Does the stored, derived state still agree with the fills underneath it?

    The diagnosis half of the pair this sits next to: this one only reads and
    reports, `/api/rematch` above is what fixes what it finds.

    POST, not GET, and deliberately not cached. It is an action with a cost --
    a cold FIFO rebuild of every ticker plus the stored-state comparisons,
    measured at ~2.6s over 88 tickers and 199 legs on this ledger -- and
    caching it would answer a weaker question than the one the UI asks: a
    stored result says the ledger WAS healthy whenever it last ran, which is
    exactly the claim that goes stale without anyone noticing. A few seconds
    behind an explicit button is not worth a cache-invalidation story.

    AUTHENTICATED, unlike `/health`. That one is a liveness probe and is
    deliberately free of trade data; this reports per-trade P&L and named
    executions, so the two must not be confused despite the similar shape.
    """
    from services.data_health import run_audit  # noqa: PLC0415 - avoids import cycle

    return await run_audit(session)


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
    closed_at: Optional[datetime] = None,
) -> bool:
    """Whether an open plan is allowed to claim a fill that just arrived.

    Separated from the allocation loop because this rule, not the loop, is what
    decides whether an attachment is trustworthy.

    A plan may trail the fill by up to PLAN_ATTACH_GRACE -- logging lag, not
    hindsight -- but never past the moment the position closed: `closed_at` is
    the earliest opposing fill after `executed_at`, and once that exists the
    outcome is on the screen, so anything written after it is a reconstruction
    presented as a prior commitment. `closed_at` is None for a position still
    open, which has no outcome yet to have seen. And it must be recent overall
    -- without PLAN_ATTACH_MAX_AGE, a setup written months ago and never
    cancelled would silently claim the next fill on that ticker, and the
    mis-attribution would look exactly like the feature working.
    """
    return (
        plan.status == PLAN_OPEN
        and plan.ticker == ticker
        and (plan.direction or "").upper() == (direction or "").upper()
        and plan.created_at is not None
        and plan.created_at <= executed_at + PLAN_ATTACH_GRACE
        and (closed_at is None or plan.created_at <= closed_at)
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

    Three rules keep a stale or hindsight-written plan from claiming a fill it
    has nothing to do with: a plan may trail the fill by no more than
    PLAN_ATTACH_GRACE, it may never be written after the position closed, and
    it must be no older than PLAN_ATTACH_MAX_AGE, so a setup you wrote up and
    forgot stops competing. See `_plan_can_claim` for the detail.

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

    # The earliest opposing fill on each ticker, which is what bounds the grace
    # window per group below. One query for every ticker rather than one query
    # per group: both collections are small, and the join is cheaper in Python
    # than a round trip each. Not restricted to `created_exec_ids` -- the fill
    # that closes a position can be an OLDER row this sync did not touch, and
    # missing it would let the grace window reach past a close that already
    # happened.
    tickers = {t for t, _, _ in groups}
    earliest = min(fills[0].entry_date for fills in groups.values())
    opposing = (
        await session.execute(
            select(Trade.ticker, Trade.direction, Trade.entry_date).where(
                Trade.ticker.in_(tickers),
                Trade.entry_date >= earliest,
            )
        )
    ).all()

    # Allocated in Python rather than re-querying per group, so a plan claimed
    # by one group is not offered to the next before the flush lands.
    used: set[uuid.UUID] = set()
    attached = 0

    for (ticker, direction, _day), fills in sorted(
        groups.items(), key=lambda kv: kv[1][0].entry_date
    ):
        executed_at = fills[0].entry_date
        # The moment this position closed, if it has -- the earliest fill on
        # the OPPOSING side after this one opened. None leaves it open, which
        # is what lets a plan for a position still running claim it any time.
        closed_at = min(
            (
                when
                for tkr, dirn, when in opposing
                if tkr == ticker
                and (dirn or "").upper() != (direction or "").upper()
                and when > executed_at
            ),
            default=None,
        )
        match = next(
            (
                plan
                for plan in candidates
                if plan.id not in used
                and _plan_can_claim(plan, ticker, direction, executed_at, closed_at)
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

    # Pre-trade checklist answers (migration 033), keyed by discipline id.
    # Optional -- a plan is worth saving before any of its checklist is
    # answered. Carried into the post-trade review as a starting value only;
    # see _plan_defaults_by_position. Never itself read by analytics.
    disciplines: Optional[dict[uuid.UUID, bool]] = None

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

    # Same as PlanCreate.disciplines. Blocked, like every other field, once
    # the plan is ATTACHED -- see update_plan.
    disciplines: Optional[dict[uuid.UUID, bool]] = None

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


class PlanCandidate(BaseModel):
    """An unplanned opening leg this plan could be attached to by hand.

    Exists because a plan that auto-attach declined is indistinguishable, from
    the dock, from one whose fill simply has not arrived -- and the two want
    opposite actions from the user. One is a click; the other is patience.
    """

    # The fill to POST /api/trades/{trade_id}/attach-plan against. The earliest
    # in the leg, matching the anchor `_opening_leg_fills` resolves to.
    trade_id: uuid.UUID
    entry_date: datetime
    fill_count: int
    quantity: float
    avg_entry: float
    # Signed minutes from the fill to the plan: positive means the fill came
    # first. Sent as a number rather than a sentence so the UI owns the wording.
    minutes_from_fill_to_plan: Optional[float] = None


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
    # Pre-trade checklist answers (migration 033). Only rules actually
    # answered appear -- same absence-means-unanswered convention as
    # PositionOut.disciplines.
    disciplines: list[PositionDisciplineOut] = []
    # Fills that match this plan but are not linked to it. Only ever populated
    # for an OPEN plan; an attached or cancelled one has nothing to offer.
    candidates: list[PlanCandidate] = []


def _plan_out(
    plan: PlannedTrade,
    attached_ids: Sequence[uuid.UUID] = (),
    disciplines: Sequence[PositionDisciplineOut] = (),
    candidates: Sequence[PlanCandidate] = (),
) -> PlanOut:
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
        disciplines=list(disciplines),
        candidates=list(candidates),
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


# A plan row has no space to explain itself, so a handful of candidates is the
# useful number and twenty is a different problem than this feature solves.
PLAN_CANDIDATES_PER_PLAN = 5


async def _candidates_by_plan(
    session: AsyncSession, plans: Sequence[PlannedTrade]
) -> dict[uuid.UUID, list[PlanCandidate]]:
    """Unlinked fills each open plan could claim, keyed by plan id.

    Exists for the plan auto-attach declines and then never revisits: a plan
    written 61 seconds after its own fill is refused by `_plan_can_claim` and
    orphaned forever, since auto-attach only ever looks at the fills one sync
    just created. This is the escape hatch -- it does not change who may
    auto-attach, it surfaces who is left so a human can attach by hand via the
    existing POST /api/trades/{trade_id}/attach-plan.

    One query for every plan, grouped by ticker/side/market day -- the same
    boundary `_opening_leg_fills` uses, so the leg offered here is exactly the
    leg an attach would cover, and the count shown cannot disagree with what
    happens on the click. `_plan_can_claim`'s grace window and age bound are
    deliberately NOT re-applied: a plan orphaned by them is exactly the case
    this exists to rescue, and manual attach has never enforced either.
    """
    # created_at is server_default=func.now(), so None only ever happens on an
    # unflushed in-memory object -- excluded here because it has no age to
    # measure and could never legitimately reach this function anyway.
    open_plans = [
        p for p in plans if p.status == PLAN_OPEN and p.created_at is not None
    ]
    if not open_plans:
        return {}

    tickers = {p.ticker for p in open_plans}
    earliest_plan = min(p.created_at for p in open_plans)

    rows = (
        await session.execute(
            select(
                Trade.id,
                Trade.ticker,
                Trade.direction,
                Trade.entry_date,
                Trade.quantity,
                Trade.actual_entry,
            ).where(
                Trade.ticker.in_(tickers),
                Trade.plan_id.is_(None),
                # A fill from before the oldest candidate plan cannot be a
                # match for ANY plan in this batch -- every plan's own
                # PLAN_ATTACH_MAX_AGE bound is tighter than this, so this is
                # purely a query-size guard, not a second copy of that rule.
                Trade.entry_date >= earliest_plan - PLAN_ATTACH_MAX_AGE,
            )
        )
    ).all()
    if not rows:
        return {}

    # Same grouping _opening_leg_fills and _auto_attach_plans both use: one
    # entry, however many executions the broker split it into.
    groups: dict[tuple[str, str, object], list] = {}
    for row in rows:
        day = row.entry_date.astimezone(MARKET_TZ).date()
        groups.setdefault((row.ticker, row.direction, day), []).append(row)

    anchors: list[PlanCandidate] = []
    anchor_ticker_direction: list[tuple[str, str]] = []
    for (ticker, direction, _day), leg in groups.items():
        leg.sort(key=lambda r: r.entry_date)
        # Numeric columns arrive as Decimal already; no re-parse needed.
        total_qty: Decimal = sum((r.quantity for r in leg), Decimal("0"))
        if total_qty == 0:
            continue
        weighted: Decimal = sum(
            (r.quantity * r.actual_entry for r in leg), Decimal("0")
        )
        anchors.append(
            PlanCandidate(
                trade_id=leg[0].id,
                entry_date=leg[0].entry_date,
                fill_count=len(leg),
                quantity=float(total_qty),
                avg_entry=float(weighted / total_qty),
            )
        )
        anchor_ticker_direction.append((ticker, direction))

    out: dict[uuid.UUID, list[PlanCandidate]] = {}
    for plan in open_plans:
        matches = [
            candidate
            for candidate, (ticker, direction) in zip(anchors, anchor_ticker_direction)
            if ticker == plan.ticker
            and direction.upper() == (plan.direction or "").upper()
        ]
        # Newest fill first -- the one most likely to be the plan's own,
        # rather than an older leg the trader has since moved past.
        matches.sort(key=lambda c: c.entry_date, reverse=True)
        if not matches:
            continue
        out[plan.id] = [
            candidate.model_copy(
                update={
                    "minutes_from_fill_to_plan": (
                        plan.created_at - candidate.entry_date
                    ).total_seconds()
                    / 60,
                }
            )
            for candidate in matches[:PLAN_CANDIDATES_PER_PLAN]
        ]
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
    disciplines = await _disciplines_by_plan(session, [p.id for p in plans])
    candidates = await _candidates_by_plan(session, plans)
    return [
        _plan_out(
            p, attached.get(p.id, []), disciplines.get(p.id, []), candidates.get(p.id, [])
        )
        for p in plans
    ]


async def _new_plan(session: AsyncSession, params: PlanCreate) -> PlannedTrade:
    """Build, price and stage one plan -- flushed, not committed.

    Shared by `create_plan` and `promote_sizing_entry`, which turns a sizing
    scratchpad note into exactly this same kind of row. Flushing rather than
    committing leaves the transaction boundary to the caller: promotion needs
    the new plan and the deletion of the note it came from to succeed or fail
    together, and a commit in here would make that impossible.
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
    await session.flush()
    if params.disciplines:
        await _upsert_discipline_answers(
            session, PlanDiscipline, "plan_id", plan.id, params.disciplines
        )
    return plan


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
    plan = await _new_plan(session, params)
    await session.commit()
    # planned_r and the timestamps were produced by the database; re-read
    # rather than report what we sent, which did not include them.
    await session.refresh(plan)
    disciplines = (await _disciplines_by_plan(session, [plan.id])).get(plan.id, [])
    return _plan_out(plan, disciplines=disciplines)


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

    # Lives in its own table, like review_position's discipline_answers --
    # assigning it to the ORM object would silently become a stray Python
    # attribute that never reaches the database.
    discipline_answers = changes.pop("disciplines", None)

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

    if discipline_answers:
        await _upsert_discipline_answers(
            session, PlanDiscipline, "plan_id", plan.id, discipline_answers
        )

    plan.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(plan)

    attached = await _attached_ids_by_plan(session, [plan.id])
    disciplines = await _disciplines_by_plan(session, [plan.id])
    return _plan_out(plan, attached.get(plan.id, []), disciplines.get(plan.id, []))


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
# Sizing scratchpad
# ---------------------------------------------------------------------------
#
# A holding pen for the four numbers that decide how many shares to buy --
# entry, stop, take profit, quantity -- for the moment there is no time to
# open the Plan modal and write a real plan (migration 031). Nothing here is
# read by the matching engine or analytics; the only bridge to a real plan is
# `promote_sizing_entry` below, and it is one-way.


class SizingScratchpadEntry(Base):
    __tablename__ = "sizing_scratchpad"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticker = Column(String(10), nullable=False)
    direction = Column(String(5), nullable=False)
    entry = Column(Numeric(10, 4), nullable=True)
    stop_loss = Column(Numeric(10, 4), nullable=True)
    take_profit = Column(Numeric(10, 4), nullable=True)
    quantity = Column(Numeric(18, 8), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# How long a note survives before the read path below deletes it. Three days,
# matching the request this table was built for: a note is a reminder of what
# was decided moments ago, not a record worth keeping.
SIZING_SCRATCHPAD_MAX_AGE = timedelta(days=3)


def _valid_side(value: str) -> str:
    side = value.strip().upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("direction must be 'BUY' or 'SELL'")
    return side


class SizingEntryCreate(BaseModel):
    """One note. Only ticker and direction are required -- the rest may be
    filled in over several quick edits as a price is watched, matching the
    same "worth recording before it is complete" reasoning as PlanCreate."""

    ticker: str = Field(..., min_length=1, max_length=10)
    direction: str = Field(..., description="BUY or SELL")
    entry: Optional[float] = Field(None, gt=0)
    stop_loss: Optional[float] = Field(None, gt=0)
    take_profit: Optional[float] = Field(None, gt=0)
    quantity: Optional[float] = Field(None, gt=0)

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("direction")
    @classmethod
    def _check_direction(cls, value: str) -> str:
        return _valid_side(value)


class SizingEntryUpdate(BaseModel):
    """Partial update -- only fields present in the request body change."""

    ticker: Optional[str] = Field(None, min_length=1, max_length=10)
    direction: Optional[str] = None
    entry: Optional[float] = Field(None, gt=0)
    stop_loss: Optional[float] = Field(None, gt=0)
    take_profit: Optional[float] = Field(None, gt=0)
    quantity: Optional[float] = Field(None, gt=0)

    @field_validator("ticker")
    @classmethod
    def _upper_ticker(cls, value: Optional[str]) -> Optional[str]:
        return value.strip().upper() if value is not None else None

    @field_validator("direction")
    @classmethod
    def _check_direction(cls, value: Optional[str]) -> Optional[str]:
        return _valid_side(value) if value is not None else None


class SizingEntryOut(BaseModel):
    id: uuid.UUID
    ticker: str
    direction: str
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    quantity: Optional[float] = None
    created_at: datetime


def _sizing_entry_out(entry: SizingScratchpadEntry) -> SizingEntryOut:
    return SizingEntryOut(
        id=entry.id,
        ticker=entry.ticker,
        direction=entry.direction,
        entry=_f(entry.entry),
        stop_loss=_f(entry.stop_loss),
        take_profit=_f(entry.take_profit),
        quantity=_f(entry.quantity),
        created_at=entry.created_at,
    )


@app.get(
    "/api/sizing-scratchpad",
    response_model=list[SizingEntryOut],
    dependencies=[Depends(verify_clerk_token)],
)
async def list_sizing_entries(session: AsyncSession = Depends(get_session)):
    """Everything noted in the last three days, newest first.

    Deletes anything older FIRST, in this same request, rather than filtering
    it out of the SELECT. Storage was never the reason for the three-day
    limit -- these rows are a handful of numbers each -- so a filter that left
    the old rows sitting underneath would be the cosmetic version of "clears
    itself". Deleting here is the literal version, and needs no external
    scheduler: the tab clears on the next visit, always.
    """
    cutoff = datetime.now(timezone.utc) - SIZING_SCRATCHPAD_MAX_AGE
    await session.execute(
        delete(SizingScratchpadEntry).where(SizingScratchpadEntry.created_at < cutoff)
    )
    await session.commit()

    # id breaks ties. Postgres's now() is transaction-scoped, so two notes
    # created moments apart within the same transaction -- exactly what
    # happens in this file's own tests, and can happen in production under
    # a fast double-click -- get an IDENTICAL created_at, and created_at
    # alone is then not a total order.
    rows = (
        await session.execute(
            select(SizingScratchpadEntry).order_by(
                SizingScratchpadEntry.created_at.desc(),
                SizingScratchpadEntry.id.desc(),
            )
        )
    ).scalars().all()
    return [_sizing_entry_out(row) for row in rows]


@app.post(
    "/api/sizing-scratchpad",
    response_model=SizingEntryOut,
    status_code=201,
    dependencies=[Depends(verify_clerk_token)],
)
async def create_sizing_entry(
    params: SizingEntryCreate, session: AsyncSession = Depends(get_session)
):
    entry = SizingScratchpadEntry(
        id=uuid.uuid4(),
        ticker=params.ticker,
        direction=params.direction,
        entry=to_decimal(params.entry),
        stop_loss=to_decimal(params.stop_loss),
        take_profit=to_decimal(params.take_profit),
        quantity=to_decimal(params.quantity),
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return _sizing_entry_out(entry)


@app.patch(
    "/api/sizing-scratchpad/{entry_id}",
    response_model=SizingEntryOut,
    dependencies=[Depends(verify_clerk_token)],
)
async def update_sizing_entry(
    entry_id: uuid.UUID,
    params: SizingEntryUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Adjust a note in place.

    `created_at` is never touched, so editing a value does not restart its
    three-day clock -- a note is exactly as old as when it was first written
    down, however many times its numbers change before then.
    """
    entry = await session.get(SizingScratchpadEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Scratchpad entry not found.")

    updates = params.model_dump(exclude_unset=True)
    for field in ("entry", "stop_loss", "take_profit", "quantity"):
        if field in updates:
            updates[field] = to_decimal(updates[field])
    for key, value in updates.items():
        setattr(entry, key, value)

    await session.commit()
    await session.refresh(entry)
    return _sizing_entry_out(entry)


@app.delete(
    "/api/sizing-scratchpad/{entry_id}",
    status_code=204,
    dependencies=[Depends(verify_clerk_token)],
)
async def delete_sizing_entry(
    entry_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    entry = await session.get(SizingScratchpadEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Scratchpad entry not found.")
    await session.delete(entry)
    await session.commit()


@app.post(
    "/api/sizing-scratchpad/{entry_id}/promote",
    response_model=PlanOut,
    status_code=201,
    dependencies=[Depends(verify_clerk_token)],
)
async def promote_sizing_entry(
    entry_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    """Turn a scratchpad note into a real plan the matching engine can attach
    a fill to.

    One-way, and one transaction: the note is deleted the moment the plan
    exists, in the SAME commit as creating it. A failure partway through
    leaves neither behind, rather than a plan and its scratch copy both
    existing, or the note surviving a promotion that silently did not happen.
    The scratchpad is scrap paper once it has done its job.

    Strategy and thesis are not carried over -- the scratchpad never captured
    them, by design (see the section header above). The promoted plan starts
    without them, exactly like any plan entered without that detail; they can
    be added afterwards like any other plan.
    """
    entry = await session.get(SizingScratchpadEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Scratchpad entry not found.")

    plan = await _new_plan(
        session,
        PlanCreate(
            ticker=entry.ticker,
            direction=entry.direction,
            quantity=_f(entry.quantity),
            planned_entry=_f(entry.entry),
            stop_loss=_f(entry.stop_loss),
            take_profit=_f(entry.take_profit),
        ),
    )
    await session.delete(entry)
    await session.commit()
    await session.refresh(plan)
    return _plan_out(plan)


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
    # Checklist rules scoped to this strategy (migration 032). Reported so
    # the delete confirmation can say what disappears, but deliberately
    # EXCLUDED from `total` below: these cascade-delete with the strategy
    # automatically, with nowhere sensible to reassign a rule like "waited
    # for the gap fill" to a different setup. Forcing reassign_to on a
    # strategy that has never been traded, purely because someone wrote its
    # checklist first, would block a delete that has no real history to lose.
    checklist_items: int = 0

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
            (Discipline, "checklist_items"),
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
    # None is a general rule, checked on every trade. Set, it scopes the rule
    # to one playbook entry (migration 032) -- validated to actually exist
    # before the insert is attempted, so a bad id surfaces as its own 404
    # rather than as an FK violation wearing the duplicate-name 409's message.
    strategy_id: Optional[uuid.UUID] = None

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
    strategy_id: Optional[uuid.UUID] = None
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
    """Add a new discipline rule, general or scoped to one strategy."""
    if params.strategy_id is not None:
        strategy = await session.get(Strategy, params.strategy_id)
        if strategy is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="The strategy to scope this rule to was not found.",
            )

    discipline = Discipline(name=params.name, strategy_id=params.strategy_id)
    session.add(discipline)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        where = "in this strategy" if params.strategy_id is not None else "as a general rule"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A discipline rule named '{params.name}' already exists {where}",
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


async def _disciplines_by_plan(
    session: AsyncSession, plan_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[PositionDisciplineOut]]:
    """Checklist answers for many plans in one query, not one per row.

    Mirrors _disciplines_by_position exactly -- see that function.
    """
    if not plan_ids:
        return {}

    rows = (
        await session.execute(
            select(
                PlanDiscipline.plan_id,
                PlanDiscipline.discipline_id,
                PlanDiscipline.followed,
                Discipline.name,
            )
            .join(Discipline, Discipline.id == PlanDiscipline.discipline_id)
            .where(PlanDiscipline.plan_id.in_(plan_ids))
            .order_by(Discipline.created_at)
        )
    ).all()

    grouped: dict[uuid.UUID, list[PositionDisciplineOut]] = {}
    for plan_id, discipline_id, followed, name in rows:
        grouped.setdefault(plan_id, []).append(
            PositionDisciplineOut(
                discipline_id=discipline_id, name=name, followed=followed
            )
        )
    return grouped


async def _plan_defaults_by_position(
    session: AsyncSession, positions: Sequence[Position]
) -> dict[uuid.UUID, list[PositionDisciplineOut]]:
    """Checklist answers carried over from the plan that opened each position.

    A plan is about ENTRY, so it is the opening trade -- not the closing one
    -- whose plan_id names the plan to read: positions.open_trade_id ->
    trades.id -> trades.plan_id -> plan_disciplines. Consulted only by
    list_positions, and only to seed a starting value for a checklist nobody
    has answered yet; a position with its own position_disciplines row for a
    rule keeps that answer regardless of what the plan says.
    """
    open_trade_ids = [p.open_trade_id for p in positions]
    if not open_trade_ids:
        return {}

    rows = (
        await session.execute(
            select(
                Trade.id.label("trade_id"),
                PlanDiscipline.discipline_id,
                PlanDiscipline.followed,
                Discipline.name,
            )
            .select_from(Trade)
            .join(PlanDiscipline, PlanDiscipline.plan_id == Trade.plan_id)
            .join(Discipline, Discipline.id == PlanDiscipline.discipline_id)
            .where(Trade.id.in_(open_trade_ids))
        )
    ).all()

    by_trade: dict[uuid.UUID, list[PositionDisciplineOut]] = {}
    for trade_id, discipline_id, followed, name in rows:
        by_trade.setdefault(trade_id, []).append(
            PositionDisciplineOut(
                discipline_id=discipline_id, name=name, followed=followed
            )
        )

    return {
        position.id: by_trade[position.open_trade_id]
        for position in positions
        if position.open_trade_id in by_trade
    }


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
    # Only consulted below for positions still pending review -- see
    # _plan_defaults_by_position.
    plan_defaults = await _plan_defaults_by_position(session, positions)

    out: list[PositionOut] = []
    for position in positions:
        row = PositionOut.model_validate(position)
        answered = by_position.get(position.id, [])
        row.disciplines = answered
        if position.review_status == ReviewStatus.pending.value:
            # A rule the plan never answered, or one added to the strategy's
            # checklist after the plan was written, is left for the reviewer
            # to tick -- only filling gaps the plan actually has an opinion
            # on, never overriding what the plan itself did not cover.
            answered_ids = {a.discipline_id for a in answered}
            row.disciplines = answered + [
                d
                for d in plan_defaults.get(position.id, [])
                if d.discipline_id not in answered_ids
            ]
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


# Ceiling on one page of closed round trips. Not a default -- an unpaged
# request is still legal and still returns the whole ledger, which the totals
# elsewhere depend on. This only stops a `limit` large enough to be equivalent
# to no limit at all from arriving by accident and materialising every row in
# one response.
ROUND_TRIP_MAX_PAGE = 500


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
    request: Request,
    response: Response,
    ticker: Optional[str] = None,
    search: Optional[str] = None,
    kind: Optional[str] = None,
    strategy: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    """The journal, grouped the way trades are actually thought about.

    Closed round trips come from `positions`; open exposure is reconstructed
    from executions that FIFO matching never paired off. Both carry their plan
    and their fills, so one row is a whole trade rather than a fragment.

    FILTERING AND PAGING HAPPEN IN SQL, not after the fact. This used to load
    every position, every execution and every fill on every request, and then
    discard most of them in Python -- three unbounded scans to answer a
    question about twenty rows. The cost grew with the size of the ledger
    rather than with the size of the answer, which is the wrong thing for a
    journal meant to accumulate for years.

    `limit`/`offset` page the CLOSED half only. Open exposure is always
    returned whole: it is bounded by how many tickers can be held at once, not
    by history, and it is the half that needs decisions -- paging it would hide
    a live position behind a "load more" button. It comes back on the first
    page only (offset 0), so a client concatenating pages does not see every
    live position repeated once per page.

    `kind` selects one half outright. Without it both are returned, open first.

    `ticker` matches a symbol exactly; `search` matches any part of one. Both
    exist because they answer different questions -- "show me AAPL" and "what
    was that ticker starting with NV" -- and collapsing them would make the
    first one silently also match NVAAPL. `search` is what the journal's search
    box sends, and it has to run in SQL rather than over the loaded rows, or it
    would only ever find what the current page happened to contain.
    """
    # Reused rather than hardcoded: the role vocabulary and the cost-basis
    # replay both belong to the matching engine, and two copies would drift.
    from services.matching_engine import (  # noqa: PLC0415 - import cycle
        ROLE_CLOSE,
        ROLE_OPEN,
        replay_open_exposure,
    )

    if kind is not None and kind not in ("open", "closed"):
        raise HTTPException(
            status_code=422,
            detail="kind must be 'open' or 'closed', or omitted for both.",
        )
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset cannot be negative.")
    if limit is not None and limit < 1:
        raise HTTPException(status_code=422, detail="limit must be at least 1.")
    # A ceiling rather than a default: an unpaged request is still legal and
    # still returns everything, because the dashboard's own totals depend on
    # it. What this stops is `limit=10000000` being used to allocate the whole
    # ledger in one response by accident.
    page_size = min(limit, ROUND_TRIP_MAX_PAGE) if limit is not None else None

    # Resolved once, here, so the SQL below and the cache tag agree on what was
    # asked for. `unassigned` is a real filter value, not a missing one -- it
    # selects round trips whose opening execution carries no strategy.
    strategy_id: Optional[uuid.UUID] = None
    if strategy and strategy != "unassigned":
        try:
            strategy_id = uuid.UUID(strategy)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="strategy must be a UUID, 'unassigned', or omitted.",
            ) from exc

    # Normalised into the tag rather than taken from the query string, so
    # `?ticker=aapl` and `?ticker=AAPL` -- which produce identical payloads,
    # the filter being upper-cased below -- do not occupy two cache entries
    # that must each be revalidated separately.
    scoped = ticker.strip().upper() if ticker else ""

    # A LIKE pattern built from user input, with the wildcards neutralised.
    # Without escaping, typing `%` in the search box matches every symbol and
    # `_` matches any single character -- so the box would quietly behave as a
    # pattern language nobody documented, and `A_L` would find AAPL.
    searched = search.strip().upper() if search else ""
    search_pattern = (
        "%"
        + searched.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        + "%"
        if searched
        else ""
    )

    version = await _journal_version(session)
    # Every parameter that changes the payload is in the tag. Leaving the page
    # out would be the worst kind of caching bug: page 2 answered from page 1's
    # entry, silently, with a 304 and no way to tell from the client.
    etag = (
        None
        if version is None
        else _etag(
            "round-trips", version, scoped, searched, kind or "", strategy or "",
            page_size if page_size is not None else "", offset,
        )
    )
    if (cached := _conditional(request, etag)) is not None:
        return cached

    def _by_strategy(stmt, column):
        """Apply the strategy filter to whichever table carries it."""
        if not strategy:
            return stmt
        if strategy == "unassigned":
            return stmt.where(column.is_(None))
        return stmt.where(column == strategy_id)

    # --- closed round trips: one page of positions -------------------------
    positions: list[Position] = []
    if kind != "open":
        closed_stmt = select(Position)
        # The strategy lives on the OPENING EXECUTION, not on the position, so
        # filtering by it means joining the trade the position opened with.
        # open_trade_id has been NOT NULL since migration 019, which is what
        # makes an inner join safe rather than silently dropping rows.
        if strategy:
            closed_stmt = closed_stmt.join(Trade, Trade.id == Position.open_trade_id)
            closed_stmt = _by_strategy(closed_stmt, Trade.strategy_id)
        if scoped:
            closed_stmt = closed_stmt.where(Position.symbol == scoped)
        if search_pattern:
            closed_stmt = closed_stmt.where(
                Position.symbol.ilike(search_pattern, escape="\\")
            )
        # id breaks ties so the order is total. Without it two round trips
        # closing in the same instant can swap places between requests, and a
        # paged reader would see one twice and miss the other.
        closed_stmt = closed_stmt.order_by(
            Position.exit_time.desc(), Position.id.desc()
        )
        if page_size is not None:
            closed_stmt = closed_stmt.limit(page_size).offset(offset)
        positions = (await session.execute(closed_stmt)).scalars().all()

    # --- fills belonging to that page only ---------------------------------
    position_ids = [p.id for p in positions]
    fills: list[PositionFill] = []
    if position_ids:
        fills = (
            await session.execute(
                select(PositionFill).where(PositionFill.position_id.in_(position_ids))
            )
        ).scalars().all()

    # --- open exposure: only what FIFO never fully paired off ---------------
    #
    # The sum that decides this used to be built in Python from every fill row
    # in the table. It is a GROUP BY, so Postgres does it without the round
    # trip, and the answer is the handful of executions that are actually open
    # rather than the whole ledger filtered down to them.
    #
    # PARTLY matters, and is why this compares quantities instead of testing
    # membership: an oversell closes the long it was aimed at AND opens a short
    # with the remainder, so it appears in position_fills as a 10-share CLOSE
    # while five shares of it are a live position.
    # Open exposure belongs to the FIRST page and only the first page. It is
    # not paged -- it always arrives whole -- so repeating it on page two would
    # duplicate every live position in a client that concatenates pages, which
    # is the only sane way to consume an endpoint like this. offset 0 covers
    # both the unpaged request and the first page of a paged one.
    open_rows: list[tuple[Trade, Decimal]] = []
    if kind != "closed" and offset == 0:
        consumed_sq = (
            select(
                PositionFill.trade_id.label("trade_id"),
                func.sum(PositionFill.quantity).label("consumed"),
            )
            .group_by(PositionFill.trade_id)
            .subquery()
        )
        remaining_expr = Trade.quantity - func.coalesce(
            consumed_sq.c.consumed, literal(Decimal("0"))
        )
        open_stmt = (
            select(Trade, remaining_expr.label("remaining"))
            .outerjoin(consumed_sq, consumed_sq.c.trade_id == Trade.id)
            # NULL quantity yields NULL here, which fails this test and is
            # excluded -- matching _unmatched_quantity, which floors at zero.
            .where(remaining_expr > 0)
            .order_by(Trade.entry_date)
        )
        if scoped:
            open_stmt = open_stmt.where(Trade.ticker == scoped)
        if search_pattern:
            open_stmt = open_stmt.where(
                Trade.ticker.ilike(search_pattern, escape="\\")
            )
        open_stmt = _by_strategy(open_stmt, Trade.strategy_id)
        open_rows = [
            (row[0], Decimal(str(row[1])))
            for row in (await session.execute(open_stmt)).all()
        ]

    # --- the executions those two halves actually reference -----------------
    #
    # Bounded by the page, not by the ledger. Needed for two things: the plan
    # attached to each opening execution, and whether any fill behind a closed
    # round trip was added by hand.
    trade_by_id: dict[uuid.UUID, Trade] = {trade.id: trade for trade, _ in open_rows}
    wanted = (
        {p.open_trade_id for p in positions if p.open_trade_id is not None}
        | {f.trade_id for f in fills}
    ) - trade_by_id.keys()
    if wanted:
        for trade in (
            await session.execute(select(Trade).where(Trade.id.in_(wanted)))
        ).scalars().all():
            trade_by_id[trade.id] = trade

    # When each attached plan was written. It is the evidence that a plan
    # predates its fill, which is the only thing separating a plan from a
    # post-hoc annotation.
    plan_created_at: dict[uuid.UUID, datetime] = {}
    # Which of them carry a chart. Sent so the ledger can decide whether to
    # render the image slot at all -- without it every plan without a
    # screenshot would still fire a request for one and take a 404 to find out.
    plan_has_chart: dict[uuid.UUID, bool] = {}
    plan_ids = {t.plan_id for t in trade_by_id.values() if t.plan_id is not None}
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
    disciplines_by_position = await _disciplines_by_position(session, position_ids)
    fills_by_position: dict[uuid.UUID, list[PositionFill]] = {}
    for fill in fills:
        fills_by_position.setdefault(fill.position_id, []).append(fill)

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
    # Executions FIFO never paired off, in whole or in PART, as selected by the
    # query above. Grouped per ticker here, because that is the unit of
    # exposure: two unsold AAPL buys are one open position, not two.
    #
    # Partly matters, and is why the query compares quantities rather than
    # testing membership of position_fills. An oversell closes the long it was
    # aimed at AND opens a short with the remainder, so it is recorded as a
    # 10-share CLOSE while five shares of it are a live position. Being in the
    # table at all used to exclude it, and the short existed nowhere in the app
    # -- not as a row, not in exposure, not in the fill list. The matcher had it
    # the whole time in `MatchingResult.open_lots`; this endpoint never asked.
    open_by_ticker: dict[str, list[tuple[Trade, Decimal]]] = {}
    for trade, remaining in open_rows:
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
    response.headers.update(_cache_headers(etag))
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
        # Upsert: re-saving a review must correct the previous answer, not
        # collide with it. Deleting and re-inserting would lose created_at and
        # briefly leave the round trip looking unreviewed.
        await _upsert_discipline_answers(
            session, PositionDiscipline, "position_id", position_id, discipline_answers
        )

    if params.mark_reviewed:
        position.review_status = ReviewStatus.reviewed.value

    await session.commit()
    await session.refresh(position)
    return await _position_out(session, position)


# ---------------------------------------------------------------------------
# Conditional responses
# ---------------------------------------------------------------------------


class DataVersion(Base):
    """A counter bumped whenever the journal changes (migration 030).

    One row, scope 'journal'. Triggers on positions, trades, position_fills,
    realized_legs, planned_trades, strategies, disciplines and
    position_disciplines increment it; nothing in Python writes it.
    """

    __tablename__ = "data_version"

    scope = Column(Text, primary_key=True)
    version = Column(BigInteger, nullable=False, default=0)
    changed_at = Column(DateTime(timezone=True), server_default=func.now())


# Served on every conditionally-cacheable response. `no-cache` does not mean
# "do not store" -- it means "store it, but revalidate before reuse", which is
# exactly the contract here: the browser keeps the payload and asks whether it
# is still current, and the answer is a 304 with no body.
#
# `private` because these responses are per-user and the requests carry a
# bearer token. Shared caches must not keep them.
CONDITIONAL_CACHE_CONTROL = "private, no-cache"


async def _journal_version(session: AsyncSession) -> Optional[int]:
    """The current journal counter, or None if it cannot be read.

    None means "do not attempt to cache this response" -- served fresh, with no
    ETag, exactly as before this existed. That is the honest degradation for
    the window between deploying this code and applying migration 030, and it
    keeps a missing table from turning the whole dashboard into a 500.

    Only UndefinedTable is swallowed, matching list_timeframes: "the migration
    is not applied yet" and "the database is unreachable" must not look alike.
    """
    try:
        return await session.scalar(
            select(DataVersion.version).where(DataVersion.scope == "journal")
        )
    except ProgrammingError as exc:
        if not isinstance(getattr(exc, "orig", None), UndefinedTableError):
            raise
        logger.warning("data_version is missing; apply migration 030")
        await session.rollback()
        return None


def _etag(scope: str, version: int, *parts: object) -> str:
    """A validator for one payload, at one version, for one set of parameters.

    `parts` is what makes two requests to the same endpoint distinct -- the
    dashboard's window, above all. Without it a 1Y request and an ALL request
    would share a tag and each would be served the other's cached body.

    Hashed rather than concatenated so the tag stays a fixed, opaque length
    whatever the parameters are, and so a date containing a quote cannot break
    out of the header. Strong (unquoted by W/), because the bytes really are
    identical: the same version and the same window rebuild the same payload.
    """
    raw = "|".join([scope, str(version), *(str(part) for part in parts)])
    return '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32] + '"'


def _not_modified(request: Request, etag: str) -> bool:
    """Whether the client already holds this exact payload.

    If-None-Match is a LIST -- a client may hold several variants and offer all
    of them, and RFC 9110 allows `*`. Splitting on comma rather than comparing
    the raw header is what makes this work with more than one cached entry.

    Weak-comparison prefixes are stripped before comparing so a tag that made a
    round trip through a proxy that weakened it still matches; for a 304 that
    is the correct comparison to use.
    """
    header = request.headers.get("if-none-match")
    if not header:
        return False
    offered = {candidate.strip() for candidate in header.split(",")}
    if "*" in offered:
        return True
    return etag in {tag[2:] if tag.startswith("W/") else tag for tag in offered}


def _conditional(request: Request, etag: Optional[str]) -> Optional[Response]:
    """A 304 when the client's copy is current, otherwise None.

    Returned bare so the caller decides what to do next; a helper that also
    built the 200 would have to know how to serialise every payload shape.

    The 304 carries the ETag and Cache-Control again because a 304 REPLACES the
    stored headers for the cached entry. Omitting them leaves the browser's copy
    with no validator, so the next request is unconditional and the saving
    happens exactly once.
    """
    if etag is None or not _not_modified(request, etag):
        return None
    return Response(
        status_code=304,
        headers={"ETag": etag, "Cache-Control": CONDITIONAL_CACHE_CONTROL},
    )


def _cache_headers(etag: Optional[str]) -> dict[str, str]:
    """Headers for the 200 that accompanies a validator.

    Empty when there is no version to key on, which leaves the response
    uncacheable rather than tagged with something that cannot be trusted.
    """
    if etag is None:
        return {}
    return {"ETag": etag, "Cache-Control": CONDITIONAL_CACHE_CONTROL}


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@app.get("/api/analytics/dashboard", dependencies=[Depends(verify_clerk_token)])
async def analytics_dashboard(
    request: Request,
    response: Response,
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

    # Keyed on the RESOLVED window, not on the query string. `preset=1Y` names
    # a span that moves every day, and two requests a week apart mean different
    # things by it; the dates they expand to are what the payload actually
    # depends on. Resolving first is also what lets an explicit range and the
    # preset that happens to match it share a cache entry.
    version = await _journal_version(session)
    etag = (
        None
        if version is None
        else _etag("dashboard", version, window.start, window.end)
    )
    if (cached := _conditional(request, etag)) is not None:
        return cached

    payload = await build_dashboard(session, window)
    response.headers.update(_cache_headers(etag))
    return payload


@app.get("/api/analytics/advanced", dependencies=[Depends(verify_clerk_token)])
async def analytics_advanced(
    request: Request,
    response: Response,
    preset: Optional[str] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    session: AsyncSession = Depends(get_session),
):
    """R-multiples, slippage, expectancy, and the per-mistake breakdown.

    Sourced from `trades` rather than `positions`: R-multiple and slippage
    need the plan (stop_loss, planned_entry), which only the ledger carries.

    Windowed exactly like /api/analytics/dashboard -- same three ways to ask,
    same precedence, same 1Y default, and trades selected by when they CLOSED.
    That last point is the whole reason this takes a window at all: the two
    pages both showed a figure called "win rate" over different populations,
    and once the ledger passes a year of history an unwindowed advanced payload
    would have diverged from the dashboard again with nothing on screen saying
    why.

    The two selections are independent by design. Each page holds its own, so
    narrowing the dashboard to YTD leaves this alone -- they answer different
    questions and are read at different times.
    """
    from services.analytics import build_advanced_analytics, resolve_window

    try:
        window = resolve_window(preset, start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Keyed on the RESOLVED window, for the reason spelled out on the dashboard
    # endpoint above -- and here it is also a correctness fix rather than a
    # refinement. This ETag used to be `_etag("advanced", version)`, correct
    # only while the payload took no parameters. Adding the window without
    # adding it here would serve a cached 1Y payload for an ALL request, and
    # keep doing so until something unrelated bumped the version.
    version = await _journal_version(session)
    etag = (
        None
        if version is None
        else _etag("advanced", version, window.start, window.end)
    )
    if (cached := _conditional(request, etag)) is not None:
        return cached

    payload = await build_advanced_analytics(session, window)
    response.headers.update(_cache_headers(etag))
    return payload


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
    # Overrides current_price when set (migration 028). current_price keeps
    # being overwritten wholesale by refresh_prices underneath this -- the
    # auto figure is never lost, so "reset to auto" is just clearing this
    # column rather than re-fetching anything.
    manual_price = Column(Numeric(18, 4), nullable=True)
    manual_price_at = Column(DateTime(timezone=True), nullable=True)
    # The day's move in whole percent (migration 029). NOT guaranteed to
    # share price_updated_at's freshness -- refresh_prices only overwrites
    # this when the provider's response included it that time, so it can lag
    # behind a fresher price. day_change_updated_at (migration 036) is the
    # column that actually tracks when this was last written; compare it to
    # price_updated_at rather than assuming they match. Never affected by
    # manual_price: an override is a correction to the LEVEL, and says
    # nothing about the day's move.
    day_change_pct = Column(Numeric(10, 4), nullable=True)
    day_change_updated_at = Column(DateTime(timezone=True), nullable=True)
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
    # Migration 037. Display/context only, not part of the DCF math or the
    # override system -- see statement_exchange_rate for the number that
    # actually converts a non-USD filer's per-share figure.
    statement_currency = Column(String(3), nullable=True)
    # 1 USD in statement_currency -- see migration 037's comment for why
    # this is NULL (not 1.0) whenever the filing currency is not USD and no
    # one has supplied a rate: there is no FX provider to fetch one from,
    # and defaulting to 1.0 would silently reproduce the bug this exists to
    # fix (treating a non-USD per-share figure as if it were already USD).
    statement_exchange_rate = Column(Numeric(18, 8), nullable=True)
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

# A correction to quantity and/or cost basis (migration 028), written only by
# the basis-correction endpoint below -- deliberately NOT in TX_TYPES, so the
# general create-transaction API can never accept one. Unlike every type in
# TX_TYPES, its quantity and total_amount are SIGNED DELTAS rather than
# magnitudes: -0.5 corrects an overcounted half share, which TransactionCreate's
# quantity > 0 rule exists specifically to forbid for an ordinary trade.
TX_ADJUSTMENT = "ADJUSTMENT"

# Transactions that add shares at a cost. TRANSFER is here because a holding
# arriving from another broker keeps its basis -- it is a purchase whose cash
# left the account somewhere this book cannot see.
TX_ADDS_SHARES = (TX_BUY, TX_TRANSFER)

# The columns a valuation input row actually carries, in one place so the
# merge, the upsert and the override endpoint cannot drift apart.
#
# statement_currency is deliberately NOT here, the same way `source` is not:
# both are read-only display context (see _input_row), never an input the
# DCF math consumes or a trader corrects -- statement_exchange_rate is the
# one number that actually does either.
VALUATION_FIELDS = (
    "base_flow", "metric", "shares_outstanding", "total_debt",
    "cash_and_st", "beta", "growth_1_5", "discount_rate", "region",
    "statement_exchange_rate",
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

    Every money field below has a `_usd` twin, computed the same way but
    converted through each TRANSACTION's own `exchange_rate` -- the rate on
    the day that money actually moved, not today's. This is what makes a
    dollar spent in January at 7.80 HKD/USD stay a January dollar even if the
    rate has since moved to 7.85: the twin is a portfolio-wide, single-
    currency figure suitable for summing across holdings, while the
    unsuffixed field stays exactly what it always was -- the position in its
    own listed currency, unconverted. See migration 026's own description of
    exchange_rate ("what one USD was worth in it at the time") and issue #5
    of the calculation audit, which found the portfolio-wide totals summing
    these unconverted figures across holdings in different currencies.
    """

    ticker: str
    quantity: float = 0.0
    average_cost: Optional[float] = None
    average_cost_usd: Optional[float] = None
    cost_basis: float = 0.0
    cost_basis_usd: float = 0.0
    realized_pnl: float = 0.0
    realized_pnl_usd: float = 0.0
    dividends: float = 0.0
    dividends_usd: float = 0.0
    transaction_count: int = 0
    first_acquired: Optional[datetime] = None
    last_activity: Optional[datetime] = None


def _derive_position(ticker: str, rows: Sequence[InvestmentTransaction]) -> DerivedPosition:
    """Walk one ticker's transactions in time order into a position.

    A SELL relieves cost at the CURRENT average, which is what makes this
    average-cost rather than FIFO: the remaining basis per share is unchanged
    by a partial sale, so selling half a position does not silently reprice
    the half still held. The USD-converted running totals (see
    DerivedPosition's docstring) mirror this exactly, one exchange rate
    per transaction rather than one for the whole position -- a partial sale
    relieves USD cost at the USD average built from EACH purchase's own
    historical rate, not at today's rate applied retroactively to all of it.
    """
    quantity = 0.0
    cost = 0.0
    cost_usd = 0.0
    realized = 0.0
    realized_usd = 0.0
    dividends = 0.0
    dividends_usd = 0.0
    first_acquired: Optional[datetime] = None
    last_activity: Optional[datetime] = None

    for tx in sorted(rows, key=lambda r: (r.transaction_date, r.created_at or r.transaction_date)):
        kind = tx.transaction_type
        amount = _f(tx.total_amount) or 0.0
        # This transaction's OWN rate, from the day the money moved -- not
        # the holding's current one. Every accumulator below converts through
        # this, per-transaction, rather than converting the final total
        # through a single rate at the end, which is what makes a sale years
        # later still relieve basis at each purchase's own historical cost.
        rate = _f(tx.exchange_rate) or 1.0
        amount_usd = amount / rate
        qty = _f(tx.quantity) or 0.0
        last_activity = tx.transaction_date

        if kind in TX_ADDS_SHARES:
            quantity += qty
            # abs(): total_amount is negative for money leaving, and cost is
            # a magnitude. Taking it as-signed would make every purchase
            # reduce the basis.
            cost += abs(amount)
            cost_usd += abs(amount_usd)
            if first_acquired is None:
                first_acquired = tx.transaction_date
        elif kind == TX_SELL:
            average = cost / quantity if quantity > 0 else 0.0
            average_usd = cost_usd / quantity if quantity > 0 else 0.0
            relieved = average * qty
            relieved_usd = average_usd * qty
            realized += amount - relieved
            realized_usd += amount_usd - relieved_usd
            cost = max(0.0, cost - relieved)
            cost_usd = max(0.0, cost_usd - relieved_usd)
            quantity -= qty
            # A rounding residue on a full exit would otherwise leave a
            # basis attached to nothing.
            if quantity <= 1e-9:
                quantity = 0.0
                cost = 0.0
                cost_usd = 0.0
        elif kind == TX_DIVIDEND:
            dividends += amount
            dividends_usd += amount_usd
        elif kind == TX_ADJUSTMENT:
            # Signed deltas, not magnitudes -- the one place in this loop
            # that touches `cost` without abs(). No realized P&L and no
            # first_acquired effect: a correction is not a trade, it is a
            # restatement of where the running total already stood.
            quantity += qty
            cost += amount
            cost_usd += amount_usd
            if quantity <= 1e-9:
                quantity = 0.0
                cost = 0.0
                cost_usd = 0.0

    return DerivedPosition(
        ticker=ticker,
        quantity=quantity,
        average_cost=(cost / quantity) if quantity > 0 else None,
        average_cost_usd=(cost_usd / quantity) if quantity > 0 else None,
        cost_basis=cost,
        cost_basis_usd=cost_usd,
        realized_pnl=realized,
        realized_pnl_usd=realized_usd,
        dividends=dividends,
        dividends_usd=dividends_usd,
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
    # None clears the override and reverts to the auto-fetched quote; a
    # number sets it. Distinguished from "field not sent" the same way every
    # other nullable field here is, via exclude_unset in the handler below.
    manual_price: Optional[float] = Field(None, ge=0)

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
    patch = params.model_dump(exclude_unset=True)
    for field, value in patch.items():
        setattr(holding, field, value)
    # Stamped server-side rather than trusted from the client, and only when
    # the field was actually touched -- every other edit to the holding
    # (sector, allocation, ...) must not look like a fresh price correction.
    if "manual_price" in patch:
        holding.manual_price_at = (
            datetime.now(timezone.utc) if patch["manual_price"] is not None else None
        )
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
# Basis correction
# ---------------------------------------------------------------------------


class BasisCorrection(BaseModel):
    """Corrects quantity and/or average cost to an exact target by writing
    one ADJUSTMENT transaction for the delta -- see migration 028 for why
    this cannot be a raw override column the way price is.

    Both are the FINAL state the trader wants, not a delta: "this should read
    2.5 shares at $180", not "subtract 0.3 shares and $12". The endpoint
    computes the signed delta itself, against whatever the ledger derives to
    right now, so the caller never needs to know the current values first.
    """

    quantity: Optional[float] = Field(None, ge=0)
    average_cost: Optional[float] = Field(None, ge=0)
    note: Optional[str] = None

    @model_validator(mode="after")
    def _at_least_one(self):
        if self.quantity is None and self.average_cost is None:
            raise ValueError("Provide a quantity, an average cost, or both to correct.")
        return self


@app.post(
    "/api/investments/holdings/{ticker}/basis-correction",
    dependencies=[Depends(verify_clerk_token)],
)
async def correct_basis(
    ticker: str, params: BasisCorrection, session: AsyncSession = Depends(get_session)
):
    """Move the derived position to exactly the quantity and/or average cost
    supplied, by writing one ADJUSTMENT transaction for the difference.

    Dated now, not backdated -- this corrects the position from THIS point
    forward. A SELL that already happened keeps the realized P&L it was
    booked with; any SELL from here on derives its average from the
    corrected total, which is the entire reason this is a transaction and
    not a column overriding the display (see migration 028).
    """
    ticker = ticker.strip().upper()
    holding = await _get_holding(session, ticker)

    position = (await _derive_all_positions(session, [ticker])).get(ticker)
    if position is None or position.transaction_count == 0:
        raise HTTPException(
            status_code=409,
            detail=f"{ticker} has no transactions yet -- there is no position to correct.",
        )

    if params.quantity is not None and params.quantity <= 1e-9:
        # A correction to zero is a full exit: no average cost can attach to
        # no shares, regardless of what was also sent for it.
        target_quantity = 0.0
        target_cost_basis = 0.0
    else:
        target_quantity = (
            params.quantity if params.quantity is not None else position.quantity
        )
        target_avg = (
            params.average_cost if params.average_cost is not None
            else (position.average_cost or 0.0)
        )
        target_cost_basis = target_avg * target_quantity

    qty_delta = target_quantity - position.quantity
    cost_delta = target_cost_basis - position.cost_basis

    if abs(qty_delta) < 1e-9 and abs(cost_delta) < 1e-6:
        return {
            "applied": False,
            "detail": "Already matches the derived position; nothing to correct.",
            "quantity": position.quantity,
            "average_cost": position.average_cost,
            "cost_basis": position.cost_basis,
        }

    parts = []
    if abs(qty_delta) >= 1e-9:
        parts.append(f"quantity {position.quantity:g} -> {target_quantity:g}")
    if abs(cost_delta) >= 1e-6:
        old_avg = position.average_cost or 0.0
        new_avg = (target_cost_basis / target_quantity) if target_quantity > 0 else 0.0
        parts.append(f"avg cost {old_avg:.4f} -> {new_avg:.4f}")
    note = params.note or ("Manual correction: " + ", ".join(parts))

    session.add(
        InvestmentTransaction(
            ticker=ticker,
            transaction_type=TX_ADJUSTMENT,
            quantity=qty_delta,
            price=None,
            total_amount=cost_delta,
            fees=0,
            transaction_date=datetime.now(timezone.utc),
            listed_currency=holding.listed_currency,
            exchange_rate=holding.exchange_rate,
            source="MANUAL",
            note=note,
        )
    )
    await session.commit()

    corrected = (await _derive_all_positions(session, [ticker])).get(ticker)
    return {
        "applied": True,
        "quantity": corrected.quantity if corrected else target_quantity,
        "average_cost": corrected.average_cost if corrected else None,
        "cost_basis": corrected.cost_basis if corrected else target_cost_basis,
    }


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
            "flex_failures": _read_flex_failures(query_failures),
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
        "flex_failures": _read_flex_failures(query_failures),
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
    # 1 USD in the company's FILING currency -- see migration 037. Only
    # needed when that differs from USD; leaving it null falls back to the
    # fetched value (1.0 when the filer is USD, otherwise still null, which
    # is what makes the DCF decline to value rather than guess).
    statement_exchange_rate: Optional[float] = Field(None, gt=0)

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
        # Context only -- see VALUATION_FIELDS' comment on why this is not
        # part of the merge/override system.
        "statement_currency": row.statement_currency,
        "statement_exchange_rate": _f(row.statement_exchange_rate),
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


# How many quotes to ask FMP for at once.
#
# The refresh was strictly sequential, which at this book's size is 25 round
# trips end to end -- 5 to 12 seconds of somebody watching a spinner for work
# that has no ordering constraint whatsoever.
#
# Bounded rather than unbounded, for one specific reason. The 429 this can earn
# is documented in market_data._get as a spent DAILY allowance, and concurrency
# cannot change that: the same 25 calls get made either way. What a wide fan-out
# could newly trip is a per-second ceiling that 25 paced calls never approach.
# Four in flight is no more than a browser opens to a single host, and it is
# worth 3x here: 25 holdings at a 300ms call measure 8.2s sequentially against
# 2.7s at this limit. Four is also the ceiling on the speedup, which is the
# trade being made -- a larger number buys progressively less and risks more.
#
# This is NOT the throttle in services/growth.py. That one is sequential on
# purpose because Finviz blocks the IP -- a different provider with a different
# failure mode, and its comment says so.
PRICE_REFRESH_CONCURRENCY = 4


@app.post(
    "/api/investments/refresh-prices",
    dependencies=[Depends(verify_clerk_token)],
)
async def refresh_prices(session: AsyncSession = Depends(get_session)):
    """Latest quote for every holding. One FMP call each -- the daily path.

    Separate from the fundamentals refresh because the two go stale at
    completely different rates, and folding them together would spend four
    calls per holding to learn a price.

    Fetched concurrently, applied sequentially. The split is deliberate: an
    AsyncSession is not safe under concurrent use, so the ORM objects are
    touched only after every network call has landed, in holding order.
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

    limit = asyncio.Semaphore(PRICE_REFRESH_CONCURRENCY)
    # Mirrors `finviz_open` in services/growth.py. Once the provider says the
    # allowance is gone, every symbol still queued would earn an identical 429,
    # so they stop asking instead of each collecting the same failure. This is
    # what the `break` used to do, and the flag is how that intent survives the
    # calls no longer being sequential -- checked after acquiring the semaphore,
    # so at most PRICE_REFRESH_CONCURRENCY - 1 calls are already in flight when
    # the limit is discovered.
    allowance_open = True

    async def quote_for(ticker: str, client: httpx.AsyncClient):
        """A Quote, the error that explains its absence, or None for skipped.

        Never raises: one dead symbol must not cost the others their prices.
        """
        nonlocal allowance_open
        async with limit:
            if not allowance_open:
                return None
            try:
                return await market_data.fetch_quote(ticker, client=client)
            except market_data.MarketDataError as exc:
                if exc.status == 429:
                    allowance_open = False
                return exc

    async with httpx.AsyncClient(timeout=market_data.TIMEOUT) as client:
        # gather preserves input order, so zipping back onto `holdings` below
        # pairs every outcome with the symbol that produced it.
        outcomes = await asyncio.gather(
            *(quote_for(h.ticker, client) for h in holdings)
        )

    for holding, outcome in zip(holdings, outcomes):
        if outcome is None:
            # Never asked -- the allowance was already spent. Deliberately
            # counted as neither updated nor failed, exactly as the sequential
            # `break` left the symbols it never reached.
            continue
        if isinstance(outcome, market_data.MarketDataError):
            failures.append({"ticker": holding.ticker, "detail": str(outcome)})
            continue
        holding.current_price = outcome.price
        holding.price_updated_at = now
        # Only overwritten when the provider actually sent one, so a
        # response missing the field leaves the last known move in place
        # rather than blanking a populated column. day_change_updated_at is
        # stamped from the same `now` ONLY here, alongside it -- that is what
        # lets a reader tell a fresh day figure from a stale one later by
        # comparing it to price_updated_at (see migration 036).
        if outcome.day_change_pct is not None:
            holding.day_change_pct = outcome.day_change_pct
            holding.day_change_updated_at = now
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
                # See migration 037. Auto-fills to 1.0 only when the filer
                # itself reports in USD -- true for every holding in this
                # book today (GOOGL, MSFT, META, NVDA, AMZN, UNH all file in
                # USD). Left NULL otherwise: there is no FX provider to fetch
                # a real rate from, so a trader supplying one by hand is
                # required, not assumed.
                "statement_currency": fundamentals.currency,
                "statement_exchange_rate": (
                    1.0 if (fundamentals.currency or "USD").upper() == "USD" else None
                ),
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


def _effective_price(holding: InvestmentHolding) -> Optional[float]:
    """The price every calculation should use: the manual override when the
    auto-fetched quote is wrong, otherwise whatever refresh_prices last saw.

    `current_price` itself is untouched by an override -- refresh_prices keeps
    overwriting it wholesale, so the auto baseline is never lost underneath a
    correction and "reset to auto" costs nothing to fetch.
    """
    return _f(holding.manual_price) if holding.manual_price is not None else _f(holding.current_price)


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
        # The number every downstream calculation (market value, unrealized
        # P&L, DCF premium) actually uses -- see _effective_price.
        "current_price": _effective_price(holding),
        "price_updated_at": holding.price_updated_at,
        "auto_price": _f(holding.current_price),
        "manual_price": _f(holding.manual_price),
        "manual_price_at": holding.manual_price_at,
        "price_is_manual": holding.manual_price is not None,
        # As of day_change_updated_at, which can lag price_updated_at when a
        # later refresh's response omitted the day change -- see migration
        # 036 and AllocationPanel.tsx's isDayChangeStale.
        "day_change_pct": _f(holding.day_change_pct),
        "day_change_updated_at": holding.day_change_updated_at,
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
    # Treated exactly like a missing base_flow or shares_outstanding, not
    # defaulted to 1.0 -- see migration 037. A holding whose filer reports
    # in USD gets this from refresh_valuations automatically; anything else
    # needs it supplied by hand before the DCF will run at all, because
    # guessing 1.0 here is the bug this column exists to stop.
    statement_rate = merged.get("statement_exchange_rate")
    if base_flow is None or not shares or growth is None or not statement_rate:
        missing = [
            name for name, value in (
                ("base_flow", base_flow),
                ("shares_outstanding", shares),
                ("growth_1_5", growth),
                ("statement_exchange_rate", statement_rate),
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
        statement_exchange_rate=float(statement_rate),
        discount_rate_override=_f(merged.get("discount_rate")),
    )
    result = valuation_engine.value(inputs)
    price = _effective_price(holding)

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


def _value_position_usd(
    market_value: Optional[float], exchange_rate: Optional[float], position: DerivedPosition
) -> dict:
    """market_value_usd and what it does to unrealised P&L, for one holding.

    Split out from get_portfolio so this conversion has its own test
    independent of a database session -- the exact reasoning
    _value_holding/_derive_position already follow elsewhere in this file.

    Uses the HOLDING's current exchange_rate, not any transaction's
    historical one: market value is what the position is worth today, so
    today's rate is what converts it. cost_basis_usd on `position` instead
    used each transaction's own historical rate (see _derive_position),
    because it is money that already moved on a specific day in the past --
    the two intentionally use different rates for different reasons.
    """
    rate = exchange_rate or 1.0
    market_value_usd = market_value / rate if market_value is not None else None
    unrealized_pnl_usd = (
        market_value_usd - position.cost_basis_usd if market_value_usd is not None else None
    )
    unrealized_pnl_pct_usd = (
        (market_value_usd / position.cost_basis_usd - 1.0) * 100.0
        if market_value_usd is not None and position.cost_basis_usd > 0 else None
    )
    return {
        "market_value_usd": market_value_usd,
        "unrealized_pnl_usd": unrealized_pnl_usd,
        "unrealized_pnl_pct_usd": unrealized_pnl_pct_usd,
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
    # USD, always -- see DerivedPosition's docstring. Every total below, and
    # portfolio_weight_pct, is built from the `_usd` twin of each figure
    # specifically so a HKD holding's weight is not computed by dividing a
    # HKD number into a sum of HKD-and-USD-and-EUR numbers added together as
    # if they were the same currency (issue #5 of the calculation audit).
    total_market_value = 0.0

    for holding in holdings:
        position = positions.get(holding.ticker) or DerivedPosition(ticker=holding.ticker)
        price = _effective_price(holding)
        # Nothing held is not the same as held and worth zero. A watchlist
        # entry, or a position fully exited, has no market value and no
        # unrealised P&L -- and rendering those as 0.00 and +0.00 would put a
        # green gain of nothing against every ticker being tracked but not
        # owned.
        market_value = (price * position.quantity) if price and position.quantity else None
        usd = _value_position_usd(market_value, _f(holding.exchange_rate), position)
        market_value_usd = usd["market_value_usd"]
        if market_value_usd:
            total_market_value += market_value_usd

        variants = inputs_by_ticker.get(holding.ticker, {})
        merged, overridden = _merge_inputs(
            variants.get(VARIANT_AUTO), variants.get(VARIANT_OVERRIDE)
        )

        rows.append({
            **_holding_row(holding),
            "quantity": position.quantity,
            "average_cost": position.average_cost,
            "cost_basis": position.cost_basis,
            "cost_basis_usd": position.cost_basis_usd,
            "market_value": market_value,
            "market_value_usd": market_value_usd,
            "unrealized_pnl": (
                market_value - position.cost_basis if market_value is not None else None
            ),
            "unrealized_pnl_pct": (
                (market_value / position.cost_basis - 1.0) * 100.0
                if market_value is not None and position.cost_basis > 0 else None
            ),
            "unrealized_pnl_usd": usd["unrealized_pnl_usd"],
            "unrealized_pnl_pct_usd": usd["unrealized_pnl_pct_usd"],
            "realized_pnl": position.realized_pnl,
            "realized_pnl_usd": position.realized_pnl_usd,
            "dividends": position.dividends,
            "dividends_usd": position.dividends_usd,
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
        if row["market_value_usd"] and total_market_value > 0:
            row["portfolio_weight_pct"] = row["market_value_usd"] / total_market_value * 100.0

    return {
        "holdings": rows,
        "total_market_value": total_market_value,
        "total_cost_basis": sum(r["cost_basis_usd"] for r in rows),
        "total_unrealized_pnl": sum(
            r["unrealized_pnl_usd"] for r in rows if r["unrealized_pnl_usd"] is not None
        ),
        "total_realized_pnl": sum(r["realized_pnl_usd"] for r in rows),
        "total_dividends": sum(r["dividends_usd"] for r in rows),
        "as_of": datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------------------
# THE CSV EXPORTS
# ---------------------------------------------------------------------------
#
# A THIRD region of this file, belonging to neither book. Everything above the
# "THE INVESTMENT BOOK" marker is the journal and everything below it is the
# investment book, and test_investments.py enforces in both directions that
# they never reference each other -- that separation is what lets one of them
# break without taking the other with it.
#
# This section reads both on purpose, which is why it sits outside both rather
# than inside either. It is READ-ONLY and additive: it defines no table, writes
# nothing, and no endpoint in either book calls into it. Nothing here may ever
# be imported the other way, or the separation those tests protect would be
# routed around by way of an export.
#
# Keep this LAST in the file. The guardrail tests treat its marker as the end
# of the investment section, so anything appended after it escapes the check.
#
# Two grains per book, because they answer different questions and neither
# substitutes for the other.
#
# The ANALYSIS grain is the unit each half of the app is about -- a round trip,
# a holding -- carrying the derived figures the pages show. That is the file to
# pivot in a spreadsheet.
#
# The LEDGER grain is the rows as stored: every fill, every transaction. Those
# are the records that cannot be recomputed from anything else if the database
# is lost, and they carry no derived column at all. Supabase's free tier takes
# no automated backups, which is most of why this exists.
#
# No cycle to break here -- csv_export imports nothing from this module -- so
# this follows the `valuation_engine` precedent above rather than the lazy
# imports the analytics module needs.
from services import csv_export as fmt  # noqa: E402


class ExportDataset(str, Enum):
    """The four exports. A str Enum so FastAPI validates the path itself."""

    ROUND_TRIPS = "round-trips"
    EXECUTIONS = "executions"
    INVESTMENT_HOLDINGS = "investment-holdings"
    INVESTMENT_TRANSACTIONS = "investment-transactions"


def _headerless_request() -> Request:
    """A Request carrying no headers, for calling an endpoint from inside one.

    `list_round_trips` honours If-None-Match. Handing it the browser's real
    request would let a validator the browser holds for the JOURNAL turn this
    export into a 304, and a 304 rendered as CSV is a header row with no trades
    under it -- indistinguishable from an account that never traded. With no
    headers there is nothing to match against.
    """
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/round-trips",
            "headers": [],
            "query_string": b"",
        }
    )


async def _strategy_names(session: AsyncSession) -> dict[uuid.UUID, str]:
    """id -> name, so an export names the playbook entry a human recognises.

    Column-only select: two fields are all that is wanted, and loading whole
    Strategy entities to read `name` off them is the pattern the analytics
    loaders were deliberately narrowed away from.
    """
    result = await session.execute(select(Strategy.id, Strategy.name))
    return {row.id: row.name for row in result}


async def _export_round_trips(session: AsyncSession):
    """The journal at round-trip grain: plan, outcome, score and review.

    Calls `list_round_trips` rather than querying for itself. Open-exposure
    reconstruction and R scoring both live in there, and a second copy is how an
    export comes to disagree with the page it claims to export -- the same
    reason `_score_r` delegates to analytics instead of restating the formula.

    Figures arrive as floats because RoundTripOut declares them that way, so
    this file agrees with the journal to the digit rather than with the database.
    The ledger-grain exports read their columns directly and keep full NUMERIC
    precision; between the two nothing is lost.

    Open positions are included. They have no exit, no P&L and no review, and
    every one of those columns is empty for them -- but dropping them would make
    the export the only surface in the app that pretends live exposure is not
    there, which is the bug `/api/round-trips` was built to fix.
    """
    from services.analytics import MARKET_TZ  # noqa: PLC0415 - avoids a cycle

    rows = await list_round_trips(
        request=_headerless_request(),
        response=Response(),
        limit=None,
        offset=0,
        session=session,
    )
    names = await _strategy_names(session)

    header = [
        "kind", "symbol", "direction", "quantity",
        "entry_time", "exit_time", "exit_date",
        "entry_price", "exit_price",
        "realized_pnl", "gross_pnl", "commission",
        "r_multiple", "planned_r_multiple",
        "planned_entry", "stop_loss", "actual_stop_loss", "target",
        "risk_percent", "risk_amount", "conviction",
        "strategy", "emotional_state", "thesis",
        "execution_count", "entry_slippage",
        "has_hand_added_fills", "from_plan",
        "review_status", "trade_grade", "exit_reason", "mistakes",
        "notes", "review_went_well", "review_went_wrong", "review_lessons",
        "ideal_entry", "ideal_stop", "ideal_target",
        "revised_entry", "revised_stop", "revised_target",
        "position_id",
    ]

    out = []
    for r in rows:
        # Market-time close date, matching how the dashboard buckets days. A UTC
        # date would file a late-session close on the far side of midnight from
        # the heatmap cell it belongs to.
        exit_day = r.exit_time.astimezone(MARKET_TZ).date() if r.exit_time else None
        out.append([
            fmt.text(r.kind),
            fmt.text(r.symbol),
            fmt.text(r.direction),
            fmt.number(r.quantity),
            fmt.timestamp(r.entry_time),
            fmt.timestamp(r.exit_time),
            fmt.day(exit_day),
            fmt.number(r.entry_price),
            fmt.number(r.exit_price),
            fmt.number(r.realized_pnl),
            fmt.number(r.gross_pnl),
            fmt.number(r.commission),
            fmt.number(r.r_multiple),
            fmt.number(r.planned_r_multiple),
            fmt.number(r.planned_entry),
            fmt.number(r.stop_loss),
            fmt.number(r.actual_stop_loss),
            fmt.number(r.target),
            fmt.number(r.risk_percent),
            fmt.number(r.risk_amount),
            fmt.number(r.conviction),
            fmt.text(names.get(r.strategy_id, "")),
            fmt.text(r.emotional_state),
            fmt.text(r.thesis),
            fmt.number(r.execution_count),
            fmt.number(r.entry_slippage),
            fmt.flag(r.has_hand_added_fills),
            fmt.flag(r.plan_id is not None),
            fmt.text(r.review_status),
            fmt.text(r.trade_grade),
            fmt.text(r.exit_reason),
            fmt.joined(r.mistakes),
            fmt.text(r.notes),
            fmt.text(r.review_went_well),
            fmt.text(r.review_went_wrong),
            fmt.text(r.review_lessons),
            fmt.number(r.ideal_entry),
            fmt.number(r.ideal_stop),
            fmt.number(r.ideal_target),
            fmt.number(r.revised_entry),
            fmt.number(r.revised_stop),
            fmt.number(r.revised_target),
            fmt.text(r.position_id or ""),
        ])
    return header, out


async def _export_executions(session: AsyncSession):
    """Every execution as stored: the ledger everything else is derived from.

    Column-only select, and every scalar column on `trades` except
    `broker_original` -- the JSONB snapshot of what the broker said before the
    first hand edit. It is left out because a JSON document inside a CSV cell is
    how a CSV stops being readable by the thing it was exported for. It stays in
    the database, and `edited_at` beside it is what tells you a row has one.

    Decimals are written at the scale the columns hold, so this file reproduces
    the ledger exactly rather than to float precision.
    """
    result = await session.execute(
        select(
            Trade.entry_date, Trade.ticker, Trade.direction, Trade.quantity,
            Trade.actual_entry, Trade.exit_date, Trade.exit_price,
            Trade.commission, Trade.broker_realized_pnl, Trade.broker_cost_basis,
            Trade.style, Trade.source_tag, Trade.ibkr_exec_id,
            Trade.strategy_id, Trade.planned_entry, Trade.stop_loss,
            Trade.actual_stop_loss, Trade.target, Trade.risk_percent,
            Trade.risk_amount, Trade.conviction, Trade.emotional_state,
            Trade.thesis, Trade.grade, Trade.market_regime,
            Trade.hard_sl_set, Trade.waited_retest, Trade.followed_plan,
            Trade.screenshot_url, Trade.edited_at, Trade.created_at,
            Trade.id, Trade.plan_id,
        )
        # id breaks the tie so two fills stamped the same second export in a
        # stable order -- a diff between two exports should show what changed,
        # not how the database felt about ordering that day.
        .order_by(Trade.entry_date, Trade.id)
    )
    names = await _strategy_names(session)

    header = [
        "executed_at", "ticker", "direction", "quantity", "price",
        "exit_date", "exit_price",
        "commission", "broker_realized_pnl", "broker_cost_basis",
        "style", "source", "ibkr_exec_id", "strategy",
        "planned_entry", "stop_loss", "actual_stop_loss", "target",
        "risk_percent", "risk_amount", "conviction", "emotional_state",
        "thesis", "grade", "market_regime",
        "hard_sl_set", "waited_retest", "followed_plan",
        "screenshot_url", "edited_at", "created_at",
        "trade_id", "plan_id",
    ]

    out = [
        [
            fmt.timestamp(t.entry_date),
            fmt.text(t.ticker),
            fmt.text(t.direction),
            fmt.number(t.quantity),
            fmt.number(t.actual_entry),
            fmt.timestamp(t.exit_date),
            fmt.number(t.exit_price),
            fmt.number(t.commission),
            fmt.number(t.broker_realized_pnl),
            fmt.number(t.broker_cost_basis),
            fmt.text(t.style),
            fmt.text(t.source_tag),
            fmt.text(t.ibkr_exec_id),
            fmt.text(names.get(t.strategy_id, "")),
            fmt.number(t.planned_entry),
            fmt.number(t.stop_loss),
            fmt.number(t.actual_stop_loss),
            fmt.number(t.target),
            fmt.number(t.risk_percent),
            fmt.number(t.risk_amount),
            fmt.number(t.conviction),
            fmt.text(t.emotional_state),
            fmt.text(t.thesis),
            fmt.text(t.grade),
            fmt.text(t.market_regime),
            fmt.flag(t.hard_sl_set),
            fmt.flag(t.waited_retest),
            fmt.flag(t.followed_plan),
            fmt.text(t.screenshot_url),
            fmt.timestamp(t.edited_at),
            fmt.timestamp(t.created_at),
            fmt.text(t.id),
            fmt.text(t.plan_id or ""),
        ]
        for t in result
    ]
    return header, out


async def _export_investment_holdings(session: AsyncSession):
    """The investment book at holding grain, as the portfolio page computes it.

    Calls `get_portfolio` for the same reason the round-trip export calls
    `list_round_trips`: portfolio weight cannot be computed for one row without
    the total across every other one, and the valuation inputs exist in two
    variants that combine only on read. Reimplementing either here would be a
    second answer to a question the app has already answered once.
    """
    payload = await get_portfolio(session=session)

    header = [
        "ticker", "name", "sector", "category", "holding_type", "country",
        "listed_currency", "exchange_rate",
        "quantity", "average_cost", "cost_basis", "cost_basis_usd",
        "current_price", "price_is_manual", "price_updated_at",
        "day_change_pct",
        "market_value", "market_value_usd",
        "unrealized_pnl", "unrealized_pnl_pct",
        "unrealized_pnl_usd", "unrealized_pnl_pct_usd",
        "realized_pnl", "realized_pnl_usd",
        "dividends", "dividends_usd", "portfolio_weight_pct",
        "planned_allocation", "transaction_count", "first_acquired",
        "is_valuable", "valuation_available",
        "intrinsic_value_base", "intrinsic_value_conservative",
        "intrinsic_value_average", "premium_pct", "discount_rate",
    ]

    out = []
    for row in payload["holdings"]:
        # Absent for an ETF (is_valuable False) and for anything missing a DCF
        # input. An empty valuation column means the model had nothing to say,
        # which is not the same as a value of zero -- see _value_holding.
        valuation = row.get("valuation") or {}
        base = valuation.get("base") or {}
        conservative = valuation.get("conservative") or {}
        out.append([
            fmt.text(row["ticker"]),
            fmt.text(row["name"]),
            fmt.text(row["sector"]),
            fmt.text(row["category"]),
            fmt.text(row["holding_type"]),
            fmt.text(row["country"]),
            fmt.text(row["listed_currency"]),
            fmt.number(row["exchange_rate"]),
            fmt.number(row["quantity"]),
            fmt.number(row["average_cost"]),
            fmt.number(row["cost_basis"]),
            fmt.number(row["cost_basis_usd"]),
            fmt.number(row["current_price"]),
            fmt.flag(row["price_is_manual"]),
            fmt.timestamp(row["price_updated_at"]),
            fmt.number(row["day_change_pct"]),
            fmt.number(row["market_value"]),
            fmt.number(row["market_value_usd"]),
            fmt.number(row["unrealized_pnl"]),
            fmt.number(row["unrealized_pnl_pct"]),
            fmt.number(row["unrealized_pnl_usd"]),
            fmt.number(row["unrealized_pnl_pct_usd"]),
            fmt.number(row["realized_pnl"]),
            fmt.number(row["realized_pnl_usd"]),
            fmt.number(row["dividends"]),
            fmt.number(row["dividends_usd"]),
            fmt.number(row["portfolio_weight_pct"]),
            fmt.number(row["planned_allocation"]),
            fmt.number(row["transaction_count"]),
            fmt.timestamp(row["first_acquired"]),
            fmt.flag(row["is_valuable"]),
            fmt.flag(valuation.get("available")),
            fmt.number(base.get("intrinsic_value")),
            fmt.number(conservative.get("intrinsic_value")),
            fmt.number(valuation.get("average_intrinsic_value")),
            fmt.number(valuation.get("premium_pct")),
            fmt.number(valuation.get("discount_rate")),
        ])
    return header, out


async def _export_investment_transactions(session: AsyncSession):
    """Every investment transaction as stored: the book's own ledger.

    The whole investment side is derived from these rows -- quantity, average
    cost, realised P&L and dividends are all replayed from them on every read
    (see `_derive_position`). Nothing else in the app can reconstruct them.
    """
    result = await session.execute(
        select(
            InvestmentTransaction.transaction_date,
            InvestmentTransaction.ticker,
            InvestmentTransaction.transaction_type,
            InvestmentTransaction.quantity,
            InvestmentTransaction.price,
            InvestmentTransaction.total_amount,
            InvestmentTransaction.fees,
            InvestmentTransaction.listed_currency,
            InvestmentTransaction.exchange_rate,
            InvestmentTransaction.source,
            InvestmentTransaction.external_id,
            InvestmentTransaction.note,
            InvestmentTransaction.created_at,
            InvestmentTransaction.id,
        ).order_by(InvestmentTransaction.transaction_date, InvestmentTransaction.id)
    )

    header = [
        "transaction_date", "ticker", "transaction_type", "quantity", "price",
        "total_amount", "fees", "listed_currency", "exchange_rate",
        "source", "external_id", "note", "created_at", "transaction_id",
    ]

    out = [
        [
            fmt.timestamp(t.transaction_date),
            fmt.text(t.ticker),
            fmt.text(t.transaction_type),
            fmt.number(t.quantity),
            fmt.number(t.price),
            fmt.number(t.total_amount),
            fmt.number(t.fees),
            fmt.text(t.listed_currency),
            fmt.number(t.exchange_rate),
            fmt.text(t.source),
            fmt.text(t.external_id),
            fmt.text(t.note),
            fmt.timestamp(t.created_at),
            fmt.text(t.id),
        ]
        for t in result
    ]
    return header, out


# One endpoint over a registry rather than four near-identical endpoints: the
# auth dependency, the response shape and the filename logic are the same for
# all of them, so a fifth export should be a line here instead of another copy.
_EXPORTS = {
    ExportDataset.ROUND_TRIPS: (_export_round_trips, "trading-round-trips"),
    ExportDataset.EXECUTIONS: (_export_executions, "trading-executions"),
    ExportDataset.INVESTMENT_HOLDINGS: (
        _export_investment_holdings, "investment-holdings",
    ),
    ExportDataset.INVESTMENT_TRANSACTIONS: (
        _export_investment_transactions, "investment-transactions",
    ),
}


@app.get(
    "/api/export/{dataset}.csv",
    dependencies=[Depends(verify_clerk_token)],
    responses={200: {"content": {"text/csv": {}}}},
)
async def export_csv(
    dataset: ExportDataset,
    session: AsyncSession = Depends(get_session),
):
    """Download one dataset as CSV.

    Deliberately NOT ETagged and NOT cacheable. Every other read endpoint here
    revalidates against `data_version`, and that is right for a page which
    should show what is current. An export is different: it is a snapshot
    someone asked for by name, and a cache handing back a copy from earlier
    would produce a file that silently predates the data it claims to hold --
    the one failure a backup must not have.
    """
    from services.analytics import market_today  # noqa: PLC0415 - avoids a cycle

    build, stem = _EXPORTS[dataset]
    header, rows = await build(session)

    # Market date, not the server's. An export taken at breakfast in Singapore
    # is still the previous session in New York, and naming it with the UTC date
    # would file it a day ahead of the trades inside it.
    filename = f"{stem}-{market_today().isoformat()}.csv"

    return Response(
        content=fmt.render(header, rows).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
