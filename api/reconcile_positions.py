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
from decimal import ROUND_HALF_UP, Decimal

sys.path.insert(0, __import__("os").path.dirname(__file__) or ".")

import main  # noqa: E402
from services.matching_engine import (  # noqa: E402
    MONEY_PRECISION,
    run_matching_for_ticker,
)
from sqlalchemy import func, select  # noqa: E402
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

                # Recomputed from the fills, at the grain money is actually
                # realised: every leg FIFO produces, closed run or not.
                fifo_net = Decimal("0")
                stale_total = 0
                missing_total = 0
                drift_total = 0
                fills_total = 0
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
                    # Quantized exactly as replace_realized_legs stores them, so
                    # this compares what SHOULD be in the table against what IS,
                    # rather than a rounded figure against an unrounded one --
                    # which would leave a permanent fraction of a cent of
                    # "drift" and train the reader to ignore this line.
                    for leg in (
                        [l for p in result.positions for l in p.legs] + result.open_legs
                    ):
                        fifo_net += (
                            leg.gross_pnl.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)
                            - leg.commission.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)
                        )
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

                    # And matching the money is not the same as matching the
                    # executions it was computed from. A round trip is keyed by
                    # its FIRST open and LAST close, so a fill landing between
                    # them joins it without changing the pair -- which is what a
                    # repair fill does by definition. Pairs agree, P&L agrees,
                    # and the drill-down can still be describing a different
                    # trade: a 15-share position whose fills sum to 10, with the
                    # repair fill floating loose as phantom open exposure.
                    fill_rows = (
                        await session.execute(
                            select(
                                main.PositionFill.position_id,
                                main.PositionFill.trade_id,
                                main.PositionFill.role,
                                main.PositionFill.quantity,
                            ).where(
                                main.PositionFill.position_id.in_(
                                    [r.id for r in stored]
                                )
                            )
                        )
                    ).all() if stored else []
                    stored_fills: dict = {}
                    for f in fill_rows:
                        stored_fills.setdefault(f.position_id, {})[
                            (f.trade_id, f.role)
                        ] = f.quantity

                    mismatched = []
                    for row in stored:
                        expected = fresh_by_pair.get(
                            (row.open_trade_id, row.close_trade_id)
                        )
                        if expected is None:
                            continue
                        want = {
                            (f.trade_id, f.role): f.quantity for f in expected.fills
                        }
                        have = stored_fills.get(row.id, {})
                        if want != have:
                            mismatched.append((row, want, have))

                    if stale or missing or drifted or mismatched:
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
                    for row, want, have in mismatched:
                        fills_total += 1
                        print(f"     FILLS   id={row.id}")
                        for key in sorted(set(want) | set(have), key=str):
                            w, h = want.get(key), have.get(key)
                            if w != h:
                                trade_id, role = key
                                print(
                                    f"        {role:<5} {str(trade_id)[:8]} "
                                    f"stored={'-' if h is None else h} "
                                    f"fifo={'-' if w is None else w}"
                                )

                reported = sum(
                    (await session.execute(select(main.Position.realized_pnl)))
                    .scalars()
                    .all()
                ) or Decimal("0")

                # ------------------------------------------------------------
                # The money, at its own grain.
                # ------------------------------------------------------------
                # Round trips agreeing with FIFO says nothing about whether the
                # journal's P&L is right, because a round trip only exists once
                # a ticker goes flat. Every dollar realised scaling out of a
                # position still held had no row at all, and this script
                # reported CLEAN throughout -- it was only ever comparing rows
                # that existed. `realized_legs` is where that money lives now
                # (migration 022), so it gets checked directly.
                legs_stored = (
                    await session.execute(
                        select(
                            func.coalesce(func.sum(main.RealizedLeg.realized_pnl), 0),
                            func.coalesce(func.sum(main.RealizedLeg.gross_pnl), 0),
                            func.count(),
                        )
                    )
                ).one()
                legs_net, legs_gross, legs_count = legs_stored

                open_run = (
                    await session.execute(
                        select(func.coalesce(func.sum(main.RealizedLeg.realized_pnl), 0))
                        .where(main.RealizedLeg.position_id.is_(None))
                    )
                ).scalar() or Decimal("0")

                leg_drift = legs_net - Decimal(str(fifo_net))
                # Legs must also account for every closed round trip exactly:
                # a position's P&L is the sum of its own legs, by construction.
                banked_in_round_trips = legs_net - open_run
                position_drift = banked_in_round_trips - reported

                print()
                print("=" * 60)
                print(f"stale positions (in the table, not in FIFO) : {stale_total}")
                print(f"missing positions (in FIFO, not the table)  : {missing_total}")
                print(f"positions whose stored P&L != FIFO's        : {drift_total}")
                print(f"positions whose FILLS != FIFO's             : {fills_total}")
                print(f"P&L attributable to stale rows              : {stale_pnl:+}")
                if drift_total:
                    print(f"P&L the drifted rows are wrong by           : {drift_pnl:+}")
                print()
                print(f"realised legs stored                        : {legs_count}")
                print(f"legs net P&L                                : {legs_net:+}")
                print(f"legs gross P&L                              : {legs_gross:+}")
                print(f"  of which banked out of OPEN positions     : {open_run:+}")
                print(f"FIFO net P&L (recomputed from fills)        : {fifo_net:+}")
                print(f"legs vs FIFO                                : {leg_drift:+}")
                print(f"round-trip rows report                      : {reported:+}")
                print(f"legs attributed to round trips              : {banked_in_round_trips:+}")
                print(f"  difference                                : {position_drift:+}")

                # ------------------------------------------------------------
                # Fill-by-fill audit against the broker's own figure.
                # ------------------------------------------------------------
                # The strongest check available, because it does not trust our
                # arithmetic at all: IBKR states what each execution realised,
                # and our legs for that execution must sum to it. Anything else
                # is a lot-matching disagreement, and it would be invisible in
                # every total above -- those only prove we agree with
                # ourselves.
                audit = (
                    await session.execute(
                        select(
                            main.Trade.ibkr_exec_id,
                            main.Trade.broker_realized_pnl,
                            func.sum(main.RealizedLeg.realized_pnl).label("ours"),
                            func.count().label("legs"),
                        )
                        .join(
                            main.RealizedLeg,
                            main.RealizedLeg.close_trade_id == main.Trade.id,
                        )
                        .where(main.Trade.broker_realized_pnl.is_not(None))
                        .group_by(main.Trade.ibkr_exec_id, main.Trade.broker_realized_pnl)
                    )
                ).all()

                broker_off = [
                    row for row in audit
                    if (row.ours or Decimal("0")) != row.broker_realized_pnl
                ]
                unverified = (
                    await session.execute(
                        select(func.count())
                        .select_from(main.RealizedLeg)
                        .where(main.RealizedLeg.broker_realized_pnl.is_(None))
                    )
                ).scalar()

                print()
                print(f"closing fills audited against IBKR          : {len(audit)}")
                print(f"  disagreeing by any amount                 : {len(broker_off)}")
                print(f"legs with no broker figure (unverified)     : {unverified}")
                for row in broker_off[:10]:
                    delta = (row.ours or Decimal("0")) - row.broker_realized_pnl
                    print(
                        f"     BROKER  {row.ibkr_exec_id:18} ours={row.ours:+} "
                        f"ibkr={row.broker_realized_pnl:+} diff={delta:+}"
                    )
                if len(broker_off) > 10:
                    print(f"     ... and {len(broker_off) - 10} more")

                money_off = (
                    leg_drift != 0 or position_drift != 0 or bool(broker_off)
                )
                if money_off:
                    print()
                    print("  !! The money does not tie out. Run POST /api/rematch.")

                print()
                clean = not (
                    stale_total or missing_total or drift_total or fills_total or money_off
                )
                print("CLEAN" if clean else "DISCREPANCIES FOUND")
                return (
                    stale_total + missing_total + drift_total + fills_total
                    + len(broker_off) + (1 if money_off and not broker_off else 0)
                )
        finally:
            await transaction.rollback()
            await main.engine.dispose()


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(reconcile()) else 0)
