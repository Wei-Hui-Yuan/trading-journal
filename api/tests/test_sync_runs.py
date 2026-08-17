"""A sync that happened has to leave a trace, especially when it failed.

THE PROBLEM THIS TABLE EXISTS FOR

`LastSyncState` lives in one browser tab's React Query cache. It is written by
the mutation that performs the sync, never fetched, and gone on reload -- so
the header badge could only ever describe a sync THIS tab had performed. That
is tolerable while every sync is a button press and stops being tolerable the
moment one runs on a schedule: a nightly job failing on an expired token
produces exactly what a quiet market produces -- no new fills, no badge, no
signal of any kind.

WHAT IS ACTUALLY LOAD-BEARING HERE

  * A run that RAISED is the row most worth having, and it is the one the
    obvious implementation loses. `record it after the ingest returns` never
    executes on the path where the ingest throws.
  * The record is written in its OWN session. A row added to the request's
    session disappears when that transaction rolls back -- which is precisely
    the situation it was meant to document.
  * Recording must never change the outcome. A sync that worked must not be
    reported as failed because the bookkeeping afterwards hit a problem, and a
    sync that failed must surface ITS error, not one from the recorder.
  * `partial` is not `success`. Some Flex queries did not return, so the ledger
    is short of fills that exist at the broker, and the staleness warning in
    the header reads exactly this distinction.

Most of these drive `main.ingest_ibkr` directly with `_run_ibkr_ingest`
monkeypatched, so the pipeline itself is not re-tested here -- what is under
test is the wrapper's bookkeeping.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402

from conftest import db_session, db_transaction, requires_db  # noqa: E402


def make_result(**overrides) -> main.IngestResult:
    """An IngestResult with every required field, shaped like a quiet run."""
    base = dict(
        executions_parsed=0,
        staged_new=0,
        staged_duplicates=0,
        trades_created=0,
        trades_duplicates=0,
        positions_matched=0,
        symbols_touched=[],
        skipped_non_tradeable=0,
    )
    base.update(overrides)
    return main.IngestResult(**base)


def _record_through(monkeypatch, spy):
    """Watch the outcome recording, and keep the endpoint on its inline path.

    Two stubs, and the second is the load-bearing one. `_claim_sync_slot`
    returning no id is the endpoint's DEGRADED path: the slot row could not be
    written, so it runs the ingest inline and records the outcome anyway rather
    than handing out a 202 pointing at a run nobody can observe. That is the
    shape these tests want -- they assert what gets RECORDED, and the transport
    split has its own tests further down.

    Without it, a manual caller would be handed a 202 and the ingest would run
    in a background task, so `asyncio.run` would return before the spy saw
    anything.
    """
    async def no_slot(**_kwargs):
        return None, None

    monkeypatch.setattr(main, "_claim_sync_slot", no_slot)
    monkeypatch.setattr(main, "_finish_sync_run", spy)


# ---------------------------------------------------------------------------
# The outcome recorded is the outcome that happened
# ---------------------------------------------------------------------------


def test_a_clean_run_is_recorded_as_success(monkeypatch):
    recorded: list[dict] = []

    async def fake_ingest(session):
        return make_result(executions_parsed=7, trades_created=3, positions_matched=2)

    async def spy(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(main, "_run_ibkr_ingest", fake_ingest)
    _record_through(monkeypatch, spy)

    result = asyncio.run(main.ingest_ibkr(session=object(), auth={}))

    assert result.trades_created == 3, "the caller still gets the real result"
    assert len(recorded) == 1
    assert recorded[0]["outcome"] == main.SYNC_OUTCOME_SUCCESS
    assert recorded[0]["result"].executions_parsed == 7


def test_a_run_with_a_failed_query_is_partial_not_success(monkeypatch):
    """`partial` means the ledger is knowingly short of fills that exist at
    the broker. Folding it into success is how a half-empty sync comes to look
    complete -- and the staleness warning reads this exact column."""
    recorded: list[dict] = []

    async def fake_ingest(session):
        return make_result(queries_failed=["query 1: IBKR rejected (code 1001): busy"])

    async def spy(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(main, "_run_ibkr_ingest", fake_ingest)
    _record_through(monkeypatch, spy)

    asyncio.run(main.ingest_ibkr(session=object(), auth={}))

    assert recorded[0]["outcome"] == main.SYNC_OUTCOME_PARTIAL


def test_an_http_failure_is_still_recorded_and_still_raised(monkeypatch):
    """The row that matters most, on the path that would otherwise skip it.

    A 503 from a throttled Flex query is the single most useful run to have on
    record, and `record after the ingest returns` never reaches it.
    """
    recorded: list[dict] = []

    async def fake_ingest(session):
        raise HTTPException(status_code=503, detail="IBKR rejected the request")

    async def spy(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(main, "_run_ibkr_ingest", fake_ingest)
    _record_through(monkeypatch, spy)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.ingest_ibkr(session=object(), auth={}))

    # Re-raised untouched: the recording observes the outcome, it does not
    # change what the caller sees.
    assert caught.value.status_code == 503
    assert caught.value.detail == "IBKR rejected the request"

    assert recorded[0]["outcome"] == main.SYNC_OUTCOME_ERROR
    assert "503" in recorded[0]["error"]
    assert "IBKR rejected the request" in recorded[0]["error"]


def test_an_unexpected_exception_is_recorded_and_re_raised(monkeypatch):
    """Not just HTTPException. A bug in the pipeline is a run that happened."""
    recorded: list[dict] = []

    async def fake_ingest(session):
        raise RuntimeError("asyncpg fell over")

    async def spy(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(main, "_run_ibkr_ingest", fake_ingest)
    _record_through(monkeypatch, spy)

    with pytest.raises(RuntimeError):
        asyncio.run(main.ingest_ibkr(session=object(), auth={}))

    assert recorded[0]["outcome"] == main.SYNC_OUTCOME_ERROR
    assert "RuntimeError" in recorded[0]["error"]


def test_recording_never_turns_a_good_sync_into_a_failed_one(monkeypatch):
    """A sync that worked must not be reported as failed because the
    bookkeeping afterwards did not."""
    async def fake_ingest(session):
        return make_result(trades_created=5)

    def exploding_session():
        raise RuntimeError("the database went away")

    monkeypatch.setattr(main, "_run_ibkr_ingest", fake_ingest)
    monkeypatch.setattr(main, "SessionLocal", exploding_session)

    result = asyncio.run(main.ingest_ibkr(session=object(), auth={}))

    assert result.trades_created == 5, (
        "a failure inside _record_sync_run must be swallowed, not surfaced"
    )


def test_a_failing_recorder_does_not_mask_the_real_error(monkeypatch):
    """The other half: when both the sync AND the recording fail, the caller
    must see the sync's error, not the recorder's."""
    async def fake_ingest(session):
        raise HTTPException(status_code=502, detail="the real problem")

    def exploding_session():
        raise RuntimeError("the decoy problem")

    monkeypatch.setattr(main, "_run_ibkr_ingest", fake_ingest)
    monkeypatch.setattr(main, "SessionLocal", exploding_session)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.ingest_ibkr(session=object(), auth={}))

    assert caught.value.detail == "the real problem"


def test_a_clerk_session_is_recorded_as_manual(monkeypatch):
    """`verify_clerk_or_cron_token` returns the ordinary Clerk claims dict for
    a browser caller -- no `cron` key at all, not `cron: False`. The trigger
    read must not assume the key is always present."""
    recorded: list[dict] = []

    async def fake_ingest(session):
        return make_result()

    async def spy(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(main, "_run_ibkr_ingest", fake_ingest)
    _record_through(monkeypatch, spy)

    asyncio.run(main.ingest_ibkr(session=object(), auth={"sub": "user_2vX...clerk"}))

    assert recorded[0]["trigger"] == main.SYNC_TRIGGER_MANUAL


def test_the_cron_secret_is_recorded_as_cron(monkeypatch):
    """The whole point of this change. `verify_clerk_or_cron_token` returns
    `{"sub": "cron", "cron": True}` for the scheduler (see auth.py), and this
    is where that stops being a fact about auth and becomes a fact about the
    row in `sync_runs` -- which is what lets the badge say "scheduled"."""
    recorded: list[dict] = []

    async def fake_ingest(session):
        return make_result()

    async def spy(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(main, "_run_ibkr_ingest", fake_ingest)
    _record_through(monkeypatch, spy)

    asyncio.run(
        main.ingest_ibkr(session=object(), auth={"sub": "cron", "cron": True})
    )

    assert recorded[0]["trigger"] == main.SYNC_TRIGGER_CRON


def test_the_endpoint_accepts_the_cron_secret_without_a_clerk_session():
    """The manual button must keep working, and the scheduler must not need
    one. Both halves of `verify_clerk_or_cron_token` are unit-tested in
    test_auth.py; this is the one place that asserts /api/ingest/ibkr is
    actually WIRED to that dependency rather than the plain Clerk check it
    replaced -- a route-level regression neither of those tests can see."""
    route = next(r for r in main.app.routes if r.path == "/api/ingest/ibkr")
    called = {dep.call for dep in route.dependant.dependencies}
    assert main.verify_clerk_or_cron_token in called
    assert main.verify_clerk_token not in called, (
        "the plain Clerk check would shut the scheduler out again"
    )


# ---------------------------------------------------------------------------
# Against real Postgres
# ---------------------------------------------------------------------------


@requires_db
def test_the_record_survives_the_ingest_transaction_rolling_back():
    """The reason `_finish_sync_run` opens its own session.

    Written into the request's session, the row describing a failed run would
    be rolled back along with the failure it documents -- losing exactly the
    evidence it exists to keep.
    """
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            started = datetime.now(timezone.utc)

            await main._finish_sync_run(
                run_id=None,
                started_at=started,
                trigger=main.SYNC_TRIGGER_MANUAL,
                outcome=main.SYNC_OUTCOME_ERROR,
                error="HTTP 503: throttled",
            )

            # Roll the REQUEST's transaction back, as a failed ingest would.
            await session.rollback()
            await session.close()

            # The row is committed independently, so a fresh session sees it.
            async with main.SessionLocal() as check:
                row = (
                    await check.execute(
                        main.select(main.SyncRun)
                        .where(main.SyncRun.started_at == started)
                    )
                ).scalars().first()
                assert row is not None, "the record did not survive the rollback"
                assert row.outcome == main.SYNC_OUTCOME_ERROR
                assert row.error == "HTTP 503: throttled"

                # Committed for real, so clean up after ourselves.
                await check.delete(row)
                await check.commit()

    asyncio.run(scenario())


@requires_db
def test_latest_reports_the_newest_run_and_the_newest_success():
    """Two different questions. A week of failing nightly runs has a very
    recent `latest` and a very old `last_success_at`, and reporting only the
    first is how a broken schedule keeps looking busy."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            now = datetime.now(timezone.utc)
            # Far enough in the future that real rows cannot outrank these.
            base = now + timedelta(days=3650)

            old_success = main.SyncRun(
                id=uuid.uuid4(), started_at=base, finished_at=base,
                trigger=main.SYNC_TRIGGER_CRON, outcome=main.SYNC_OUTCOME_SUCCESS,
                executions_parsed=4, trades_created=1,
                positions_matched=0, plans_attached=0,
            )
            newer_failure = main.SyncRun(
                id=uuid.uuid4(),
                started_at=base + timedelta(hours=24),
                finished_at=base + timedelta(hours=24),
                trigger=main.SYNC_TRIGGER_CRON, outcome=main.SYNC_OUTCOME_ERROR,
                executions_parsed=0, trades_created=0,
                positions_matched=0, plans_attached=0,
                error="HTTP 503: throttled",
            )
            session.add_all([old_success, newer_failure])
            await session.flush()

            status = await main.latest_sync_run(session=session)

            assert status.latest.outcome == main.SYNC_OUTCOME_ERROR, (
                "latest must be the newest run, failure or not"
            )
            assert status.latest.trigger == main.SYNC_TRIGGER_CRON
            # ...but the freshness answer comes from the last one that WORKED.
            assert status.last_success_at == base
            assert status.seconds_since_success is not None

    asyncio.run(scenario())


