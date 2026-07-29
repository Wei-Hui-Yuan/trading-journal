"""Does incremental matching land where a clean rebuild would?

    cd api && python verify_convergence.py

Every C1 symptom was one shape: stored state disagreeing with what FIFO would
produce from the fills currently in the ledger. A round trip that dissolved but
stayed in the table. One that re-paired and got counted twice. One whose
quantity refreshed while its fills did not. Rather than test each symptom, this
tests the property all of them violate.

For each scenario the fills are applied ONE AT A TIME, re-matching after every
one -- which is what the sync actually does, and the only reason any of those
bugs existed. Then the ticker's positions are deleted and matched once from
scratch. The two states must be identical: same round trips, same quantities,
prices, times, P&L, gross, commission, and the same fill composition
underneath. A no-op re-match in between must change nothing, and reviews must
survive it.

The journal's open exposure must also equal the matcher's. An oversell puts one
execution in two places at once -- closing a long and opening a short with what
is left over -- and deciding by membership in position_fills made that short
invisible everywhere in the app while the matcher had it all along.

Read-only with respect to the real ledger: everything runs inside a transaction
that is always rolled back, on ZZ* tickers that do not exist in it.

Exit code is the number of scenarios that failed, so it can gate a deploy.
"""

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, __import__("os").path.dirname(__file__) or ".")

import main  # noqa: E402
from services.matching_engine import run_matching_for_ticker  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

T0 = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)

# (label, [(tag, direction, qty, price, minutes_from_T0), ...]) in ARRIVAL
# order, which is deliberately not chronological order -- that gap is the
# entire C1 family.
SCENARIOS = [
    ("simple round trip, then a repair fill dated inside it", [
        ("A", "BUY", "10", "100", 0),
        ("B", "SELL", "15", "110", 60),
        ("C", "BUY", "5", "90", 30),
    ]),
    ("backdated buy dissolves the round trip", [
        ("A", "BUY", "10", "100", 0),
        ("B", "SELL", "10", "110", 60),
        ("C", "BUY", "10", "90", -30),
    ]),
    ("backdated fill re-pairs into a different round trip", [
        ("A", "BUY", "10", "100", 0),
        ("B", "SELL", "10", "110", 60),
        ("C", "BUY", "10", "90", -30),
        ("D", "SELL", "10", "120", 90),
    ]),
    ("oversell flips long to short, one fill in two round trips", [
        ("A", "BUY", "10", "100", 0),
        ("B", "SELL", "15", "110", 60),
        ("C", "BUY", "5", "105", 120),
    ]),
    ("scale in and scale out, arriving out of order", [
        ("D", "SELL", "8", "120", 180),
        ("A", "BUY", "5", "100", 0),
        ("C", "SELL", "4", "115", 120),
        ("B", "BUY", "7", "104", 60),
    ]),
    ("repair fill added inside an existing flip", [
        ("A", "BUY", "10", "100", 0),
        ("B", "SELL", "15", "110", 120),
        ("C", "BUY", "5", "105", 180),
        ("D", "BUY", "3", "95", 60),
    ]),
    ("every fill arrives in reverse chronological order", [
        ("D", "SELL", "6", "130", 180),
        ("C", "BUY", "6", "120", 120),
        ("B", "SELL", "4", "115", 60),
        ("A", "BUY", "4", "100", 0),
    ]),
]


async def snapshot(session, ticker: str) -> dict:
    """State keyed by round trip identity, since row ids differ on a rebuild."""
    rows = (
        await session.execute(
            select(main.Position).where(main.Position.symbol == ticker)
        )
    ).scalars().all()

    out = {}
    for position in rows:
        fills = (
            await session.execute(
                select(
                    main.PositionFill.trade_id,
                    main.PositionFill.role,
                    main.PositionFill.quantity,
                    main.PositionFill.price,
                ).where(main.PositionFill.position_id == position.id)
            )
        ).all()
        out[(position.open_trade_id, position.close_trade_id)] = {
            "direction": position.direction,
            "quantity": Decimal(str(position.quantity)),
            "entry_price": Decimal(str(position.entry_price)),
            "exit_price": Decimal(str(position.exit_price)),
            "entry_time": position.entry_time,
            "exit_time": position.exit_time,
            "realized_pnl": Decimal(str(position.realized_pnl)),
            "gross_pnl": Decimal(str(position.gross_pnl)),
            "commission": Decimal(str(position.commission)),
            "style": position.style,
            "fills": sorted(
                (
                    str(f.trade_id),
                    f.role,
                    str(Decimal(str(f.quantity))),
                    str(Decimal(str(f.price))),
                )
                for f in fills
            ),
        }
    return out


