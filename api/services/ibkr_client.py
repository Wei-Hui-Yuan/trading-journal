"""IBKR Flex Web Service client.

The Flex service is a two-step handshake: `SendRequest` asks IBKR to compile a
saved query and returns a `ReferenceCode`, then `GetStatement` downloads the
report once compilation finishes. Compilation is not instant, so GetStatement
is polled until the payload arrives or the attempt budget runs out.
"""

from __future__ import annotations

import asyncio
import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

FLEX_BASE = "https://ndcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService"
SEND_REQUEST_URL = f"{FLEX_BASE}.SendRequest"
GET_STATEMENT_URL = f"{FLEX_BASE}.GetStatement"

# Transient conditions. All of them mean "wait and ask again", never "give up":
#   1001 - statement could not be generated at this time (server busy/throttled)
#   1018 - too many requests have been made
#   1019 - statement generation in progress
#
# 1001 is the one that bites when several queries run in a row: the Flex
# service rate-limits per token, so the second request is refused. Treated as
# fatal it fails the whole sync over a condition that clears in seconds.
NOT_READY_CODES = {"1001", "1018", "1019"}

# Of those, only "generation in progress" is worth retrying inside the request.
# 1001 and 1018 are rate limiting, whose cooldown is measured in minutes and
# can be *restarted* by asking again -- retrying there lengthens the lockout it
# is trying to escape. Those are reported to the caller to retry later instead.
RETRY_IN_REQUEST_CODES = {"1019"}

def is_transient_failure(message: str) -> bool:
    """Whether a failure message names a condition that clears on its own.

    Lives here so the codes stay in one place. The caller needs this to tell
    the user whether retrying is worth anything: a throttled query is a "try
    again in a few minutes", while a bad token or unknown query id will fail
    identically forever and retrying only wastes the request budget.

    Matches on the `(code NNNN)` fragment that request_statement and
    download_statement embed, not on a bare number, so a quantity or a price
    that happens to read 1001 cannot be mistaken for a rate limit.
    """
    return any(f"code {code}" in message for code in NOT_READY_CODES)


# ---------------------------------------------------------------------------
# What IBKR actually said, and whether waiting can fix it
# ---------------------------------------------------------------------------
#
# Deliberately SEPARATE from NOT_READY_CODES above, which is deliberately
# minimal because it drives retry CONTROL FLOW. This table drives what a person
# is told, and the two are different decisions: widening the retry set to every
# code below would change when the app hammers IBKR, which is not the goal.
#
# Categories answer one question -- what would you have to change to make this
# stop? A code that needs a new token and a code that needs a different time of
# day are both "the sync failed", and telling them apart is the whole point:
# reporting an expired token as "try again shortly" sends the user to wait for
# something that will never happen on its own.
FAILURE_WAIT = "wait"
FAILURE_QUERY = "query"
FAILURE_TOKEN = "token"
FAILURE_REQUEST = "request"

# code -> (what it means, category, what to do about it)
#
# IBKR's own ErrorMessage is always carried alongside these, so a label here
# being less precise than IBKR's wording cannot mislead -- the source of truth
# travels with the interpretation.
FLEX_CODES: dict[str, tuple[str, str, str]] = {
    "1001": (
        "IBKR could not generate the statement at this time",
        FAILURE_WAIT,
        # Deliberately names no hour. Measured only that it fails in the ET
        # small hours and works mid-morning ET; the boundary between those is
        # unverified, and a confident wrong cutoff is worse than none.
        "Activity statements are compiled on a daily cycle in New York time. "
        "Asked for before that day's statement exists, this is the refusal you "
        "get. It usually clears once the US morning is under way -- so this is "
        "worth retrying later in the day, not in a few minutes.",
    ),
    "1003": (
        "statement is not available",
        FAILURE_QUERY,
        "The account or period this query names has no statement to return.",
    ),
    "1004": ("statement is incomplete at this time", FAILURE_WAIT, ""),
    "1005": ("settlement data is not ready", FAILURE_WAIT, ""),
    "1006": ("FIFO P&L data is not ready", FAILURE_WAIT, ""),
    "1007": ("MTM P&L data is not ready", FAILURE_WAIT, ""),
    "1008": ("MTM and FIFO P&L data is not ready", FAILURE_WAIT, ""),
    "1009": ("IBKR is under heavy load", FAILURE_WAIT, ""),
    "1010": (
        "legacy Flex query, no longer supported",
        FAILURE_QUERY,
        "Recreate it as an Activity Flex query.",
    ),
    "1011": ("the Flex service account is inactive", FAILURE_TOKEN, ""),
    "1012": (
        "the Flex token has expired",
        FAILURE_TOKEN,
        "Flex tokens expire. Generate a new one under Reports -> Flex Queries "
        "-> Flex Web Service, then update IBKR_TOKEN wherever it is set.",
    ),
    "1013": (
        "the token is IP-restricted",
        FAILURE_TOKEN,
        "This server's IP is not on the token's allow list, which is not the "
        "same IP you browse from.",
    ),
    "1014": (
        "the query id is invalid",
        FAILURE_QUERY,
        "Check the id exists and is exposed to the Flex Web Service, not "
        "merely saved.",
    ),
    "1015": ("the token is invalid", FAILURE_TOKEN, ""),
    "1016": (
        "the account is invalid",
        FAILURE_QUERY,
        "The query points at an account this token cannot read.",
    ),
    "1017": ("the reference code was invalid or had expired", FAILURE_REQUEST, ""),
    "1018": (
        "too many requests from this token",
        FAILURE_WAIT,
        "A real throttle, measured in minutes. Asking again during the cooldown "
        "can restart it.",
    ),
    "1019": ("statement generation in progress", FAILURE_WAIT, ""),
    "1020": ("IBKR could not validate the request", FAILURE_REQUEST, ""),
    "1021": ("statement could not be retrieved at this time", FAILURE_WAIT, ""),
}