@requires_db
def test_a_partial_run_does_not_count_as_a_successful_sync():
    """The staleness warning is built on this. A partial run left fills at the
    broker that never reached the ledger, so it cannot reset the clock."""
    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            base = datetime.now(timezone.utc) + timedelta(days=3650)

            session.add(main.SyncRun(
                id=uuid.uuid4(), started_at=base, finished_at=base,
                trigger=main.SYNC_TRIGGER_CRON, outcome=main.SYNC_OUTCOME_PARTIAL,
                executions_parsed=2, trades_created=0,
                positions_matched=0, plans_attached=0,
            ))
            await session.flush()

            status = await main.latest_sync_run(session=session)

            assert status.latest.outcome == main.SYNC_OUTCOME_PARTIAL
            assert status.last_success_at != base, (
                "a partial run must not be read as a successful one"
            )

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Who waits, and who is handed a run id (migration 035)
# ---------------------------------------------------------------------------


def _claims_slot(monkeypatch, run_id):
    """Stub the slot claim so it succeeds, without touching a database."""
    async def claim(**_kwargs):
        return run_id, None

    monkeypatch.setattr(main, "_claim_sync_slot", claim)


def test_the_scheduler_waits_and_gets_the_full_result(monkeypatch):
    """The cron job's exit code is the only signal Northflank has.

    Answering 202 there would make every scheduled run look successful the
    moment it could reach the API -- which is exactly the confusion `sync_runs`
    was added to end, and the reason the split is by caller rather than global.
    """
    ran: list[bool] = []

    async def fake_ingest(session):
        ran.append(True)
        return make_result(trades_created=4)

    async def finish(**_kwargs):
        return None

    monkeypatch.setattr(main, "_run_ibkr_ingest", fake_ingest)
    monkeypatch.setattr(main, "_finish_sync_run", finish)
    _claims_slot(monkeypatch, uuid.uuid4())

    result = asyncio.run(
        main.ingest_ibkr(session=object(), auth={"sub": "cron", "cron": True})
    )

    assert ran, "the scheduler's request returned without running the ingest"
    assert isinstance(result, main.IngestResult)
    assert result.trades_created == 4, (
        "the cron caller must still receive the real result, unchanged"
    )