def differences(left: dict, right: dict) -> list[str]:
    if set(left) != set(right):
        return [f"different round trips: {len(left)} vs {len(right)}"]
    return [
        f"{field}: incremental={left[key][field]} rebuild={right[key][field]}"
        for key in left
        for field in left[key]
        if left[key][field] != right[key][field]
    ]


async def verify() -> int:
    """Returns the number of scenarios that failed, for use as an exit code."""
    async with main.engine.connect() as conn:
        transaction = await conn.begin()
        Session = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        failures = 0
        try:
            async with Session() as session:
                for index, (label, fills) in enumerate(SCENARIOS):
                    ticker = f"ZZCV{index}"

                    # Incremental: re-match after every arriving fill.
                    for tag, direction, quantity, price, minute in fills:
                        session.add(
                            main.Trade(
                                id=uuid.uuid4(),
                                ibkr_exec_id=f"IBKR-{ticker}-{tag}",
                                ticker=ticker,
                                direction=direction,
                                style="Unclassified",
                                entry_date=T0 + timedelta(minutes=minute),
                                actual_entry=Decimal(price),
                                quantity=Decimal(quantity),
                                commission=Decimal("0.35"),
                            )
                        )
                        await session.commit()
                        await run_matching_for_ticker(session, ticker, persist=True)

                    incremental = await snapshot(session, ticker)

                    # Grade everything, so the no-op pass has something to lose.
                    await session.execute(
                        main.Position.__table__.update()
                        .where(main.Position.symbol == ticker)
                        .values(review_status="reviewed", trade_grade="A")
                    )
                    await session.commit()
                    graded = len(incremental)

                    await run_matching_for_ticker(session, ticker, persist=True)
                    unchanged = await snapshot(session, ticker)
                    still = (
                        await session.execute(
                            select(main.Position.review_status).where(
                                main.Position.symbol == ticker
                            )
                        )
                    ).scalars().all()

                    # Clean rebuild from the same fills.
                    await session.execute(
                        delete(main.Position).where(main.Position.symbol == ticker)
                    )
                    await session.commit()
                    await run_matching_for_ticker(session, ticker, persist=True)
                    rebuilt = await snapshot(session, ticker)

                    # And what the journal says is still open must equal what
                    # the matcher says is still open. An oversell puts one
                    # execution in both places at once -- closing the long and
                    # opening a short with the remainder -- and the journal
                    # used to decide by membership, so the short was invisible.
                    expected_open = (
                        await run_matching_for_ticker(session, ticker, persist=False)
                    ).open_quantity
                    journal = await main.list_round_trips(ticker=ticker, session=session)
                    reported_open = sum(
                        Decimal(str(row.quantity))
                        for row in journal
                        if row.kind == "open"
                    )
                    exposure_agrees = reported_open == expected_open

                    drift = differences(incremental, rebuilt)
                    churn = differences(incremental, unchanged)
                    # A scenario can legitimately close nothing; there are then
                    # no reviews to keep, which is not a failure to keep them.
                    reviews_kept = len(still) == graded and all(
                        status == "reviewed" for status in still
                    )

                    ok = not drift and not churn and reviews_kept and exposure_agrees
                    failures += 0 if ok else 1
                    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
                    print(
                        f"          {len(incremental)} round trip(s), "
                        f"{expected_open:g} share(s) still open; "
                        f"reviews survive a re-match: {reviews_kept}"
                    )
                    if not exposure_agrees:
                        print(
                            f"          EXPOSURE HIDDEN  journal reports "
                            f"{reported_open:g} open, matcher says {expected_open:g}"
                        )
                    for problem in drift:
                        print(f"          DIVERGED    {problem}")
                    for problem in churn:
                        print(f"          NOT IDEMPOTENT  {problem}")

                print()
                print("=" * 60)
                print(
                    f"{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios "
                    "converge on a clean rebuild"
                )
                print("CONVERGENT" if not failures else "DIVERGENCE FOUND")
                return failures
        finally:
            await transaction.rollback()
            await main.engine.dispose()


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(verify()) else 0)
