"""ETag construction and If-None-Match matching.

The parts that decide whether a client gets a 304. They are pure, so they can
be tested directly -- and they are worth testing precisely because both failure
directions are silent. A tag that collides serves one payload in answer to a
request for another; a tag that never matches turns the whole mechanism into
dead weight that still costs a round trip.

The database-backed half (`_journal_version` reading the counter, and the
triggers that bump it) is not covered here -- there is no Postgres in CI.
"""

from datetime import date

import pytest

from main import _cache_headers, _etag, _not_modified, CONDITIONAL_CACHE_CONTROL


class _Request:
    """Just enough of a Request for the header lookups under test."""

    def __init__(self, **headers):
        self.headers = {k.replace("_", "-").lower(): v for k, v in headers.items()}


# ---------------------------------------------------------------------------
# Tag construction
# ---------------------------------------------------------------------------


def test_same_inputs_produce_the_same_tag():
    assert _etag("dashboard", 7, date(2026, 1, 1)) == _etag(
        "dashboard", 7, date(2026, 1, 1)
    )


def test_a_new_version_produces_a_new_tag():
    """The whole mechanism. A write bumps the counter; the tag must move with
    it or the browser keeps serving the pre-write payload indefinitely."""
    assert _etag("dashboard", 7) != _etag("dashboard", 8)


def test_different_windows_do_not_share_a_tag():
    """A 1Y dashboard and an ALL dashboard are different payloads at the same
    version. Sharing a tag would serve one in answer to the other."""
    one_year = _etag("dashboard", 7, date(2025, 1, 1), date(2026, 1, 1))
    all_time = _etag("dashboard", 7, None, None)
    assert one_year != all_time


def test_different_endpoints_do_not_share_a_tag():
    """Scope is part of the tag, so the dashboard and the advanced payload
    cannot collide at the same version."""
    assert _etag("dashboard", 7) != _etag("advanced", 7)


def test_round_trip_ticker_filters_do_not_share_a_tag():
    assert _etag("round-trips", 7, "AAPL") != _etag("round-trips", 7, "MSFT")


def test_unfiltered_is_distinct_from_any_filter():
    assert _etag("round-trips", 7, "") != _etag("round-trips", 7, "AAPL")


def test_tag_is_quoted_and_fixed_length():
    """An ETag must be a quoted string per RFC 9110. Fixed length because the
    inputs are hashed rather than concatenated, so a long custom range cannot
    produce an unbounded header."""
    short = _etag("advanced", 1)
    long = _etag("dashboard", 999999, date(1970, 1, 1), date(2099, 12, 31), "x" * 500)
    assert short.startswith('"') and short.endswith('"')
    assert len(short) == len(long)


def test_tag_contains_no_quote_that_would_break_the_header():
    """Hashing is what guarantees this; a concatenated tag carrying a raw
    parameter could terminate the header early."""
    assert '"' not in _etag("dashboard", 1, 'evil" tag')[1:-1]


# ---------------------------------------------------------------------------
# If-None-Match
# ---------------------------------------------------------------------------


def test_no_header_is_not_a_match():
    assert not _not_modified(_Request(), '"abc"')


def test_exact_match():
    assert _not_modified(_Request(if_none_match='"abc"'), '"abc"')


def test_different_tag_is_not_a_match():
    assert not _not_modified(_Request(if_none_match='"abc"'), '"def"')


def test_a_list_of_tags_matches_on_any_member():
    """If-None-Match is a list, not a single value. A client holding several
    variants offers all of them, and comparing the raw header would miss every
    one after the first."""
    request = _Request(if_none_match='"aaa", "bbb", "ccc"')
    assert _not_modified(request, '"bbb"')
    assert _not_modified(request, '"ccc"')
    assert not _not_modified(request, '"ddd"')


def test_whitespace_around_list_entries_is_tolerated():
    assert _not_modified(_Request(if_none_match='  "aaa" ,   "bbb"  '), '"bbb"')


def test_star_matches_anything():
    """RFC 9110: `*` means "any current representation"."""
    assert _not_modified(_Request(if_none_match="*"), '"whatever"')


def test_weak_tags_still_match():
    """A proxy may weaken a tag in transit. For a 304 the weak comparison is
    the correct one, so W/"abc" must satisfy a request for "abc"."""
    assert _not_modified(_Request(if_none_match='W/"abc"'), '"abc"')


def test_empty_header_is_not_a_match():
    assert not _not_modified(_Request(if_none_match=""), '"abc"')


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def test_headers_carry_the_tag_and_revalidation_policy():
    headers = _cache_headers('"abc"')
    assert headers["ETag"] == '"abc"'
    assert headers["Cache-Control"] == CONDITIONAL_CACHE_CONTROL


def test_cache_control_is_private():
    """These responses are per-user and the request carries a bearer token. A
    shared cache holding one would serve it to somebody else."""
    assert "private" in CONDITIONAL_CACHE_CONTROL


def test_cache_control_forces_revalidation():
    """`no-cache` means "store, but check before reuse" -- not "do not store".
    Without it the browser could reuse a stale payload for its heuristic
    freshness lifetime without ever asking."""
    assert "no-cache" in CONDITIONAL_CACHE_CONTROL


def test_no_version_means_no_caching_headers_at_all():
    """When the counter cannot be read -- migration 030 not yet applied -- the
    response must go out untagged rather than carrying a validator that is not
    backed by anything."""
    assert _cache_headers(None) == {}


@pytest.mark.parametrize("version", [0, 1, 2**62])
def test_any_version_value_produces_a_usable_tag(version):
    """Including 0, which is what a freshly seeded counter holds, and which a
    truthiness check would wrongly treat as absent."""
    tag = _etag("dashboard", version)
    assert tag.startswith('"') and len(tag) > 2
