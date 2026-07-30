"""A fill IBKR sends with no price or no execution time has no honest value
to promote it with -- and marking its staging row done anyway is losing it
twice.

I5: an undated fill used to be promoted with `datetime.now(MARKET_TZ)`
standing in for its real execution_time. That is not a placeholder, it is a
wrong answer stored as data: FIFO matches by execution order, so a fill from
last month promoted at today's timestamp is matched last regardless of which
lots it actually closed, and the heatmap buckets it into today's session.

I6: an unpriced fill was correctly excluded from `trades` (line 979's
original `if e.price is not None` guard), but the staging row was still
stamped `processed = True` alongside everything that genuinely was promoted.
The next sync's ON CONFLICT DO NOTHING then treats it as an already-seen
duplicate forever -- it is gone, and nothing about a subsequent sync says so.

Both fixes land in the same place: a fill missing either field is left out of
`trade_rows`, logged (not silently dropped), and its staging row is left
`processed = False` so the gap stays visible in `_symbols_awaiting_match`
rather than being marked resolved for work that was never done.

A residual instance of I6 was found while fixing it: the SEPARATE update for
`recovered` tickers scoped its `processed = True` by symbol alone, which would
have sub-selected a permanently-unpromotable row back into "processed" the
very next sync, as a side effect of its ticker's OTHER fills resolving. Both
updates are now the same EXISTS-gated one -- a row only ever becomes
processed once a `trades` row actually backs it, regardless of which of the
two paths (this run's new fills, or a recovered ticker) put its symbol up for
re-matching. Verified live against Supabase in a rolled-back transaction: a
real ingest run over a mix of clean, undated and unpriced fills, followed by
a second run of the same statement (simulating the next sync), leaves the
undated and unpriced rows unprocessed after BOTH runs -- including the second,
where their ticker is swept into `recovered` precisely because they are still
unresolved.
"""

import inspect
import os

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

SOURCE = inspect.getsource(main.ingest_ibkr)


# ---------------------------------------------------------------------------
# IngestResult -- the two new counters
# ---------------------------------------------------------------------------


def test_skipped_counters_default_to_zero():
    """Every existing caller of IngestResult predates these two fields."""
    result = main.IngestResult(
        executions_parsed=0, staged_new=0, staged_duplicates=0,
        trades_created=0, trades_duplicates=0, positions_matched=0,
        symbols_touched=[], skipped_non_tradeable=0,
    )
    assert result.skipped_undated == 0
    assert result.skipped_unpriced == 0


def test_skipped_counters_report_what_was_skipped():
    result = main.IngestResult(
        executions_parsed=4, staged_new=4, staged_duplicates=0,
        trades_created=2, trades_duplicates=0, positions_matched=1,
        symbols_touched=["ACME"], skipped_non_tradeable=0,
        skipped_undated=1, skipped_unpriced=1,
    )
    assert result.skipped_undated == 1
    assert result.skipped_unpriced == 1


# ---------------------------------------------------------------------------
# I5: no fabricated timestamp
# ---------------------------------------------------------------------------


def test_undated_fills_are_excluded_from_trade_rows():
    """The list comprehension that builds `trade_rows` must filter on
    execution_time, not just price."""
    assert "if e.price is not None and e.execution_time is not None" in SOURCE


def test_the_fabricated_now_fallback_is_gone():
    """The whole point of I5: no code path may stand in a fabricated
    timestamp for a fill's real execution time."""
    assert "datetime.now(MARKET_TZ)" not in SOURCE
    assert "e.execution_time or " not in SOURCE


def test_undated_skips_are_logged_with_their_transaction_ids():
    """Silently dropping a real fill is the failure mode being replaced, so
    the replacement must not itself be silent."""
    assert "skipped_undated_execs" in SOURCE
    assert "logger.error" in SOURCE
    assert "no execution_time" in SOURCE


# ---------------------------------------------------------------------------
# I6: promotion and "processed" must agree on the same fills
# ---------------------------------------------------------------------------


def test_unpriced_fills_are_excluded_from_trade_rows():
    assert "if e.price is not None and e.execution_time is not None" in SOURCE


def test_unpriced_skips_are_logged_with_their_transaction_ids():
    assert "skipped_unpriced_execs" in SOURCE
    assert "no price" in SOURCE


def test_processed_is_gated_on_a_trade_actually_existing():
    """Not "was staged this run" (I6's original bug) and not "belongs to a
    recovered ticker" (the residual instance of the same bug) -- a staging
    row only earns `processed = True` once something in `trades` backs it."""
    assert "exists()" in SOURCE
    assert 'Trade.ibkr_exec_id == "IBKR-" + IBKRExecution.transaction_id' in SOURCE


def test_processed_is_no_longer_set_by_staged_ids_alone():
    """Pins the shape of the fix rather than just its presence: the old,
    unguarded `.where(IBKRExecution.transaction_id.in_(staged_ids))` update --
    which marked every staged row processed regardless of promotion -- must be
    gone, not just supplemented."""
    assert "IBKRExecution.transaction_id.in_(staged_ids)" not in SOURCE


def test_processed_is_no_longer_set_by_symbol_alone():
    """The residual gap: `.where(IBKRExecution.symbol.in_(recovered))` with no
    further condition would sweep an unpromoted row into "processed" as soon
    as its ticker's OTHER fills got it swept into `recovered`. The fixed
    update still scopes by symbol (`IBKRExecution.symbol.in_(symbols)`,
    checked separately below) but must also require the EXISTS check on the
    same statement -- so a bare, unguarded `.in_(recovered)` must not appear.
    """
    assert "IBKRExecution.symbol.in_(recovered)" not in SOURCE


def test_the_unified_update_still_scopes_by_this_run_and_recovered_symbols():
    """Scoping by symbol is still correct and still worth keeping -- it is
    what bounds the update to the tickers this run actually touched, rather
    than scanning every row in the table. `symbols` is exactly `{this run's
    tickers} | recovered`."""
    assert "IBKRExecution.symbol.in_(symbols)" in SOURCE
