"""A fill IBKR sends with no price or no execution time has no honest value
to promote it with -- and marking its staging row done anyway is losing it
twice.

I5: an undated fill used to be promoted with `datetime.now(MARKET_TZ)`
standing in for its real execution_time. That is not a placeholder, it is a
wrong answer stored as data: FIFO matches by execution order, so a fill from
last month promoted at today's timestamp is matched last regardless of which
lots it actually closed, and the heatmap buckets it into today's session.

I6: an unpriced fill was correctly excluded from `trades`, but the staging row
was still stamped `processed = True` alongside everything that genuinely was
promoted. The next sync's ON CONFLICT DO NOTHING then treats it as an
already-seen duplicate forever -- it is gone, and nothing about a subsequent
sync says so.

Both fixes land in the same place: a fill missing either field is left out of
`trade_rows`, logged (not silently dropped), and its staging row is left
`processed = False`.

That fix has its own edge, found while hardening it rather than while writing
it: a staging row that can NEVER be promoted (no future sync will supply a
price IBKR never sent) stays `processed = False` forever, and used to feed
`_symbols_awaiting_match` -> `recovered` on every subsequent sync, telling the
UI a ticker's fills "were imported but never matched" when they were never
imported at all. `_promoted_into_trades()` is the one predicate both the
processed-flag update and the resume/stranded split are built from, so they
cannot drift onto different definitions of "promoted" -- `_symbols_awaiting_match`
now returns only genuine resume work, and `_stranded_fills` / `_stranded_summary`
report the permanently-stuck rows on their own, standing counters
(`IngestResult.stranded_fills` / `stranded_symbols`) that are queried fresh
every sync rather than computed from what that sync's statement contained.

The tests below that need a real database (marked `@requires_db`) drive the
actual `main.ingest_ibkr` endpoint function through two simulated syncs, with
`services.ibkr_client.fetch_statements` and `services.ibkr_parser.parse_statement`
monkeypatched to hand it synthetic fills -- so they exercise the real staging
insert, the real promotion filter, and the real processed-flag UPDATE, not a
reconstruction of them. Run inside a transaction that is always rolled back
(see `tests/conftest.py::db_transaction`); nothing here touches the live
ledger.
"""

import asyncio
import os
import uuid
from datetime import timedelta
from decimal import Decimal

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from services.ibkr_parser import ParsedExecution  # noqa: E402

from conftest import db_session, db_transaction, requires_db  # noqa: E402


# ---------------------------------------------------------------------------
# IngestResult -- shape and defaults
# ---------------------------------------------------------------------------


def test_skipped_counters_default_to_zero():
    """Every existing caller of IngestResult predates these fields."""
    result = main.IngestResult(
        executions_parsed=0, staged_new=0, staged_duplicates=0,
        trades_created=0, trades_duplicates=0, positions_matched=0,
        symbols_touched=[], skipped_non_tradeable=0,
    )
    assert result.skipped_undated == 0
    assert result.skipped_unpriced == 0
    assert result.skipped_unusable == 0
    assert result.stranded_fills == 0
    assert result.stranded_symbols == []


def test_skipped_counters_report_what_was_skipped():
    result = main.IngestResult(
        executions_parsed=4, staged_new=4, staged_duplicates=0,
        trades_created=2, trades_duplicates=0, positions_matched=1,
        symbols_touched=["ACME"], skipped_non_tradeable=0,
        skipped_undated=1, skipped_unpriced=1, skipped_unusable=2,
        stranded_fills=2, stranded_symbols=["ACME"],
    )
    assert result.skipped_undated == 1
    assert result.skipped_unpriced == 1
    assert result.skipped_unusable == 2
    assert result.stranded_fills == 2
    assert result.stranded_symbols == ["ACME"]


# ---------------------------------------------------------------------------
# _promoted_into_trades / _symbols_awaiting_match / _stranded_fills --
# the resume/stranded split, against real Postgres
# ---------------------------------------------------------------------------


