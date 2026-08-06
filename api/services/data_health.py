"""Does the journal's stored, derived state still agree with its fills?

The same checks `reconcile_positions.py` has run from a terminal since C1,
extracted so the app can run them too. That script keeps its CLI (exit code,
printed report, deploy gating); this module is the reusable half, taking a
session and returning structure instead of opening its own connection and
printing.

WHAT IS DELIBERATELY NOT HERE

`positions.review_status = 'pending'` -- how many round trips are awaiting a
review -- is a WORKFLOW figure, not a health one, and the header already shows
it as "N Pending Reviews". Folding it in here would let the indicator sit
amber for the boring reason, which is how a health signal gets ignored on the
day it finally means something.

WHY THE BROKER CHECK IS AT CLOSING-FILL GRAIN

`_reconcile_to_broker` sets each closing fill's all-in cost to
`our_gross - broker_realized_pnl`, so realised P&L equals the broker's figure
BY CONSTRUCTION wherever that figure exists and the implied cost was
plausible. Comparing the two per LEG therefore measures apportionment, not
agreement: on this ledger it reports 13 divergences that are all artefacts.
Compared per closing fill -- the grain IBKR actually reports on -- it reports
zero, and the only things that can make it non-zero are the two real ones:
a plug `_plug_is_plausible` refused (logged, persisted nowhere, so this
comparison is the only way to see it), or a genuine lot-matching
disagreement.

Coverage is reported beside it because a check that can only fire on the
exceptions is worthless without knowing how many rows it actually covered:
a leg with no broker figure is unverified, not verified-clean.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.matching_engine import MONEY_PRECISION, run_matching_for_ticker

# Worst-first, so `max(..., key=SEVERITY.index)` folds a list of check
# statuses into the overall one.
STATUS_CLEAN = "clean"
STATUS_ATTENTION = "attention"
STATUS_CRITICAL = "critical"
SEVERITY = (STATUS_CLEAN, STATUS_ATTENTION, STATUS_CRITICAL)

# Enough to see the shape of a problem without shipping the whole ledger to a
# modal. The counts above each list are always complete.
MAX_ITEMS = 25


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=SEVERITY.index) if statuses else STATUS_CLEAN


def _f(value: Optional[Decimal]) -> float:
    return float(value or 0)


async def run_audit(session: AsyncSession) -> dict[str, Any]:
    """Every check, against the ledger as it stands. Read-only.

    Never writes: matching runs with `persist=False`, which returns before
    `lock_ticker` and before any insert or delete, so this cannot alter what
    it is auditing and cannot make a concurrent sync wait.
    """
    started = time.perf_counter()

    # Deferred, and for the same reason matching_engine defers it: `main`
    # imports this package, so importing it at module scope is a cycle.
    from main import Position, PositionFill, RealizedLeg, Trade  # noqa: PLC0415
    from main import _stranded_fills  # noqa: PLC0415

    tickers = sorted(
        {t for (t,) in (await session.execute(select(Trade.ticker).distinct())).all()}
    )

    # ---------------------------------------------------------------------
    # 1. Stored round trips vs a cold FIFO rebuild
    # ---------------------------------------------------------------------
    stale = missing = drifted = fills_mismatched = 0
    stale_pnl = Decimal("0")
    drift_pnl = Decimal("0")
    fifo_net = Decimal("0")
    integrity_items: list[dict] = []

    for ticker in tickers:
        stored = (
            await session.execute(
                select(
                    Position.id,
                    Position.open_trade_id,
                    Position.close_trade_id,
                    Position.realized_pnl,
                    Position.review_status,
                ).where(Position.symbol == ticker)
            )
        ).all()

        result = await run_matching_for_ticker(session, ticker, persist=False)

        # Quantized exactly as replace_realized_legs stores them, so this
        # compares what SHOULD be in the table against what IS, rather than a
        # rounded figure against an unrounded one -- which would leave a
        # permanent fraction of a cent of "drift" and train the reader to
        # ignore the line.
        for leg in [l for p in result.positions for l in p.legs] + result.open_legs:
            fifo_net += leg.gross_pnl.quantize(
                MONEY_PRECISION, rounding=ROUND_HALF_UP
            ) - leg.commission.quantize(MONEY_PRECISION, rounding=ROUND_HALF_UP)

        fresh_by_pair = {(p.open_trade_id, p.close_trade_id): p for p in result.positions}
        stored_pairs = {(r.open_trade_id, r.close_trade_id) for r in stored}

        for row in stored:
            if (row.open_trade_id, row.close_trade_id) not in fresh_by_pair:
                stale += 1
                stale_pnl += row.realized_pnl or Decimal("0")
                if len(integrity_items) < MAX_ITEMS:
                    integrity_items.append({
                        "ticker": ticker,
                        "kind": "STALE",
                        "detail": (
                            f"stored round trip FIFO no longer produces "
                            f"(P&L {_f(row.realized_pnl):+.2f}, review {row.review_status})"
                        ),
                    })

        for pair in fresh_by_pair.keys() - stored_pairs:
            missing += 1
            if len(integrity_items) < MAX_ITEMS:
                integrity_items.append({
                    "ticker": ticker,
                    "kind": "MISSING",
                    "detail": f"FIFO produces a round trip with no stored row ({pair[0]} -> {pair[1]})",
                })

        # Matching the pairs is not matching the money: a stored row can name
        # the right two executions and hold a figure computed under different
        # rules, which is exactly what every position did before migration 020.
        for row in stored:
            expected = fresh_by_pair.get((row.open_trade_id, row.close_trade_id))
            if expected is None:
                continue
            if (row.realized_pnl or Decimal("0")) != expected.realized_pnl:
                drifted += 1
                drift_pnl += expected.realized_pnl - (row.realized_pnl or Decimal("0"))
                if len(integrity_items) < MAX_ITEMS:
                    integrity_items.append({
                        "ticker": ticker,
                        "kind": "DRIFT",
                        "detail": (
                            f"stored P&L {_f(row.realized_pnl):+.2f} vs FIFO "
                            f"{float(expected.realized_pnl):+.2f}"
                        ),
                    })

        # And matching the money is not matching the executions it came from.
        # A round trip is keyed by its FIRST open and LAST close, so a repair
        # fill landing between them joins it without changing the pair: P&L
        # agrees while the drill-down describes a different trade.
        if stored:
            fill_rows = (
                await session.execute(
                    select(
                        PositionFill.position_id,
                        PositionFill.trade_id,
                        PositionFill.role,
                        PositionFill.quantity,
                    ).where(PositionFill.position_id.in_([r.id for r in stored]))
                )
            ).all()
            stored_fills: dict = {}
            for f in fill_rows:
                stored_fills.setdefault(f.position_id, {})[(f.trade_id, f.role)] = f.quantity

            for row in stored:
                expected = fresh_by_pair.get((row.open_trade_id, row.close_trade_id))
                if expected is None:
                    continue
                want = {(f.trade_id, f.role): f.quantity for f in expected.fills}
                if want != stored_fills.get(row.id, {}):
                    fills_mismatched += 1
                    if len(integrity_items) < MAX_ITEMS:
                        integrity_items.append({
                            "ticker": ticker,
                            "kind": "FILLS",
                            "detail": "stored fills do not match what FIFO attributes to this round trip",
                        })

    integrity_bad = stale + missing + drifted + fills_mismatched

    # ---------------------------------------------------------------------
    # 2. Does the money tie out, at its own grain
    # ---------------------------------------------------------------------
    # Round trips agreeing with FIFO says nothing about total P&L, because a
    # round trip only exists once a ticker goes flat. Money realised scaling
    # out of a position still held lives only in `realized_legs`.
    legs_net, legs_gross, legs_count = (
        await session.execute(
            select(
                func.coalesce(func.sum(RealizedLeg.realized_pnl), 0),
                func.coalesce(func.sum(RealizedLeg.gross_pnl), 0),
                func.count(),
            )
        )
    ).one()

    open_run = (
        await session.execute(
            select(func.coalesce(func.sum(RealizedLeg.realized_pnl), 0)).where(
                RealizedLeg.position_id.is_(None)
            )
        )
    ).scalar() or Decimal("0")

    reported = (
        await session.execute(select(func.coalesce(func.sum(Position.realized_pnl), 0)))
    ).scalar() or Decimal("0")

    leg_drift = Decimal(str(legs_net)) - fifo_net
    position_drift = (Decimal(str(legs_net)) - open_run) - reported

    # ---------------------------------------------------------------------
    # 3. Our legs vs IBKR's own realised figure, per closing fill
    # ---------------------------------------------------------------------
    audit_rows = (
        await session.execute(
            select(
                Trade.ibkr_exec_id,
                Trade.ticker,
                Trade.broker_realized_pnl,
                func.sum(RealizedLeg.realized_pnl).label("ours"),
            )
            .join(RealizedLeg, RealizedLeg.close_trade_id == Trade.id)
            .where(Trade.broker_realized_pnl.is_not(None))
            .group_by(Trade.ibkr_exec_id, Trade.ticker, Trade.broker_realized_pnl)
        )
    ).all()

    broker_items: list[dict] = []
    for row in audit_rows:
        delta = (row.ours or Decimal("0")) - row.broker_realized_pnl
        if delta != 0 and len(broker_items) < MAX_ITEMS:
            broker_items.append({
                "ticker": row.ticker,
                "kind": "BROKER",
                "detail": (
                    f"{row.ibkr_exec_id}: ours {_f(row.ours):+.2f} vs IBKR "
                    f"{_f(row.broker_realized_pnl):+.2f} (off by {float(delta):+.2f})"
                ),
            })
    broker_off = sum(
        1 for row in audit_rows if (row.ours or Decimal("0")) != row.broker_realized_pnl
    )

    unverified = (
        await session.execute(
            select(func.count())
            .select_from(RealizedLeg)
            .where(RealizedLeg.broker_realized_pnl.is_(None))
        )
    ).scalar() or 0

    # ---------------------------------------------------------------------
    # 4. Fills IBKR sent that could never be imported
    # ---------------------------------------------------------------------
    stranded = await _stranded_fills(session)
    stranded_symbols = sorted(set(stranded))

    # ---------------------------------------------------------------------
    money_ok = leg_drift == 0 and position_drift == 0
    checks = [
        {
            "key": "fifo_integrity",
            "label": "FIFO engine integrity",
            "status": STATUS_CRITICAL if integrity_bad else STATUS_CLEAN,
            "headline": (
                f"{integrity_bad} stored round trip(s) disagree with a cold rebuild"
                if integrity_bad
                else f"every stored round trip across {len(tickers)} ticker(s) matches a cold rebuild"
            ),
            "counts": {
                "stale": stale,
                "missing": missing,
                "drifted": drifted,
                "fills_mismatched": fills_mismatched,
                "pnl_in_stale_rows": _f(stale_pnl),
                "pnl_drift": _f(drift_pnl),
            },
            "items": integrity_items,
        },
        {
            "key": "money_ties_out",
            "label": "Realised P&L ties out",
            "status": STATUS_CLEAN if money_ok else STATUS_CRITICAL,
            "headline": (
                f"legs, round trips and a FIFO recompute all agree on {_f(Decimal(str(legs_net))):+.2f}"
                if money_ok
                else "stored P&L does not equal what the fills produce"
            ),
            "counts": {
                "legs": int(legs_count or 0),
                "legs_net_pnl": _f(Decimal(str(legs_net))),
                "legs_gross_pnl": _f(Decimal(str(legs_gross))),
                "banked_from_open_positions": _f(open_run),
                "round_trip_rows_report": _f(reported),
                "legs_vs_fifo": _f(leg_drift),
                "legs_vs_round_trips": _f(position_drift),
            },
            "items": [],
        },
        {
            "key": "broker_reconciliation",
            "label": "Agreement with IBKR",
            "status": (
                STATUS_CRITICAL if broker_off
                else STATUS_ATTENTION if unverified
                else STATUS_CLEAN
            ),
            "headline": (
                f"{broker_off} closing fill(s) disagree with IBKR's own realised figure"
                if broker_off
                else f"{len(audit_rows)} closing fill(s) tie to IBKR exactly"
                + (f", {unverified} leg(s) unverified" if unverified else "")
            ),
            "counts": {
                "fills_audited": len(audit_rows),
                "fills_disagreeing": broker_off,
                "legs_unverified": int(unverified),
                "legs_total": int(legs_count or 0),
            },
            "items": broker_items,
        },
        {
            "key": "stranded_fills",
            "label": "Stranded broker fills",
            "status": STATUS_ATTENTION if stranded else STATUS_CLEAN,
            "headline": (
                f"{len(stranded)} fill(s) IBKR sent without a price or a timestamp"
                if stranded
                else "every staged fill was importable"
            ),
            "counts": {"fills": len(stranded), "symbols": len(stranded_symbols)},
            "items": [
                {"ticker": symbol, "kind": "STRANDED", "detail": "missing price or execution time"}
                for symbol in stranded_symbols[:MAX_ITEMS]
            ],
        },
    ]

    return {
        "generated_at": datetime.now(timezone.utc),
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "tickers_checked": len(tickers),
        "status": _worst([c["status"] for c in checks]),
        "checks": checks,
    }
