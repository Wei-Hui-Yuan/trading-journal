"""Reconciliation check: does `positions` still agree with FIFO?

Read-only. Re-runs matching for every ticker with persist=False and compares
what is stored against what FIFO produces -- both which round trips exist, and
what each of them is worth.

Run it after any bulk import, or whenever a headline figure looks wrong:

    cd api && python reconcile_positions.py

A clean run prints zeros. Anything else means `positions` and the trade ledger
disagree, and the reported net P&L, win rate, trade count and equity curve are
all computed from the stale side.

Historically this could happen silently. `run_matching_for_ticker` only ever
appended, guarded by ON CONFLICT on the open/close pair -- which holds only
while re-running produces the same pairs, and a fill arriving dated earlier
than ones already stored re-partitions the queue so it does not. That path is
now authoritative and deletes what it no longer produces, so this script
should stay quiet. It is kept because "should" is not "does", and the failure
it detects is invisible from the UI.

The value check was added later, and it was overdue: for the whole life of the
journal every stored realized_pnl was GROSS of commission while FIFO would
have computed it net, and a check that only compared execution pairs reported
CLEAN throughout. Agreeing on which round trips exist says nothing about
whether they are worth what the table claims.
"""

import asyncio
import sys
from decimal import Decimal

sys.path.insert(0, __import__("os").path.dirname(__file__) or ".")

import main  # noqa: E402
from services.matching_engine import run_matching_for_ticker  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402


async def reconcile() -> int:
    """Returns the number of discrepancies found, for use as an exit code."""
    async with main.engine.connect() as conn:
        # Everything runs inside a transaction that is never committed, so a
        # reconciliation check can never itself change what it is checking.
        transaction = await conn.begin()
        Session = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            async with Session() as session:
                tickers = sorted(
                    {
                        ticker
                        for (ticker,) in (
                            await session.execute(select(main.Trade.ticker).distinct())
                        ).all()
                    }
                )
                print(f"reconciling {len(tickers)} ticker(s)\n")

                stale_total = 0
                missing_total = 0
                drift_total = 0
                stale_pnl = Decimal("0")
                drift_pnl = Decimal("0")

                for ticker in tickers:
                    stored = (
                        await session.execute(
                            select(
                                main.Position.id,
                                main.Position.open_trade_id,
                                main.Position.close_trade_id,
                                main.Position.realized_pnl,
                                main.Position.gross_pnl,
                                main.Position.commission,
                                main.Position.review_status,
                            ).where(main.Position.symbol == ticker)
                        )
                    ).all()

                    result = await run_matching_for_ticker(session, ticker, persist=False)
                    fresh = {(p.open_trade_id, p.close_trade_id) for p in result.positions}
                    stored_pairs = {(r.open_trade_id, r.close_trade_id) for r in stored}

                    stale = [
                        r for r in stored
                        if (r.open_trade_id, r.close_trade_id) not in fresh
                    ]
                    missing = fresh - stored_pairs

                    # Matching the pairs is not the same as matching the money.
                    # A stored row can name the right two executions and still
                    # hold a figure computed under different rules -- which is
                    # exactly what every position did before migration 020,
                    # when realized_pnl was gross and this check said CLEAN.
                    fresh_by_pair = {
                        (p.open_trade_id, p.close_trade_id): p for p in result.positions
                    }
                    drifted = []
                    for row in stored:
                        pair = (row.open_trade_id, row.close_trade_id)
                        expected = fresh_by_pair.get(pair)
                        if expected is None:
                            continue
                        if (row.realized_pnl or Decimal("0")) != expected.realized_pnl:
                            drifted.append((row, expected))

                    if stale or missing or drifted:
                        print(f"  {ticker}")
                    for row in stale:
                        stale_total += 1
                        stale_pnl += row.realized_pnl or Decimal("0")
                        print(
                            f"     STALE   pnl={row.realized_pnl:+} "
                            f"review={row.review_status} id={row.id}"
                        )
                    for pair in missing:
                        missing_total += 1
                        print(f"     MISSING round trip for pair {pair}")
                    for row, expected in drifted:
                        drift_total += 1
                        drift_pnl += expected.realized_pnl - (
                            row.realized_pnl or Decimal("0")
                        )
                        print(
                            f"     DRIFT   stored={row.realized_pnl:+} "
                            f"fifo={expected.realized_pnl:+} id={row.id}"
                        )

                reported = sum(
                    (await session.execute(select(main.Position.realized_pnl)))
                    .scalars()
                    .all()
                )

                print()
                print("=" * 60)
                print(f"stale positions (in the table, not in FIFO) : {stale_total}")
                print(f"missing positions (in FIFO, not the table)  : {missing_total}")
                print(f"positions whose stored P&L != FIFO's        : {drift_total}")
                print(f"P&L attributable to stale rows              : {stale_pnl:+}")
                if drift_total:
                    print(f"P&L the drifted rows are wrong by           : {drift_pnl:+}")
                print(f"reported net P&L                            : {reported:+}")
                if stale_total:
                    print(f"net P&L with stale rows excluded            : {reported - stale_pnl:+}")
                print()
                clean = not (stale_total or missing_total or drift_total)
                print("CLEAN" if clean else "DISCREPANCIES FOUND")
                return stale_total + missing_total + drift_total
        finally:
            await transaction.rollback()
            await main.engine.dispose()


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(reconcile()) else 0)