@requires_db
def test_symbols_awaiting_match_excludes_stranded_rows():
    """The regression this whole fix chases: a symbol whose ONLY unprocessed
    rows can never be promoted must not appear in the resume set, or it feeds
    `recovered` forever."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            sym = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]
            now = main.datetime.now(main.MARKET_TZ)

            session.add_all([
                main.IBKRExecution(
                    id=uuid.uuid4(), transaction_id=f"T-{uuid.uuid4().hex[:10]}",
                    symbol=sym, quantity=Decimal("5"), price=None,
                    execution_time=now - timedelta(days=1), processed=False,
                ),
            ])
            await session.flush()

            awaiting = await main._symbols_awaiting_match(session)
            assert sym not in awaiting

            stranded_syms = await main._stranded_fills(session)
            assert sym in stranded_syms

    asyncio.run(scenario())


@requires_db
def test_symbols_awaiting_match_still_finds_genuine_resume_work():
    """The other half of the split: a row that WAS promoted but never matched
    is real resume work and must still surface."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            sym = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]
            now = main.datetime.now(main.MARKET_TZ)
            tid = f"T-{uuid.uuid4().hex[:10]}"

            session.add(main.IBKRExecution(
                id=uuid.uuid4(), transaction_id=tid, symbol=sym,
                quantity=Decimal("5"), price=Decimal("100"),
                execution_time=now - timedelta(days=1), processed=False,
            ))
            # A `trades` row backs it (promoted) but nothing marked it
            # processed -- exactly what an interrupted run between the two
            # commits in ingest_ibkr leaves behind.
            session.add(main.Trade(
                id=uuid.uuid4(), ibkr_exec_id=f"IBKR-{tid}", ticker=sym,
                direction="BUY", style="Unclassified", quantity=Decimal("5"),
                actual_entry=Decimal("100"), entry_date=now - timedelta(days=1),
                source_tag="IBKR",
            ))
            await session.flush()

            awaiting = await main._symbols_awaiting_match(session)
            assert sym in awaiting

            stranded_syms = await main._stranded_fills(session)
            assert sym not in stranded_syms

    asyncio.run(scenario())


@requires_db
def test_a_deleted_and_suppressed_fill_is_not_counted_as_stranded():
    """The narrowing this fix adds.

    A fill that WAS priced and dated -- and would therefore have been
    promoted -- but never got marked `processed` before its `trades` row was
    removed (an ingest crash mid-run, then the user deleting the
    just-promoted trade before the next sync could catch up and finish
    marking it) has no backing trade for a reason that has nothing to do with
    what IBKR sent. The old predicate here was "unprocessed and no backing
    trade", which miscounted this row as a data-quality problem identical to
    a genuinely undated or unpriced one. Requiring price/execution_time to
    actually be NULL is what tells the two apart.

    `delete_trade` itself deletes the `Trade` row outright and never touches
    staging, so what it actually leaves behind is: a staging row with a real
    price and time, `processed` still False, no `trades` row, and a
    `SuppressedExecution` tombstone. Built directly here rather than through
    `main.delete_trade` because the interesting state is what survives AFTER
    deletion, not the deletion call itself -- I7's tests already cover that a
    delete does not corrupt anything mid-flight.
    """
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            sym = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]
            now = main.datetime.now(main.MARKET_TZ)
            tid = f"T-{uuid.uuid4().hex[:10]}"

            session.add(main.IBKRExecution(
                id=uuid.uuid4(), transaction_id=tid, symbol=sym,
                quantity=Decimal("5"), price=Decimal("100"),
                execution_time=now - timedelta(days=1), processed=False,
            ))
            session.add(main.SuppressedExecution(
                ibkr_exec_id=f"IBKR-{tid}", ticker=sym,
                reason="Deleted from the journal",
                direction="BUY", quantity=Decimal("5"), price=Decimal("100"),
                executed_at=now - timedelta(days=1),
            ))
            await session.flush()

            stranded_syms = await main._stranded_fills(session)
            assert sym not in stranded_syms, (
                "a deleted, well-formed fill must not be reported as a "
                "price/execution_time data-quality problem"
            )

            # Nothing to resume either -- the trade is gone on purpose, not
            # interrupted mid-match.
            awaiting = await main._symbols_awaiting_match(session)
            assert sym not in awaiting

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# The end-to-end regression, through the real endpoint
# ---------------------------------------------------------------------------


def _patch_ibkr(monkeypatch, executions_by_call):
    """Feed `ingest_ibkr` synthetic fills without touching the real IBKR API.

    `executions_by_call` is consumed one list per call to `fetch_statements`,
    so the test can hand sync 1 and sync 2 different -- or, as here,
    identical -- payloads, exactly mirroring IBKR's rolling Flex window
    re-reporting the same unresolved fills on every query.
    """
    import services.ibkr_client as ibkr_client_mod
    import services.ibkr_parser as ibkr_parser_mod

    calls = iter(executions_by_call)

    async def fake_fetch_statements():
        return [("Q1", next(calls))], []

    def fake_parse_statement(execs):
        return execs

    def fake_count_non_tradeable(_execs):
        return 0

    monkeypatch.setattr(ibkr_client_mod, "fetch_statements", fake_fetch_statements)
    monkeypatch.setattr(ibkr_parser_mod, "parse_statement", fake_parse_statement)
    monkeypatch.setattr(ibkr_parser_mod, "count_non_tradeable", fake_count_non_tradeable)


