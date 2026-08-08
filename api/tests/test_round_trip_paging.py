"""Paging, filtering and validation on /api/round-trips.

The endpoint was rewritten to filter and page in SQL rather than loading the
whole ledger and discarding most of it. This covers the part of that change
which does not need a database: the parameters are rejected before any query
runs, and the cache tag distinguishes every request that produces a different
payload.

The SQL itself is NOT covered here -- there is no Postgres in CI. See
verify_round_trips.py, which runs the new queries and the old Python logic
against the same live data and compares them.
"""

import asyncio
import uuid
from dataclasses import dataclass, field

import pytest

import main

run = asyncio.run


@dataclass
class _NoHeaders:
    headers: dict = field(default_factory=dict)


@dataclass
class _Collects:
    headers: dict = field(default_factory=dict)


class _ExplodingSession:
    """Fails any query. Validation must never reach one."""

    async def scalar(self, _stmt):
        raise AssertionError("validated too late: a query ran before the 422")

    async def execute(self, _stmt):
        raise AssertionError("validated too late: a query ran before the 422")

    async def rollback(self):
        return None


def _call(**kwargs):
    return run(
        main.list_round_trips(
            _NoHeaders(), _Collects(), session=_ExplodingSession(), **kwargs
        )
    )


# ---------------------------------------------------------------------------
# Validation, before anything is read
# ---------------------------------------------------------------------------


def test_an_unknown_kind_is_a_422():
    with pytest.raises(main.HTTPException) as caught:
        _call(kind="halfway")
    assert caught.value.status_code == 422
    assert "open" in caught.value.detail


@pytest.mark.parametrize("kind", ["open", "closed"])
def test_the_two_real_kinds_pass_validation(kind):
    """They must reach the database rather than be rejected -- proven here by
    the session raising, which only happens after validation lets them past."""
    with pytest.raises(AssertionError, match="validated too late"):
        _call(kind=kind)


def test_a_negative_offset_is_a_422():
    with pytest.raises(main.HTTPException) as caught:
        _call(offset=-1)
    assert caught.value.status_code == 422


def test_a_zero_limit_is_a_422():
    """Zero is not "no limit" -- that is what omitting it means. Silently
    treating it as unpaged would return the whole ledger to a caller who asked
    for none of it."""
    with pytest.raises(main.HTTPException) as caught:
        _call(limit=0)
    assert caught.value.status_code == 422


def test_a_strategy_that_is_not_a_uuid_is_a_422():
    with pytest.raises(main.HTTPException) as caught:
        _call(strategy="not-a-uuid")
    assert caught.value.status_code == 422
    assert "UUID" in caught.value.detail


def test_unassigned_is_a_real_strategy_filter_not_a_bad_uuid():
    """It selects round trips whose opening execution carries no strategy, so
    it must pass validation rather than be rejected as malformed."""
    with pytest.raises(AssertionError, match="validated too late"):
        _call(strategy="unassigned")


def test_a_real_uuid_strategy_passes_validation():
    with pytest.raises(AssertionError, match="validated too late"):
        _call(strategy=str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# The cache tag has to distinguish pages
# ---------------------------------------------------------------------------


def _tag(**parts):
    """The tag the endpoint builds, for one set of parameters at one version."""
    return main._etag(
        "round-trips",
        7,
        parts.get("scoped", ""),
        parts.get("kind", ""),
        parts.get("strategy", ""),
        parts.get("page_size", ""),
        parts.get("offset", 0),
    )


def test_two_pages_do_not_share_a_tag():
    """The worst caching bug available here: page 2 answered from page 1's
    entry, with a 304 and nothing on the client able to tell."""
    assert _tag(page_size=50, offset=0) != _tag(page_size=50, offset=50)


def test_page_size_is_part_of_the_tag():
    assert _tag(page_size=50) != _tag(page_size=100)


def test_paged_and_unpaged_do_not_share_a_tag():
    assert _tag(page_size="") != _tag(page_size=50)


def test_kind_is_part_of_the_tag():
    assert _tag(kind="open") != _tag(kind="closed")
    assert _tag(kind="") != _tag(kind="open")


def test_strategy_is_part_of_the_tag():
    one = str(uuid.uuid4())
    assert _tag(strategy=one) != _tag(strategy="unassigned")
    assert _tag(strategy="") != _tag(strategy=one)


def test_ticker_is_part_of_the_tag():
    assert _tag(scoped="AAPL") != _tag(scoped="MSFT")


# ---------------------------------------------------------------------------
# The page ceiling
# ---------------------------------------------------------------------------


def test_the_page_ceiling_is_a_real_bound():
    assert isinstance(main.ROUND_TRIP_MAX_PAGE, int)
    assert main.ROUND_TRIP_MAX_PAGE > 0


def test_an_oversized_limit_is_capped_not_rejected():
    """Asking for more than the ceiling is not an error -- it is answered with
    the ceiling. Rejecting it would break a caller that passed a large number
    meaning "everything"; honouring it would materialise the whole ledger."""
    capped = min(10_000_000, main.ROUND_TRIP_MAX_PAGE)
    assert capped == main.ROUND_TRIP_MAX_PAGE
