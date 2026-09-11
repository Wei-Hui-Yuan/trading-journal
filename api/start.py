"""Bring the schema up to date, then hand the container over to gunicorn.

WHY THIS EXISTS

Three outages, one shape each time. A migration merges, the push to `main`
triggers a Northflank build, and the new code boots against a schema that
still has the old columns. The first time it served HTTP 500 from every
endpoint touching the changed table while /health cheerfully reported the
database as "ok". The boot check in main.py fixed the lying, not the
downtime: since then the container refuses to start instead, which is a
clean failure but is still a failure.

The gap that produced all three is the same: applying the migration was a
separate human step, taken AFTER the deploy that needed it. So this closes
the gap rather than warning about it. The container migrates itself, once,
before any worker accepts a request -- so merge order stops mattering.

A CI check was the obvious alternative and cannot work here: branch
protection and rulesets both require GitHub Pro or a public repository, and
this one is private on the free plan. A red tick that nothing enforces is
exactly the advisory signal that failed the last three times.

WHY NOT IN THE APP'S OWN LIFESPAN

WEB_CONCURRENCY is 2, so main.py's lifespan runs once per worker and both
would migrate simultaneously. This runs in the single process that exists
before gunicorn forks. The advisory lock in migrate.py covers the other
race -- two CONTAINERS, which a rolling deploy briefly has.

WHEN IT REFUSES TO START, AND WHEN IT DOES NOT

This is the part worth reading, because getting it backwards is what caused
the second outage. That one was a boot check that treated "I could not work
out whether the schema is current" the same as "the schema is behind", and
killed every worker over a missing directory.

The rule it settled on, kept here: fail CLOSED only on a definite answer,
and fail OPEN on any inability to work one out. A container that starts with
an unmigrated schema is a bad outcome; a container that refuses to start
because it could not reach the database for a moment is a worse one, since
the database being unreachable is already reported honestly by /health and
recovers on its own.

WHAT THIS BUYS AND WHAT IT DOES NOT

It removes the ordering problem for ADDITIVE migrations, which is every
migration this project has: new columns, new tables, new indexes. The old
container keeps serving happily while the new one migrates, because code
that does not know about a column is unaffected by its existence.

It does NOT make a DESTRUCTIVE migration safe. A rolling deploy has both
versions live for a moment, so a migration that drops or renames something
the old code still selects will break the old container in that window. The
standard answer is to split such a change in two -- one release that stops
using the column, a later one that drops it -- and that discipline is now
load-bearing here rather than merely advisable, because nobody is choosing
the moment the migration runs any more.
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Optional, Sequence

#: Set to 0/false/no in the Northflank dashboard to deploy without migrating.
#:
#: An escape hatch that needs no rebuild, the same reasoning as
#: WEB_CONCURRENCY. The case it is for: a migration that is wrong in a way
#: only production reveals, where the priority is getting the previous image
#: serving again rather than debugging in the deploy path.
RUN_MIGRATIONS_ENV = "RUN_MIGRATIONS_ON_START"

#: Exit codes from migrate.main() that mean the schema is DEFINITELY not
#: usable, and the container should not start.
#:
#: Only 1, which migrate.py returns when a migration raised partway through
#: applying. That leaves the schema between two shapes, with the failing file
#: and everything after it unapplied -- main.py's boot check would refuse the
#: start anyway, and stopping here does it with the actual SQL error in the
#: log instead of a list of filenames.
#:
#: The codes deliberately NOT here, each of which starts the app:
#:
#:   0  applied cleanly, or nothing was pending
#:   2  migrate.py declined to act -- a MigrationError, which includes the
#:      missing-migrations-directory case that killed every worker in the
#:      second outage. Refusing to act says nothing about the schema.
#:   3  drift: a file changed after it was applied. A real problem, and a
#:      developer-hygiene one -- the running schema is whatever it was, and
#:      an edited comment in an applied migration must not take production
#:      down. main.py's check still catches a genuinely BEHIND schema.
BLOCKING_EXIT_CODES = frozenset({1})


def migrations_enabled(environ: Optional[dict] = None) -> bool:
    """Whether to migrate at all. On unless explicitly switched off."""
    raw = (environ or os.environ).get(RUN_MIGRATIONS_ENV, "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def should_start(code: Optional[int]) -> bool:
    """Given migrate.main()'s exit code, may the app start?

    `None` means the runner raised rather than returning -- the database was
    unreachable, or something in it broke unexpectedly. That is the "could
    not determine" case, and it starts: see the module docstring.
    """
    if code is None:
        return True
    return code not in BLOCKING_EXIT_CODES


def apply_pending() -> Optional[int]:
    """Run the migrations, returning migrate.main()'s code, or None if it raised.

    `--allow-pooler` is not optional here, it is the only thing that works.
    This deployment's DATABASE_URL is Supabase's transaction pooler on 6543,
    and migrate.py refuses DDL through that by default with good reason. The
    alternatives are both dead ends on this project: the session pooler on
    5432 times out, and the direct host does not resolve. So the flag the
    trader already passes by hand is the flag the container passes too --
    same command, same path, no second code route that only production takes.
    """
    if not migrations_enabled():
        print(f"[start] {RUN_MIGRATIONS_ENV} is off; skipping migrations.")
        return 0

    # Imported here rather than at module scope so that a broken or missing
    # runner cannot stop this file from exec'ing gunicorn. The whole point is
    # that the app still starts when the migration step cannot.
    import migrate  # noqa: PLC0415

    try:
        return migrate.main(["--allow-pooler"])
    except Exception:  # noqa: BLE001 - deliberately broad; see the docstring
        print(
            "[start] The migration runner raised, so whether the schema is "
            "current is unknown. Starting anyway -- an unreachable database "
            "is reported by /health and recovers on its own, and main.py's "
            "boot check still refuses a schema it can prove is behind.",
            file=sys.stderr,
        )
        traceback.print_exc()
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Migrate, then become the command in argv (gunicorn, from the CMD line).

    os.execvp replaces this process rather than spawning a child, so gunicorn
    keeps PID 1 and receives the container's signals directly. A wrapper left
    in the middle would have to forward SIGTERM itself to avoid turning every
    graceful shutdown into a kill.
    """
    command = list(argv if argv is not None else sys.argv[1:])

    code = apply_pending()
    if not should_start(code):
        print(
            "\n[start] Refusing to start: a migration failed partway through, "
            "so the schema is between two shapes. The error above names the "
            "file. Nothing after it has run, and this container will not "
            "serve traffic until it does.",
            file=sys.stderr,
        )
        return 1

    if not command:
        print("[start] No command to exec.", file=sys.stderr)
        return 2

    # Flush before exec, or the migration log is thrown away.
    #
    # execvp REPLACES this process: anything still sitting in Python's stdout
    # buffer is never written. Caught by running this file locally, where
    # "Applied 1 migration(s)" simply never appeared -- the migration had run,
    # and the only record that it had was gone. The Dockerfile does set
    # PYTHONUNBUFFERED, which hides this in the container, but the log of what
    # the schema just did is not something to leave resting on an environment
    # variable staying set.
    sys.stdout.flush()
    sys.stderr.flush()

    os.execvp(command[0], command)
    # Unreachable: execvp either replaces this process or raises.
    return 0  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