@requires_db
def test_stranded_fills_survive_a_second_sync_without_polluting_recovered(monkeypatch):
    """The full regression, end to end, through the real `ingest_ibkr`.

    Two symbols, four fills:
      sym1/clean         -- promotable, the control that proves the run works
      sym1/undated       -- no execution_time
      sym1/unpriced      -- no price
      sym2/both_missing  -- no price AND no execution_time, which is what
                             makes skipped_unusable a real union rather than a
                             sum: undated(2) + unpriced(2) would say 4, the
                             true count of distinct unusable fills is 3.

    Sync 1 promotes clean and stamps it processed; the other three are staged
    but deliberately left unprocessed. Sync 2 resubmits the SAME four
    transaction_ids -- what IBKR resending an unresolved statement looks like
    -- and must find nothing new to skip (staging already has them) while
    still reporting the standing stranded total, and without re-touching
    either symbol in `symbols_touched`.
    """
    async def scenario():
        async with db_transaction() as conn:
            sym1 = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]
            sym2 = f"ZZ{uuid.uuid4().hex[:6].upper()}"[:10]
            now = main.datetime.now(main.MARKET_TZ)
            clean_tid = f"T-{uuid.uuid4().hex[:10]}"
            undated_tid = f"T-{uuid.uuid4().hex[:10]}"
            unpriced_tid = f"T-{uuid.uuid4().hex[:10]}"
            both_missing_tid = f"T-{uuid.uuid4().hex[:10]}"

            def fills():
                return [
                    ParsedExecution(
                        transaction_id=clean_tid, symbol=sym1,
                        quantity=Decimal("10"), price=Decimal("100"),
                        commission=Decimal("-1"), execution_time=now - timedelta(days=3),
                    ),
                    ParsedExecution(
                        transaction_id=undated_tid, symbol=sym1,
                        quantity=Decimal("5"), price=Decimal("101"),
                        commission=Decimal("-1"), execution_time=None,
                    ),
                    ParsedExecution(
                        transaction_id=unpriced_tid, symbol=sym1,
                        quantity=Decimal("-7"), price=None,
                        commission=None, execution_time=now - timedelta(days=2),
                    ),
                    ParsedExecution(
                        transaction_id=both_missing_tid, symbol=sym2,
                        quantity=Decimal("3"), price=None,
                        commission=None, execution_time=None,
                    ),
                ]

            # --- sync 1 ---------------------------------------------------
            _patch_ibkr(monkeypatch, [fills()])
            session1 = db_session(conn)
            result1 = await main.ingest_ibkr(session=session1)
            await session1.close()

            assert result1.skipped_undated == 2   # undated + both_missing
            assert result1.skipped_unpriced == 2  # unpriced + both_missing
            # Union, not sum: sum would be 2 (undated) + 2 (unpriced) = 4,
            # counting both_missing twice. It is one fill, counted once.
            assert result1.skipped_unusable == 3
            assert result1.stranded_fills >= 3
            assert set(result1.stranded_symbols) >= {sym1, sym2}

            check1 = db_session(conn)
            flags = dict((
                await check1.execute(
                    main.select(
                        main.IBKRExecution.transaction_id, main.IBKRExecution.processed
                    ).where(
                        main.IBKRExecution.transaction_id.in_(
                            [clean_tid, undated_tid, unpriced_tid, both_missing_tid]
                        )
                    )
                )
            ).all())
            assert flags[clean_tid] is True
            assert not flags[undated_tid]
            assert not flags[unpriced_tid]
            assert not flags[both_missing_tid]
            await check1.close()

            # --- sync 2: IBKR resends the same unresolved statement -------
            _patch_ibkr(monkeypatch, [fills()])
            session2 = db_session(conn)
            result2 = await main.ingest_ibkr(session=session2)
            await session2.close()

            # All four transaction_ids already exist in staging, so nothing
            # is NEW this run -- the this-run counters go quiet...
            assert result2.skipped_undated == 0
            assert result2.skipped_unpriced == 0
            assert result2.skipped_unusable == 0
            # ...but the standing total does not, because the rows are still
            # actually there, unresolved.
            assert result2.stranded_fills >= 3
            assert set(result2.stranded_symbols) >= {sym1, sym2}
            # The headline property: neither symbol is re-touched forever.
            assert sym1 not in result2.symbols_touched
            assert sym2 not in result2.symbols_touched
            assert sym1 not in result2.symbols_recovered
            assert sym2 not in result2.symbols_recovered

    asyncio.run(scenario())