def test_a_browser_is_handed_a_run_id_without_waiting(monkeypatch):
    """The point of the change: 202 now, outcome later via the badge."""
    run_id = uuid.uuid4()
    started = asyncio.Event()

    async def slow_ingest(session):
        started.set()
        await asyncio.sleep(0.05)
        return make_result()

    async def finish(**_kwargs):
        return None

    monkeypatch.setattr(main, "_run_ibkr_ingest", slow_ingest)
    monkeypatch.setattr(main, "_finish_sync_run", finish)
    _claims_slot(monkeypatch, run_id)

    async def scenario():
        response = await main.ingest_ibkr(session=object(), auth={"sub": "user_2vX"})
        # Let the handed-off task actually begin before the loop closes.
        await asyncio.wait_for(started.wait(), timeout=1)
        return response

    response = asyncio.run(scenario())

    assert response.status_code == 202
    body = json.loads(response.body)
    assert body["run_id"] == str(run_id)
    assert body["outcome"] == main.SYNC_OUTCOME_RUNNING


def test_the_background_task_is_held_so_it_cannot_be_collected(monkeypatch):
    """asyncio keeps only a WEAK reference to a bare create_task.

    A collected task stops silently, leaving a `running` row nothing will finish
    and no traceback to explain it. The strong-reference set is what prevents
    that, and it is invisible in behaviour until it bites.
    """
    async def slow_ingest(session):
        await asyncio.sleep(0.05)
        return make_result()

    async def finish(**_kwargs):
        return None

    monkeypatch.setattr(main, "_run_ibkr_ingest", slow_ingest)
    monkeypatch.setattr(main, "_finish_sync_run", finish)
    _claims_slot(monkeypatch, uuid.uuid4())

    async def scenario():
        await main.ingest_ibkr(session=object(), auth={"sub": "user_2vX"})
        held = len(main._BACKGROUND_SYNCS)
        # Drain it, so the suite does not leave a task pending on a dead loop.
        await asyncio.gather(*list(main._BACKGROUND_SYNCS))
        return held, len(main._BACKGROUND_SYNCS)

    while_running, after = asyncio.run(scenario())

    assert while_running == 1, "the task was not retained while it ran"
    assert after == 0, "the done-callback did not release the task"