@dataclass(frozen=True)
class FlexDiagnosis:
    """A failure message, read.

    `code` is None when the failure carried no IBKR code at all -- a network or
    transport fault rather than a Flex refusal, which is worth saying rather
    than guessing a category for.
    """

    code: Optional[str]
    label: str
    category: str
    guidance: str


def classify_failure(message: str) -> FlexDiagnosis:
    """Read one failure message into something explainable.

    Reads the `code NNNN` fragment for the same reason `is_transient_failure`
    does -- it is the one part of the message this module wrote itself, so it
    is the only part safe to match on.
    """
    found = next(
        (code for code in FLEX_CODES if f"code {code}" in message), None
    )
    if found is None:
        transport = "code" not in message
        return FlexDiagnosis(
            code=None,
            label=(
                "the request never reached IBKR, or IBKR never answered"
                if transport
                else "IBKR returned a code this app does not recognise"
            ),
            category=FAILURE_WAIT if transport else FAILURE_REQUEST,
            guidance=(
                "A network or timeout fault rather than a refusal -- the sync "
                "itself is fine to run again."
                if transport
                else "Not in this app's table; check IBKR's Flex Web Service docs."
            ),
        )

    label, category, guidance = FLEX_CODES[found]
    return FlexDiagnosis(code=found, label=label, category=category, guidance=guidance)


MAX_POLL_ATTEMPTS = 5
POLL_DELAY_SECONDS = 4.0
REQUEST_TIMEOUT = 30.0

# SendRequest is where throttling shows up, so it gets its own retry rather
# than surfacing to the caller as a failed sync.
SEND_ATTEMPTS = 3
SEND_RETRY_DELAY_SECONDS = 5.0

# Breathing room between queries in a multi-query sync, to stay under the
# per-token rate limit rather than tripping it and recovering.
BETWEEN_QUERIES_SECONDS = 3.0

# Ceiling on the whole handshake, across every configured query.
#
# WHY A BUDGET AND NOT A LONGER CLIENT TIMEOUT. One query's worst case is
# already ~266s: 3 SendRequest attempts (2 x 5s backoff, each with a 30s HTTP
# timeout) plus 5 GetStatement polls (4 x 4s backoff, same timeout). Every
# second of that is legitimate -- IBKR compiles the report on demand. But
# IBKR_QUERY_ID takes a LIST, and running two is the documented configuration,
# so the honest worst case is ~535s. The browser gives up at 300s.
#
# What that costs is not the wait, it is the reporting. A client-side timeout
# throws away a run that had not failed: IBKR was already asked to compile the
# report, fills may have been staged, and the user is told "no response from
# the server" about work whose outcome is simply unknown to them.
#
# Bounding the SERVER instead makes the endpoint answer within the client's
# window by construction, and a query that runs out of budget lands in the
# partial-result path that already exists -- reported in `queries_failed`,
# rendered by the sync toast, and picked up by the next run, because ingestion
# is idempotent. 240s leaves the request headroom for the staging, promotion
# and FIFO work that follows the fetch.
#
# The real fix remains a 202 with a job id to poll: a request whose duration is
# bounded by a third party's compile time does not belong in a synchronous
# round trip. This makes the synchronous version honest until then.
TOTAL_BUDGET_SECONDS = 240.0


