"""The chart screenshot attached to a plan.

A plan records levels and a thesis -- what you intended. For a discretionary
setup that is half the decision: "reclaiming the 50 EMA after basing three
days" is a sentence, and whether the base was there is a picture. Without it,
reviewing the trade grades the sentence rather than the decision.

The bytes go to Supabase Storage rather than Postgres, and `planned_trades`
keeps the key (migration 025). Two things about that split are load-bearing
and pinned below:

  * The four chart columns move together. A row holding a path but no size is
    a half-written attachment -- it reads as "there is a chart" to anything
    checking one column, and the endpoint checks `chart_path`.
  * Storage is written BEFORE the database on upload, and also before it on
    delete. Both orders are chosen so the surviving failure mode is an object
    nothing points at (harmless, and overwritten by the next upload because
    the key is derived from the plan id) rather than a row pointing at bytes
    that were never stored (a broken image indistinguishable from a slow one).
"""

import inspect
import os
import uuid

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402
from services import storage  # noqa: E402


# ---------------------------------------------------------------------------
# What the API will accept
# ---------------------------------------------------------------------------


def test_only_the_two_formats_the_compressor_produces_are_accepted():
    """WebP and PNG are what lib/chartImage.ts emits, and both are lossless
    in the paths it uses. Accepting JPEG here would let a client skip the
    compressor and store a blurred chart -- the opposite of the point."""
    assert set(main.CHART_MIME_EXTENSIONS) == {"image/webp", "image/png"}


def test_the_extension_follows_the_mime_so_objects_are_self_describing():
    assert main.CHART_MIME_EXTENSIONS["image/webp"] == "webp"
    assert main.CHART_MIME_EXTENSIONS["image/png"] == "png"


def test_the_size_ceiling_is_generous_but_bounded():
    """A losslessly-encoded chart measures in the hundreds of kilobytes.
    The cap is not a target -- it is what stops one request taking a worker's
    memory with it."""
    assert main.CHART_MAX_BYTES == 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# The ETag -- what makes a re-view free without making a replacement invisible
# ---------------------------------------------------------------------------


class _Plan:
    def __init__(self, plan_id, uploaded_at, chart_bytes):
        self.id = plan_id
        self.chart_uploaded_at = uploaded_at
        self.chart_bytes = chart_bytes


def test_the_etag_changes_when_the_image_is_replaced():
    """The failure this prevents: the storage key is derived from the plan id,
    so it is byte-identical across a replacement. An ETag built from the path
    alone would keep serving the OLD screenshot from cache forever."""
    from datetime import datetime, timezone

    plan_id = uuid.uuid4()
    first = _Plan(plan_id, datetime(2026, 8, 1, tzinfo=timezone.utc), 120_000)
    second = _Plan(plan_id, datetime(2026, 8, 2, tzinfo=timezone.utc), 120_000)

    assert main._chart_etag(first) != main._chart_etag(second)


def test_the_etag_is_stable_for_an_unchanged_image():
    """Otherwise every view re-downloads, which is what the header exists to
    avoid."""
    from datetime import datetime, timezone

    plan_id = uuid.uuid4()
    at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert main._chart_etag(_Plan(plan_id, at, 120_000)) == main._chart_etag(
        _Plan(plan_id, at, 120_000)
    )


def test_a_resized_replacement_at_the_same_moment_still_differs():
    """Size is in the tag too, so two writes that somehow share a timestamp
    are still distinguishable."""
    from datetime import datetime, timezone

    plan_id = uuid.uuid4()
    at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert main._chart_etag(_Plan(plan_id, at, 120_000)) != main._chart_etag(
        _Plan(plan_id, at, 90_000)
    )


# ---------------------------------------------------------------------------
# Ordering: which write lands first, and why
# ---------------------------------------------------------------------------


def test_upload_writes_storage_before_the_database():
    """Reversed, a commit could record a chart whose bytes never arrived --
    a broken image with nothing to distinguish it from a slow one."""
    source = inspect.getsource(main.upload_plan_chart)
    assert source.index("storage.upload(") < source.index("session.commit()")


def test_upload_removes_a_superseded_object_when_the_extension_changes():
    """Replacing a PNG with a WebP writes a different key, so the old object
    would otherwise linger with nothing pointing at it, consuming the bucket
    quota invisibly."""
    source = inspect.getsource(main.upload_plan_chart)
    assert "previous" in source
    assert "storage.delete(previous)" in source


def test_delete_removes_the_object_before_clearing_the_row():
    """The other order leaves bytes in the bucket that the user can no longer
    see or remove -- quota consumed by something invisible. This order's
    failure mode is a row the GET reports honestly."""
    source = inspect.getsource(main.delete_plan_chart)
    assert source.index("storage.delete(") < source.index("chart_path = None")


def test_a_failed_replacement_cleanup_does_not_fail_the_upload():
    """The new chart is stored and recorded by then. Failing the request would
    report a successful upload as an error."""
    source = inspect.getsource(main.upload_plan_chart)
    tail = source[source.index("storage.delete(previous)"):]
    assert "logger.warning" in tail


# ---------------------------------------------------------------------------
# Storage configuration -- an unset key is a 503, not a 500
# ---------------------------------------------------------------------------


def test_missing_credentials_report_which_ones_and_how_to_fix_it(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    with pytest.raises(storage.StorageError) as caught:
        storage._config()

    assert caught.value.status == 503, "actionable by the operator, not a crash"
    message = str(caught.value)
    assert "SUPABASE_URL" in message and "SUPABASE_SERVICE_ROLE_KEY" in message
    assert storage.BUCKET in message


def test_a_partially_configured_server_still_reports_the_missing_half(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    with pytest.raises(storage.StorageError) as caught:
        storage._config()

    assert "SUPABASE_SERVICE_ROLE_KEY" in str(caught.value)
    assert "SUPABASE_URL" not in str(caught.value), "already set; naming it misleads"


def test_is_configured_answers_without_raising(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert storage.is_configured() is False

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-value")
    assert storage.is_configured() is True


def test_credentials_are_read_per_call_not_captured_at_import(monkeypatch):
    """A server started before the key was set must work after a restart that
    supplies it -- binding once at import would keep reporting it missing."""
    monkeypatch.setenv("SUPABASE_URL", "https://later.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "set-after-import")
    base, key = storage._config()
    assert base == "https://later.supabase.co"
    assert key == "set-after-import"


def test_a_trailing_slash_does_not_produce_a_double_slash_url(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    base, _ = storage._config()
    assert storage._object_url(base, "abc.webp") == (
        f"https://example.supabase.co/storage/v1/object/{storage.BUCKET}/abc.webp"
    )


# ---------------------------------------------------------------------------
# What the plan payload tells the UI
# ---------------------------------------------------------------------------


def test_the_storage_key_is_never_sent_to_the_browser():
    """The bucket is private, so the path is useless to a client -- and it is
    an internal layout detail that would become an API contract the moment
    something rendered it."""
    assert "chart_path" not in main.PlanOut.model_fields
    assert "has_chart" in main.PlanOut.model_fields
    assert "chart_bytes" in main.PlanOut.model_fields


def test_the_round_trip_says_whether_its_plan_has_a_chart():
    """So the ledger renders the image slot only where there is one, instead
    of requesting it for every planned trade and taking a 404 to find out."""
    assert "plan_has_chart" in main.RoundTripOut.model_fields
    assert main.RoundTripOut.model_fields["plan_has_chart"].default is False
