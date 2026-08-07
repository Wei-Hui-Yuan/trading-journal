"""The migration runner's decisions, tested without a database.

Everything that decides WHICH migrations run and IN WHAT ORDER is pure, so it
can be tested directly. That is the part worth guarding: applying SQL is
Postgres's job, but choosing to apply the wrong file, twice, or in the wrong
order is this module's, and those are the failures that corrupt a schema
quietly.

The last group runs against the real api/migrations directory, so the checks
that protect the sequence are exercised on the actual sequence rather than on
fixtures that agree with them by construction.
"""

import pytest

import migrate
from migrate import (
    GRANDFATHERED_DUPLICATES,
    Migration,
    MigrationError,
    check_prefix_collisions,
    checksum,
    drifted,
    load_migrations,
    normalise,
    pending,
    sort_key,
)


def _migration(filename: str, sql: str = "SELECT 1;") -> Migration:
    return Migration(filename=filename, sql=sql, checksum=checksum(sql))


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------


def test_normalise_flattens_both_line_ending_conventions():
    assert normalise("a\r\nb\rc\nd") == "a\nb\nc\nd"


def test_checksum_ignores_line_endings():
    """Otherwise the checksum describes the checkout, not the migration.

    Git hands a Windows machine CRLF and the Linux container LF for the same
    commit. Without normalising, every migration would read as edited-since-
    applied on whichever platform did not write the row, and the drift check
    would cry wolf until it was ignored entirely.
    """
    assert checksum("CREATE TABLE t ();\n") == checksum("CREATE TABLE t ();\r\n")


def test_checksum_still_notices_a_real_edit():
    assert checksum("ALTER TABLE t ADD COLUMN a INT;") != checksum(
        "ALTER TABLE t ADD COLUMN b INT;"
    )


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_sort_key_orders_numerically_not_lexically():
    """9 before 10. String sorting would put 010 before 009 only by luck of
    zero-padding, and the padding is a convention nothing enforces."""
    names = ["010_ten.sql", "009_nine.sql", "100_hundred.sql"]
    assert sorted(names, key=sort_key) == [
        "009_nine.sql",
        "010_ten.sql",
        "100_hundred.sql",
    ]


def test_sort_key_breaks_ties_on_the_full_name():
    """The 021 pair has to have SOME total order, and it has to be the same one
    every time and on every machine -- otherwise two environments built from the
    same files disagree about what ran first."""
    a, b = "021_drop_redundant_position_fill_indexes.sql", "021_timeframe_presets.sql"
    assert sort_key(a) < sort_key(b)
    assert sorted([b, a], key=sort_key) == [a, b]


def test_sort_key_rejects_a_file_with_no_number():
    with pytest.raises(MigrationError, match="numeric prefix"):
        sort_key("add_a_column.sql")


# ---------------------------------------------------------------------------
# Collision detection -- the check that stops this recurring
# ---------------------------------------------------------------------------


def test_clean_sequence_passes():
    check_prefix_collisions(["001_a.sql", "002_b.sql", "003_c.sql"])


def test_the_historical_021_and_022_pairs_are_allowed():
    """They predate the runner and are recorded as fact. Renaming an applied
    migration would change its identity, which is the one thing a migration's
    filename must never do."""
    check_prefix_collisions(sorted(GRANDFATHERED_DUPLICATES))


def test_a_new_duplicate_prefix_is_rejected():
    """The whole point. A third file numbered 021 has no defined position
    relative to the two that already exist."""
    with pytest.raises(MigrationError, match="021"):
        check_prefix_collisions(
            sorted(GRANDFATHERED_DUPLICATES) + ["021_something_new.sql"]
        )


def test_a_duplicate_on_a_fresh_prefix_is_rejected():
    with pytest.raises(MigrationError, match="030"):
        check_prefix_collisions(["030_first.sql", "030_second.sql"])