class IBKRError(RuntimeError):
    """Any failure talking to the Flex service."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def get_credentials() -> tuple[str, list[str]]:
    """Read Flex credentials from the environment.

    Accepts IBKR_FLEX_TOKEN or the shorter IBKR_TOKEN already used elsewhere in
    this project, so both naming conventions work.

    IBKR_QUERY_ID takes a comma-separated list, because no single Flex query
    covers everything: a Trade Confirmation query reports today's fills but
    drops them once they settle, while an Activity query carries history and
    commissions but lags by a day or so. Running both and letting the staging
    table's transaction_id UNIQUE guard absorb the overlap is what gives full
    coverage. A single id is still valid.
    """
    token = os.environ.get("IBKR_FLEX_TOKEN") or os.environ.get("IBKR_TOKEN")
    raw_ids = os.environ.get("IBKR_QUERY_ID", "")
    query_ids = [qid.strip() for qid in raw_ids.split(",") if qid.strip()]

    if not token or not query_ids:
        raise IBKRError(
            "IBKR_FLEX_TOKEN (or IBKR_TOKEN) and IBKR_QUERY_ID must be set in the environment"
        )
    return token, query_ids


def _text(root: ET.Element, tag: str) -> Optional[str]:
    node = root.find(f".//{tag}")
    return node.text.strip() if node is not None and node.text else None


def _parse_xml(payload: str) -> ET.Element:
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise IBKRError(f"IBKR returned unparseable XML: {exc}") from exc


async def request_statement(
    client: httpx.AsyncClient, token: str, query_id: str
) -> str:
    """Step 1 -- ask IBKR to compile the query; return the ReferenceCode.

    Retries the transient refusals itself. The Flex service rate-limits per
    token, so back-to-back queries routinely draw a "try again shortly", and
    bubbling that up would fail a sync over something that clears in seconds.
    """
    last_error: Optional[IBKRError] = None

    for attempt in range(SEND_ATTEMPTS):
        if attempt:
            await asyncio.sleep(SEND_RETRY_DELAY_SECONDS)

        response = await client.get(
            SEND_REQUEST_URL, params={"t": token, "q": query_id, "v": "3"}
        )
        response.raise_for_status()
        root = _parse_xml(response.text)

        status = (_text(root, "Status") or "").lower()
        if status == "success":
            reference_code = _text(root, "ReferenceCode")
            if not reference_code:
                raise IBKRError("IBKR response contained no ReferenceCode")
            return reference_code

        code = _text(root, "ErrorCode") or ""
        message = _text(root, "ErrorMessage") or "unknown error"
        error = IBKRError(
            f"IBKR rejected the statement request (code {code}): {message}",
            retryable=code in NOT_READY_CODES,
        )
        # Retry only what a few seconds can actually fix. A hard rejection
        # (bad token, unknown query) will never improve, and rate limiting
        # gets worse if prodded -- both are raised straight to the caller,
        # which reports them rather than burning the request budget.
        if code not in RETRY_IN_REQUEST_CODES:
            raise error
        last_error = error

    raise last_error or IBKRError("IBKR statement request failed")


async def download_statement(
    client: httpx.AsyncClient, token: str, reference_code: str
) -> ET.Element:
    """Step 2 -- poll GetStatement until the compiled report arrives."""
    last_message = "IBKR statement was never ready"

    for attempt in range(MAX_POLL_ATTEMPTS):
        # IBKR needs a few seconds to compile; wait before every retry.
        if attempt:
            await asyncio.sleep(POLL_DELAY_SECONDS)

        response = await client.get(
            GET_STATEMENT_URL, params={"q": reference_code, "t": token, "v": "3"}
        )
        response.raise_for_status()
        root = _parse_xml(response.text)

        # A still-compiling report comes back as an error envelope rather than
        # the statement payload.
        code = _text(root, "ErrorCode")
        if code:
            message = _text(root, "ErrorMessage") or "unknown error"
            if code in NOT_READY_CODES:
                last_message = f"IBKR statement still generating (code {code}): {message}"
                continue
            raise IBKRError(f"IBKR statement fetch failed (code {code}): {message}")

        return root

    raise IBKRError(f"{last_message}. Retry shortly.", retryable=True)


async def fetch_statement(
    token: Optional[str] = None, query_id: Optional[str] = None
) -> ET.Element:
    """Run the full handshake for one query and return its XML root."""
    if token is None or query_id is None:
        token, query_ids = get_credentials()
        query_id = query_id or query_ids[0]

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            reference_code = await request_statement(client, token, query_id)
            return await download_statement(client, token, reference_code)
        except httpx.HTTPError as exc:
            raise IBKRError(f"IBKR request failed: {exc}") from exc


async def fetch_statements(
    token: Optional[str] = None, query_ids: Optional[list[str]] = None
) -> tuple[list[tuple[str, ET.Element]], list[str]]:
    """Fetch every configured query.

    Returns the statements that came back and a description of each query that
    did not, so the caller can ingest what it has *and* say what is missing.

    Queries run sequentially, spaced out: the Flex service rate-limits per
    token, so firing several handshakes together earns a refusal rather than a
    faster sync.

    One query failing does not abandon the others. IBKR's limit is measured in
    minutes, far longer than a request can wait out, so an all-or-nothing sync
    would routinely return nothing at all. Reporting the failure alongside the
    partial result keeps that visible instead of silently losing a date range,
    and ingestion is idempotent so the next run fills the gap.

    The whole loop is bounded by TOTAL_BUDGET_SECONDS. A query that exhausts it
    is reported exactly like one IBKR refused -- the caller already knows how to
    render a partial run, and a timeout the server names is worth far more than
    one the browser discovers.
    """
    # Resolved INDEPENDENTLY, not as an all-or-nothing pair. A caller wanting
    # the shared token but its OWN query ids -- the investment sync, reading a
    # different account's query while still using the one Flex token -- must
    # not have that query_ids silently discarded just because it left token
    # unset. `token is None or query_ids is None: both from env` was exactly
    # that bug: it fetched the TRADING account's queries for a caller that had
    # explicitly asked for a different one, caught before it ever ran for
    # real because a query id came back that nothing had requested.
    if token is None:
        token = os.environ.get("IBKR_FLEX_TOKEN") or os.environ.get("IBKR_TOKEN")
        if not token:
            raise IBKRError(
                "IBKR_FLEX_TOKEN (or IBKR_TOKEN) must be set in the environment"
            )
    if query_ids is None:
        raw_ids = os.environ.get("IBKR_QUERY_ID", "")
        query_ids = [qid.strip() for qid in raw_ids.split(",") if qid.strip()]
        if not query_ids:
            raise IBKRError("IBKR_QUERY_ID must be set in the environment")

    statements: list[tuple[str, ET.Element]] = []
    failures: list[str] = []

    deadline = asyncio.get_running_loop().time() + TOTAL_BUDGET_SECONDS

    for index, query_id in enumerate(query_ids):
        # Space the requests out. Cheaper to wait than to trip the limit.
        if index:
            await asyncio.sleep(BETWEEN_QUERIES_SECONDS)

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            logger.warning(
                "IBKR query %s skipped: the %.0fs budget was spent on earlier "
                "queries", query_id, TOTAL_BUDGET_SECONDS,
            )
            failures.append(
                f"query {query_id}: skipped, the {TOTAL_BUDGET_SECONDS:.0f}s "
                "budget for this sync was already spent. Run the sync again."
            )
            continue

        # Divided by what is LEFT to run, so one slow query cannot starve the
        # rest -- and so the common case, where a query returns in seconds,
        # still hands its unused share to the next one.
        share = remaining / (len(query_ids) - index)

        try:
            statements.append(
                (query_id, await asyncio.wait_for(
                    fetch_statement(token, query_id), timeout=share
                ))
            )
        except asyncio.TimeoutError:
            logger.warning(
                "IBKR query %s gave up after %.0fs of its share of the sync "
                "budget", query_id, share,
            )
            failures.append(
                f"query {query_id}: IBKR had not returned a statement after "
                f"{share:.0f}s. This usually means the report is still "
                "compiling -- run the sync again shortly."
            )
        except IBKRError as exc:
            logger.warning("IBKR query %s failed: %s", query_id, exc)
            failures.append(f"query {query_id}: {exc}")

    # Nothing at all came back: that is a failed sync, not a partial one.
    if not statements and failures:
        raise IBKRError(
            "; ".join(failures),
            # Rate limiting and "still generating" both clear on their own, so
            # let the caller advertise this as worth retrying.
            retryable=True,
        )

    return statements, failures
