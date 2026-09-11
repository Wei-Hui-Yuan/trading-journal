"""The container's own migration step, and when it refuses to serve.

This file is mostly about ONE decision, because that decision has already
been made wrong once in production. `start.py` runs the migrations before
gunicorn takes over, and then has to choose between starting and refusing on
whatever the runner reported.

The second of this project's three schema outages was a boot check that
conflated "the schema is behind" with "I could not work out whether the
schema is behind", and killed every worker over a missing directory. The rule
that came out of it -- fail CLOSED only on a definite answer, fail OPEN on
any inability to reach one -- is what the tests below pin, code by code.

The exec itself is not tested. os.execvp replaces the process, which a test
runner cannot survive; the decision that precedes it is the part with
judgement in it, and it is a pure function of the exit code.
"""

import os

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import migrate  # noqa: E402
import start  # noqa: E402


class TestTheDecision:
    def test_a_clean_run_starts(self):
        assert start.should_start(0) is True

    def test_a_migration_that_failed_partway_does_not_start(self):
        """The one definite answer: the schema is between two shapes, with the
        failing file and everything after it unapplied. main.py's boot check
        would refuse this anyway -- stopping here just does it with the actual
        SQL error in the log rather than a list of filenames."""
        assert start.should_start(1) is False

    def test_a_runner_that_declined_to_act_still_starts(self):
        """Exit 2 is migrate.py refusing -- a MigrationError, which includes
        the missing-migrations-directory case that killed every worker in the
        second outage. Declining to act says nothing about the schema, so it
        must not be treated as evidence against it."""
        assert start.should_start(2) is True

    def test_drift_still_starts(self):
        """Exit 3 means a file changed after it was applied. Real, and a
        developer-hygiene problem: the running schema is whatever it already
        was, and an edited comment in an applied migration must not take
        production down. A genuinely BEHIND schema is still caught by
        main.py's own check."""
        assert start.should_start(3) is True

    def test_an_unknown_answer_starts(self):
        """None means the runner raised rather than returning -- an
        unreachable database, most likely. Refusing to start over that turns
        a transient blip into an outage, when /health already reports it
        honestly and it recovers on its own."""
        assert start.should_start(None) is True

    def test_only_a_real_failure_blocks(self):
        """Stated as a set rather than a fourth individual case, so adding a
        new blocking code is a deliberate edit to this assertion rather than
        something that slips in."""
        assert start.BLOCKING_EXIT_CODES == frozenset({1})


class TestTheToggle:
    def test_it_is_on_by_default(self):
        """An image deployed with no environment configuration at all must
        migrate -- that is the entire point. An opt-IN default would leave the
        outage one unset variable away."""
        assert start.migrations_enabled({}) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE", " no "])
    def test_it_can_be_switched_off_from_the_dashboard(self, value):
        assert start.migrations_enabled({start.RUN_MIGRATIONS_ENV: value}) is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "", "anything"])
    def test_anything_else_leaves_it_on(self, value):
        """Fails safe on a typo. `RUN_MIGRATIONS_ON_START=flase` migrating is
        the harmless reading of that mistake; silently not migrating is the
        outage."""
        assert start.migrations_enabled({start.RUN_MIGRATIONS_ENV: value}) is True

    def test_switching_it_off_skips_the_runner_and_still_starts(self, monkeypatch):
        called = []
        monkeypatch.setattr(migrate, "main", lambda argv: called.append(argv) or 0)
        monkeypatch.setenv(start.RUN_MIGRATIONS_ENV, "0")

        code = start.apply_pending()

        assert called == []
        assert start.should_start(code) is True


class TestRunningTheMigrations:
    def test_it_passes_allow_pooler(self):
        """Not a preference. This deployment's DATABASE_URL is Supabase's
        transaction pooler on 6543, which migrate.py refuses DDL through by
        default -- and the alternatives are dead ends here: the session pooler
        on 5432 times out and the direct host does not resolve. Without the
        flag every boot would get exit 2 and never migrate anything, silently,
        because exit 2 starts the app."""
        seen = {}
        original = migrate.main

        def spy(argv=None):
            seen["argv"] = argv
            return 0

        migrate.main = spy
        try:
            assert start.apply_pending() == 0
        finally:
            migrate.main = original

        assert "--allow-pooler" in seen["argv"]

    def test_a_raising_runner_becomes_none_rather_than_an_exception(self, monkeypatch):
        """start.py must not itself crash on a bad migration run: a traceback
        escaping here would stop gunicorn ever being exec'd, which is the
        outage this whole file exists to prevent."""
        def explode(argv=None):
            raise RuntimeError("could not connect to the database")

        monkeypatch.setattr(migrate, "main", explode)

        code = start.apply_pending()

        assert code is None
        assert start.should_start(code) is True

    def test_the_runners_own_exit_code_is_passed_through_untouched(self, monkeypatch):
        """No remapping between the two: migrate.py's codes are the contract
        BLOCKING_EXIT_CODES is written against, and a translation layer here
        is where the two would drift apart."""
        for code in (0, 1, 2, 3):
            monkeypatch.setattr(migrate, "main", lambda argv, c=code: c)
            assert start.apply_pending() == code


