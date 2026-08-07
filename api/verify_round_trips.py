"""Check the rewritten /api/round-trips queries against the logic they replaced.

READ ONLY. Opens one transaction, runs SELECTs, and rolls back. It cannot write
and does not try to.

WHY THIS EXISTS. The endpoint used to load every position, every execution and
every fill and sort it out in Python. It now asks Postgres the same questions
directly, which is a large change to the most important read in the app -- and
one no test in CI can check, because CI has no database.

So the old implementation is kept here, in full, and both are run against the
same live data and compared. Agreement on a real ledger, with its real
oversells and partial fills and hand-added repairs, is worth more than any
fixture: the cases that would break this are exactly the ones nobody thinks to
invent.

    python verify_round_trips.py

Exits 0 when they agree, 1 when they do not, naming the executions involved.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

from sqlalchemy import func, literal, select

import main
from main import Position, PositionFill, Trade


# ---------------------------------------------------------------------------
# The old way, preserved verbatim in behaviour
# ---------------------------------------------------------------------------


async def open_exposure_the_old_way(session) -> dict:
    """Every execution FIFO never fully paired off, found by loading the lot.

    This is what the endpoint did before: pull all trades, pull all fills, sum
    the fills per trade in Python, and keep the trades with quantity left over.
    """
    trades = (await session.execute(select(Trade))).scalars().all()
    fills = (await session.execute(select(PositionFill))).scalars().all()

    consumed: dict = {}
    for fill in fills:
        consumed[fill.trade_id] = consumed.get(fill.trade_id, Decimal("0")) + (
            fill.quantity or Decimal("0")
        )

    out = {}
    for trade in trades:
        remaining = main._unmatched_quantity(trade, consumed)
        if remaining > 0:
            out[trade.id] = remaining
    return out


# ---------------------------------------------------------------------------
# The new way, as the endpoint now does it
# ---------------------------------------------------------------------------


async def open_exposure_the_new_way(session) -> dict:
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
    stmt = (
        select(Trade.id, remaining_expr.label("remaining"))
        .outerjoin(consumed_sq, consumed_sq.c.trade_id == Trade.id)
        .where(remaining_expr > 0)
        .order_by(Trade.entry_date)
    )
    return {
        row[0]: Decimal(str(row[1])) for row in (await session.execute(stmt)).all()
    }


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------


async def compare_open_exposure(session) -> list[str]:
    old = await open_exposure_the_old_way(session)
    new = await open_exposure_the_new_way(session)

    problems = []
    only_old = old.keys() - new.keys()
    only_new = new.keys() - old.keys()

    for trade_id in sorted(only_old, key=str):
        problems.append(
            f"  the rewrite LOST open exposure: {trade_id} "
            f"({old[trade_id]} shares the old code reported)"
        )
    for trade_id in sorted(only_new, key=str):
        problems.append(
            f"  the rewrite INVENTED open exposure: {trade_id} "
            f"({new[trade_id]} shares)"
        )
    for trade_id in sorted(old.keys() & new.keys(), key=str):
        if old[trade_id] != new[trade_id]:
            problems.append(
                f"  quantity disagrees for {trade_id}: "
                f"old {old[trade_id]}, new {new[trade_id]}"
            )

    print(
        f"open exposure: {len(old)} execution(s) old, {len(new)} new, "
        f"{len(problems)} disagreement(s)"
    )
    return problems


async def compare_paging(session, page_size: int = 7) -> list[str]:
    """Walking the closed half in pages must visit exactly the unpaged list.

    Page size is deliberately small and odd, so the last page is partial and
    the boundaries do not line up with anything round.
    """
    ordered = select(Position.id).order_by(
        Position.exit_time.desc(), Position.id.desc()
    )
    unpaged = [row[0] for row in (await session.execute(ordered)).all()]

    walked = []
    offset = 0
    while True:
        page = [
            row[0]
            for row in (
                await session.execute(ordered.limit(page_size).offset(offset))
            ).all()
        ]
        if not page:
            break
        walked.extend(page)
        offset += page_size
        if offset > len(unpaged) + page_size:
            break  # runaway guard; the mismatch below will report it

    problems = []
    if walked != unpaged:
        problems.append(
            f"  paging visited {len(walked)} rows, unpaged has {len(unpaged)}"
        )
        duplicated = len(walked) - len(set(walked))
        if duplicated:
            problems.append(f"  {duplicated} row(s) appeared on more than one page")
        missed = set(unpaged) - set(walked)
        if missed:
            problems.append(f"  {len(missed)} row(s) were never returned by any page")

    print(
        f"paging: {len(unpaged)} closed round trip(s), walked in pages of "
        f"{page_size}, {len(problems)} disagreement(s)"
    )
    return problems


async def compare_ticker_filter(session) -> list[str]:
    """Filtering in SQL must select what filtering in Python selected."""
    symbols = [
        row[0]
        for row in (await session.execute(select(Position.symbol).distinct())).all()
    ]
    problems = []
    for symbol in sorted(s for s in symbols if s):
        in_sql = {
            row[0]
            for row in (
                await session.execute(
                    select(Position.id).where(Position.symbol == symbol)
                )
            ).all()
        }
        in_python = {
            row[0]
            for row in (await session.execute(select(Position.id, Position.symbol))).all()
            if row[1] == symbol
        }
        if in_sql != in_python:
            problems.append(f"  ticker {symbol}: SQL and Python select different rows")

    print(f"ticker filter: {len(symbols)} symbol(s), {len(problems)} disagreement(s)")
    return problems


async def run() -> int:
    async with main.engine.connect() as conn:
        outer = await conn.begin()
        try:
            session = main.AsyncSession(bind=conn, expire_on_commit=False)
            problems = []
            problems += await compare_open_exposure(session)
            problems += await compare_paging(session)
            problems += await compare_ticker_filter(session)
        finally:
            # Nothing above writes; this makes that structural rather than a
            # promise, so the script stays safe to point at production.
            await outer.rollback()
            await main.engine.dispose()

    print()
    if problems:
        print(f"{len(problems)} DISAGREEMENT(S):")
        for line in problems:
            print(line)
        print("\nThe rewrite does not match the logic it replaced. Do not merge.")
        return 1

    print("The rewritten queries agree with the logic they replaced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
