"""Writing a note must not silently complete a review nobody did.

`PATCH /api/positions/{id}/review` backs two different actions: completing
the Trade Inbox checklist, and the Analytics drawer jotting `notes` and
`mistakes` on a trade that may not have been looked at yet. The handler used
to end with an unconditional

    position.review_status = ReviewStatus.reviewed.value

so the drawer's save button cleared the trade from the Trade Inbox queue as a
side effect nothing in its UI announced -- the opposite of what the payload
model's own docstring promised ("one surface never clears the other's work").

`mark_reviewed` makes the two actions distinguishable at the wire: the
Trade Inbox checklist and the Trade Ledger's post-mortem both rely on the
default of `True`, so neither had to change. The Analytics drawer is the one
caller that sends `mark_reviewed=False`.
"""

import inspect
import os

import pytest

os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main  # noqa: E402


# ---------------------------------------------------------------------------
# The payload model
# ---------------------------------------------------------------------------


def test_mark_reviewed_defaults_true():
    """Every existing caller (Trade Inbox, Trade Ledger) omits the field, so
    the default has to preserve today's "completing the checklist marks it
    reviewed" behaviour without any of them changing."""
    assert main.PositionReviewUpdate().mark_reviewed is True


def test_mark_reviewed_can_opt_out():
    assert main.PositionReviewUpdate(mark_reviewed=False).mark_reviewed is False


def test_notes_only_payload_still_defaults_to_marking_reviewed():
    """Documents the bug's shape rather than the fix: a payload that carries
    only `notes`/`mistakes` and omits `mark_reviewed` entirely still marks the
    position reviewed, by design of the default. The Analytics drawer is
    required to send `mark_reviewed=False` explicitly -- the field existing is
    not, by itself, protection against a caller that forgets to set it."""
    payload = main.PositionReviewUpdate(notes="chased this one", mistakes=["FOMO"])
    assert payload.mark_reviewed is True


# ---------------------------------------------------------------------------
# The endpoint: the flag gates the assignment, and never leaks onto the row
# ---------------------------------------------------------------------------


def test_the_guard_is_conditional_on_the_flag():
    """Pins the shape of the fix: `review_status` is set inside an `if
    params.mark_reviewed:`, not unconditionally."""
    source = inspect.getsource(main.review_position)
    assert "if params.mark_reviewed:" in source
    assert "position.review_status = ReviewStatus.reviewed.value" in source
    # And not sitting bare at the function's top level (unconditional again).
    unconditional = "\n    position.review_status = ReviewStatus.reviewed.value"
    assert unconditional not in source


def test_mark_reviewed_is_popped_before_the_setattr_loop():
    """Not a Position column -- like `disciplines`, it has to be pulled out of
    `updates` before the generic `setattr(position, field, value)` loop, or it
    becomes a stray attribute on the ORM object that never reaches the
    database and never gets cleaned up."""
    source = inspect.getsource(main.review_position)
    assert 'updates.pop("mark_reviewed", None)' in source


def test_docstrings_no_longer_promise_an_unconditional_completion():
    """The Analytics drawer's notes/mistakes payload is explicitly called out
    as the case that opts out, so the invariant is written down somewhere a
    future caller can find it."""
    assert "mark_reviewed" in (main.review_position.__doc__ or "")
    assert "mark_reviewed" in (main.ReviewStatus.__doc__ or "")