def test_collision_error_names_both_files():
    """An error that says only "there is a collision" sends you looking for it.
    This one should be actionable on its own."""
    with pytest.raises(MigrationError) as exc:
        check_prefix_collisions(["030_first.sql", "030_second.sql"])
    assert "030_first.sql" in str(exc.value)
    assert "030_second.sql" in str(exc.value)


# ---------------------------------------------------------------------------
# What is pending, what has drifted
# ---------------------------------------------------------------------------


def test_pending_excludes_what_is_already_recorded():
    migrations = [_migration("001_a.sql"), _migration("002_b.sql")]
    assert [m.filename for m in pending(migrations, {"001_a.sql"})] == ["002_b.sql"]


def test_pending_is_keyed_on_filename_not_number():
    """Both 021s must be tracked independently. Keying on the prefix would let
    one of them mark the other as done."""
    migrations = [
        _migration("021_drop_redundant_position_fill_indexes.sql"),
        _migration("021_timeframe_presets.sql"),
    ]
    still = pending(migrations, {"021_drop_redundant_position_fill_indexes.sql"})
    assert [m.filename for m in still] == ["021_timeframe_presets.sql"]


def test_pending_preserves_order():
    migrations = [_migration(f"{i:03d}_m.sql") for i in range(1, 6)]
    assert [m.filename for m in pending(migrations, {"002_m.sql"})] == [
        "001_m.sql",
        "003_m.sql",
        "004_m.sql",
        "005_m.sql",
    ]


def test_drift_is_detected_when_an_applied_file_changes():
    migration = _migration("001_a.sql", "SELECT 1;")
    assert drifted([migration], {"001_a.sql": checksum("SELECT 2;")}) == ["001_a.sql"]


def test_no_drift_when_the_file_is_untouched():
    migration = _migration("001_a.sql", "SELECT 1;")
    assert drifted([migration], {"001_a.sql": checksum("SELECT 1;")}) == []


def test_baselined_rows_are_not_reported_as_drifted():
    """A NULL checksum means the row was adopted from a hand-migrated database.
    There is no record of what actually ran, so there is nothing to compare
    against -- and guessing would flag all 31 existing migrations on the first
    run after adoption."""
    migration = _migration("001_a.sql", "SELECT 1;")
    assert drifted([migration], {"001_a.sql": None}) == []


def test_unapplied_migrations_are_not_drift():
    migration = _migration("001_a.sql")
    assert drifted([migration], {}) == []


# ---------------------------------------------------------------------------
# Transaction handling
# ---------------------------------------------------------------------------


def test_a_file_opening_with_begin_manages_its_own_transaction():
    assert _migration("001_a.sql", "BEGIN;\nDROP INDEX x;\nCOMMIT;").manages_own_transaction


def test_plpgsql_begin_inside_a_do_block_is_not_a_transaction():
    """022_positions_direction.sql opens a DO block whose body starts with
    BEGIN. Reading that as transaction management would leave the migration
    unwrapped and its bookkeeping non-atomic."""
    sql = "DO $$\nDECLARE x INT;\nBEGIN\n  x := 1;\nEND $$;"
    assert not _migration("001_a.sql", sql).manages_own_transaction


def test_a_plain_file_does_not_manage_its_own_transaction():
    assert not _migration("001_a.sql", "ALTER TABLE t ADD COLUMN a INT;").manages_own_transaction


def test_the_no_transaction_marker_is_honoured():
    sql = "-- migrate: no-transaction\nCREATE INDEX CONCURRENTLY i ON t (a);"
    assert _migration("001_a.sql", sql).opts_out_of_transaction


# ---------------------------------------------------------------------------
# The real directory
# ---------------------------------------------------------------------------


def test_the_real_migration_sequence_loads():
    """Guards the actual files against the actual rules. If someone adds a
    migration with a colliding number or no number at all, this fails in CI
    rather than on the database."""
    migrations = load_migrations()
    assert len(migrations) >= 31


def test_the_real_sequence_is_strictly_ordered():
    migrations = load_migrations()
    keys = [sort_key(m.filename) for m in migrations]
    assert keys == sorted(keys)