def test_a_second_sync_while_one_runs_is_refused(monkeypatch):
    """409 rather than a second ingest.

    Two concurrent runs are safe at the data layer -- ticker advisory locks, ON
    CONFLICT on transaction_id -- so this is about not showing two runs in a UI
    with room for one, and not spending IBKR's per-token rate limit twice on the
    same fills.
    """
    blocker = main.SyncRun(
        id=uuid.uuid4(),
        started_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        trigger=main.SYNC_TRIGGER_MANUAL,
        outcome=main.SYNC_OUTCOME_RUNNING,
    )

    async def claim(**_kwargs):
        return None, blocker

    async def fake_ingest(session):  # pragma: no cover - must not run
        raise AssertionError("a second ingest was started while one was running")

    monkeypatch.setattr(main, "_claim_sync_slot", claim)
    monkeypatch.setattr(main, "_run_ibkr_ingest", fake_ingest)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.ingest_ibkr(session=object(), auth={"sub": "user_2vX"}))

    assert caught.value.status_code == 409
    assert "still running" in caught.value.detail


def test_an_unrecordable_slot_falls_back_to_waiting(monkeypatch):
    """A 202 pointing at a run nobody can observe is worse than a slow one.

    `_claim_sync_slot` yielding no id means the database would not take the row,
    so the endpoint degrades to the old synchronous behaviour rather than
    promising a record that does not exist.
    """
    async def fake_ingest(session):
        return make_result(trades_created=2)

    async def finish(**_kwargs):
        return None

    async def no_slot(**_kwargs):
        return None, None

    monkeypatch.setattr(main, "_run_ibkr_ingest", fake_ingest)
    monkeypatch.setattr(main, "_finish_sync_run", finish)
    monkeypatch.setattr(main, "_claim_sync_slot", no_slot)

    result = asyncio.run(main.ingest_ibkr(session=object(), auth={"sub": "user_2vX"}))

    assert isinstance(result, main.IngestResult), (
        "the caller was handed a 202 despite the run being unrecordable"
    )
    assert result.trades_created == 2


def test_the_reaper_waits_longer_than_the_fetch_budget():
    """Reaping a run that is merely slow would mark a LIVE ingest failed and let
    a second start beside it.

    Enforced against the real constant rather than a copy, because
    SYNC_RUN_ABANDONED_AFTER is written as a literal in main.py -- everything in
    `services` is imported locally there to keep that module out of an import
    cycle, so it cannot be derived. Same approach
    test_the_budget_is_below_the_worker_timeout takes to the Dockerfile.
    """
    from services import ibkr_client

    assert (
        main.SYNC_RUN_ABANDONED_AFTER.total_seconds()
        > ibkr_client.TOTAL_BUDGET_SECONDS
    ), (
        f"the reaper fires after {main.SYNC_RUN_ABANDONED_AFTER.total_seconds()}s "
        f"but the ingest is allowed {ibkr_client.TOTAL_BUDGET_SECONDS}s just for "
        "the Flex handshake, so a healthy run would be reaped mid-flight"
    )
