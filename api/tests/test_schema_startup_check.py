"""Refusing to boot when the database is behind the code.

THE OUTAGE THIS ENCODES. Migration 038 added `positions.journaled_at` to the
ORM model. The PR merged, CI was green -- it runs migrate.py against its own
throwaway Postgres -- and Northflank redeployed. Nothing applied the migration
to the real database: the container's command is gunicorn and nothing else.
Every endpoint issuing `select(Position)` began returning 500 while /health
went on reporting `"database": "ok"`, because connectivity was never the
problem. The Journal, Trade Inbox and Analytics pages were down. The
dashboard's equity curve kept working, since it selects a narrow explicit
column list that never mentions the new column -- so the symptom did not even
look like a schema problem, and reproduced as "the database is unreachable".

The check turns that into a container that refuses to start, which shows up in
the deploy log in seconds and cannot be read as anything else.

Three distinctions carry the whole design, and each is a test below:

  * BEHIND is fatal; UNREACHABLE is not. An unreachable database says nothing
    about the schema, is transient, and is what /health exists to report -- a
    process that died at boot reports nothing. A database that answers and is
    behind never fixes itself, so only that one stops the boot.
  * A MISSING TRACKING TABLE reads as "nothing applied", not as an error, and
    the message says how to adopt a hand-migrated database rather than only
    how to migrate one.
  * The ESCAPE HATCH works, because this check stands between a deploy and all
    traffic. If it is ever wrong, the alternative to an env var is a rebuild
    during an outage.

No database needed: the one I/O call is `_applied_migration_filenames`, and
every test here substitutes it.
"""

import asyncio
import os

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
import migrate  # noqa: E402

run = asyncio.run


@pytest.fixture
def applied(monkeypatch):
    """Substitute what the database reports, without touching one."""

    def set_to(value):
        async def fake():
            return value

        monkeypatch.setattr(main, "_applied_migration_filenames", fake)

    return set_to


@pytest.fixture(autouse=True)
def _no_skip(monkeypatch):
    """The escape hatch is off unless a test turns it on -- otherwise a stray
    value in the developer's own environment would quietly disable every
    assertion in this file."""
    monkeypatch.delenv(main.SKIP_SCHEMA_CHECK_ENV, raising=False)


def _all_on_disk() -> set[str]:
    return {m.filename for m in migrate.load_migrations()}


def test_a_current_schema_boots(applied):
    applied(_all_on_disk())
    run(main._assert_schema_current())  # does not raise


def test_a_behind_schema_refuses_to_boot(applied):
    on_disk = _all_on_disk()
    newest = max(on_disk, key=migrate.sort_key)
    applied(on_disk - {newest})

    with pytest.raises(main.SchemaBehindError) as caught:
        run(main._assert_schema_current())

    # The message has to name the file. "Schema is behind" alone leaves
    # whoever is reading the crash log to go and diff it by hand, at the exact
    # moment the site is down.
    assert newest in str(caught.value)


def test_the_message_says_how_to_fix_it(applied):
    applied(_all_on_disk() - {max(_all_on_disk(), key=migrate.sort_key)})

    with pytest.raises(main.SchemaBehindError) as caught:
        run(main._assert_schema_current())

    message = str(caught.value)
    assert "python migrate.py" in message
    # And how to override, because the person reading this is mid-outage and
    # needs to know an escape hatch exists at all.
    assert main.SKIP_SCHEMA_CHECK_ENV in message


def test_an_unreachable_database_still_boots(applied):
    """None, not an empty set -- the distinction the whole design rests on.

    Refusing to boot here would turn a transient blip into a crashloop, and
    take /health down with it precisely when it is the thing worth reading.
    """
    applied(None)
    run(main._assert_schema_current())  # does not raise


def test_a_database_with_nothing_applied_refuses_and_mentions_baseline(applied):
    """An empty set is a real answer: no migration has ever run here. That is
    as fatal as being one behind -- more so -- but it also describes a
    database migrated by hand before the runner existed, so the message points
    at --baseline rather than only at a plain migrate run."""
    applied(set())

    with pytest.raises(main.SchemaBehindError) as caught:
        run(main._assert_schema_current())

    assert "--baseline" in str(caught.value)


def test_baseline_is_not_suggested_when_the_table_merely_lags(applied):
    """The opposite case: a tracking table that exists and is simply behind.
    Suggesting --baseline there would be actively harmful advice -- it records
    the pending migrations as applied WITHOUT running them, which is exactly
    how a schema silently diverges from its own migration files."""
    applied(_all_on_disk() - {max(_all_on_disk(), key=migrate.sort_key)})

    with pytest.raises(main.SchemaBehindError) as caught:
        run(main._assert_schema_current())

    assert "--baseline" not in str(caught.value)


def test_the_escape_hatch_boots_a_behind_schema(applied, monkeypatch):
    monkeypatch.setenv(main.SKIP_SCHEMA_CHECK_ENV, "1")
    applied(set())

    run(main._assert_schema_current())  # does not raise


def test_the_escape_hatch_needs_exactly_one(applied, monkeypatch):
    """Anything else is not an opt-out. A half-set variable ("true", "yes", "")
    left over from a previous incident must not silently disable the check."""
    for value in ["true", "yes", "0", ""]:
        monkeypatch.setenv(main.SKIP_SCHEMA_CHECK_ENV, value)
        applied(set())
        with pytest.raises(main.SchemaBehindError):
            run(main._assert_schema_current())


def test_the_check_runs_before_the_pool_is_warmed():
    """Ordering, pinned: a boot that is going to be refused should not first
    spend several round trips opening connections it is about to discard."""
    import inspect

    source = inspect.getsource(main.lifespan)
    assert source.index("_assert_schema_current") < source.index(
        "_warm_connection_pool"
    )