def test_the_real_sequence_has_no_ungrandfathered_collisions():
    check_prefix_collisions([m.filename for m in load_migrations()])


def test_the_two_known_duplicate_pairs_are_all_present():
    """If one of these is ever renamed the exception list is stale, and a stale
    allowlist silently permits a collision it was never meant to cover."""
    on_disk = {m.filename for m in load_migrations()}
    assert GRANDFATHERED_DUPLICATES <= on_disk


def test_migration_000_sorts_before_001():
    """000_bootstrap_hand_created_tables.sql recreates trades and strategies --
    hand-created in Supabase before this directory existed, and never CREATEd
    by any numbered migration. It has to run first, or 001's foreign key into
    trades fails on a table that is not there yet."""
    migrations = load_migrations()
    assert migrations[0].filename == "000_bootstrap_hand_created_tables.sql"


def test_migration_000_is_entirely_guarded():
    """The whole reason it is safe to run against production, where both
    tables already exist: every statement has to be CREATE ... IF NOT EXISTS
    or ADD COLUMN IF NOT EXISTS, so applying it there does nothing rather than
    erroring on a duplicate table or column."""
    bootstrap = load_migrations()[0]
    statements = bootstrap.statements.upper()
    assert "CREATE TABLE IF NOT EXISTS" in statements
    assert "ADD COLUMN IF NOT EXISTS" in statements
    # A bare CREATE TABLE (no guard) or ADD COLUMN (no guard) would fail
    # loudly on a database that already has these tables -- which is every
    # database except a brand new one, including production.
    import re

    bare_create = re.search(r"CREATE TABLE(?! IF NOT EXISTS)\s", statements)
    assert bare_create is None, "found an unguarded CREATE TABLE in migration 000"


def test_every_real_migration_checksums_distinctly():
    """Two identical migrations would be a copy-paste mistake worth catching."""
    migrations = load_migrations()
    assert len({m.checksum for m in migrations}) == len(migrations)


def test_migrations_that_manage_their_own_transaction_are_left_alone():
    """A file opening with BEGIN; must not be wrapped again -- its COMMIT would
    close the runner's transaction and the recording INSERT would fall outside
    it. Asserts the split is recognised on the real files rather than on a
    fixture."""
    migrations = load_migrations()
    own = [m.filename for m in migrations if m.manages_own_transaction]
    assert "021_drop_redundant_position_fill_indexes.sql" in own
    assert "022_positions_direction.sql" in own
    # And a file with no transaction of its own is wrapped by the runner.
    wrapped = [m.filename for m in migrations if not m.manages_own_transaction]
    assert "001_create_positions.sql" in wrapped


def test_no_current_migration_needs_the_no_transaction_escape_hatch():
    """None of the 31 EXECUTE anything CONCURRENTLY. If one ever does, it must
    carry the marker, and this test is the reminder.

    Read against `statements` rather than the raw file: migration 021 discusses
    DROP INDEX CONCURRENTLY in a comment explaining why it did not need it, and
    a check that cannot tell prose from SQL would fail on the explanation.
    """
    for migration in load_migrations():
        if "CONCURRENTLY" in migration.statements.upper():
            assert migration.opts_out_of_transaction, (
                f"{migration.filename} uses CONCURRENTLY, which cannot run "
                f"inside a transaction. Add '{migrate._NO_TRANSACTION_MARKER}'."
            )


def test_comment_stripping_distinguishes_prose_from_sql():
    """The distinction the test above depends on, asserted directly."""
    discussed = _migration("x.sql", "-- DROP INDEX CONCURRENTLY is an option\nDROP INDEX i;")
    assert "CONCURRENTLY" not in discussed.statements
    used = _migration("y.sql", "DROP INDEX CONCURRENTLY i;")
    assert "CONCURRENTLY" in used.statements


def test_a_commented_out_begin_is_not_transaction_management():
    sql = "-- BEGIN; was here once\nALTER TABLE t ADD COLUMN a INT;"
    assert not _migration("x.sql", sql).manages_own_transaction