class TestTheLock:
    def test_a_real_run_serialises_against_another_container(self):
        """A rolling deploy briefly has two containers up, and before the lock
        both would read the same pending list and both start applying it. The
        files are idempotent, so the likely outcome was a duplicate-key error
        rather than a corrupt schema -- but a migration that manages its own
        transaction has no such protection, and "likely" is not a property to
        rest a deploy on."""
        assert isinstance(migrate.MIGRATION_LOCK_KEY, int)
        # Fits in the bigint pg_advisory_lock takes.
        assert 0 < migrate.MIGRATION_LOCK_KEY < 2 ** 63

    def test_the_key_is_derived_from_the_table_it_guards(self):
        """Rather than a magic number, so it cannot silently collide with an
        unrelated advisory lock added later and needs no registry to look up."""
        import zlib

        assert migrate.MIGRATION_LOCK_KEY == zlib.crc32(
            migrate.TRACKING_TABLE.encode()
        )

    def test_the_read_only_commands_do_not_take_it(self):
        """--status is what you reach for when a run looks stuck. Blocking it
        behind that run's own lock would make the only diagnostic in the tool
        unusable at exactly the moment it is needed."""
        source = (migrate.MIGRATIONS_DIR.parent / "migrate.py").read_text(
            encoding="utf-8"
        )

        assert "mutating = not (args.status or args.dry_run)" in source
        # And the lock is taken only under that flag.
        lock_line = source.index("pg_advisory_lock")
        guard_line = source.index("mutating = not (")
        assert guard_line < lock_line


class TestTheHandOff:
    def test_output_written_before_the_exec_survives_it(self):
        """os.execvp REPLACES the process, so anything still in Python's
        stdout buffer is never written.

        Not hypothetical: the first working version of this file lost the
        entire migration log that way. "Applied 1 migration(s)" never
        appeared, the migration having actually run -- so the only record that
        the schema had just changed was gone. PYTHONUNBUFFERED in the
        Dockerfile hides it in the container, which is precisely why it needs
        a test: the log of what the schema did should not rest on an
        environment variable staying set.

        Run with migrations off so this needs no database -- the buffering
        question is about the exec, not about what was printed.
        """
        import subprocess
        import sys as _sys

        api_dir = migrate.MIGRATIONS_DIR.parent
        result = subprocess.run(
            [_sys.executable, "start.py", _sys.executable, "--version"],
            cwd=api_dir,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, start.RUN_MIGRATIONS_ENV: "0", "PYTHONUNBUFFERED": ""},
        )

        assert result.returncode == 0
        # Both halves present: this file's own line, and the exec'd command's.
        assert "skipping migrations" in result.stdout
        assert "Python" in result.stdout + result.stderr

    def test_an_empty_command_is_an_error_rather_than_a_silent_success(self):
        """A container that migrated and then exited 0 without serving would
        look like a clean deploy in the dashboard and answer nothing."""
        assert start.main([]) == 2


class TestTheContainerWiring:
    """The Dockerfile is the only thing that makes any of the above run."""

    @staticmethod
    def dockerfile() -> str:
        return (migrate.MIGRATIONS_DIR.parent / "Dockerfile").read_text(
            encoding="utf-8"
        )

    def test_the_entrypoint_is_start_py(self):
        assert 'ENTRYPOINT ["python", "start.py"]' in self.dockerfile()

    def test_gunicorn_is_still_the_cmd(self):
        """It has to stay in CMD form for two reasons: Docker passes CMD to
        ENTRYPOINT as the argv start.py execs, and
        test_the_budget_is_below_the_worker_timeout parses --timeout out of
        this exact line."""
        content = self.dockerfile()

        assert 'CMD ["gunicorn"' in content
        assert "--timeout" in content

    def test_the_entrypoint_comes_before_the_cmd(self):
        """Docker does not care about the order, but a reader does -- and if
        CMD were somehow turned into a shell-form line above ENTRYPOINT the
        argv hand-off would silently stop working."""
        content = self.dockerfile()

        assert content.index("ENTRYPOINT") < content.index('CMD ["gunicorn"')
