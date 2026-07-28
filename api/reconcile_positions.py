"""Reconciliation check: does `positions` still agree with FIFO?

Read-only. Re-runs matching for every ticker with persist=False and compares
the (open_trade_id, close_trade_id) pairs against what is actually stored.

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
                stale_pnl = Decimal("0")

                for ticker in tickers:
                    stored = (
                        await session.execute(
                            select(
                                main.Position.id,
                                main.Position.open_trade_id,
                                main.Position.close_trade_id,
                                main.Position.realized_pnl,
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

                    if stale or missing:
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

                reported = sum(
                    (await session.execute(select(main.Position.realized_pnl)))
                    .scalars()
                    .all()
                )

                print()
                print("=" * 60)
                print(f"stale positions (in the table, not in FIFO) : {stale_total}")
                print(f"missing positions (in FIFO, not the table)  : {missing_total}")
                print(f"P&L attributable to stale rows              : {stale_pnl:+}")
                print(f"reported net P&L                            : {reported:+}")
                if stale_total:
                    print(f"net P&L with stale rows excluded            : {reported - stale_pnl:+}")
                print()
                print("CLEAN" if not (stale_total or missing_total) else "DISCREPANCIES FOUND")
                return stale_total + missing_total
        finally:
            await transaction.rollback()
            await main.engine.dispose()


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(reconcile()) else 0)
