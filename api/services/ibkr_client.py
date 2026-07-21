"""IBKR Flex Web Service client.

The Flex service is a two-step handshake: `SendRequest` asks IBKR to compile a
saved query and returns a `ReferenceCode`, then `GetStatement` downloads the
report once compilation finishes. Compilation is not instant, so GetStatement
is polled until the payload arrives or the attempt budget runs out.
"""

from __future__ import annotations

import asyncio
import os
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

FLEX_BASE = "https://ndcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService"
SEND_REQUEST_URL = f"{FLEX_BASE}.SendRequest"
GET_STATEMENT_URL = f"{FLEX_BASE}.GetStatement"

# IBKR returns these while the report is still being generated on their side.
# They are transient: the correct response is to wait and retry, not to fail.
NOT_READY_CODES = {"1018", "1019"}

MAX_POLL_ATTEMPTS = 5
POLL_DELAY_SECONDS = 4.0
REQUEST_TIMEOUT = 30.0


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
    """Step 1 -- ask IBKR to compile the query; return the ReferenceCode."""
    response = await client.get(
        SEND_REQUEST_URL, params={"t": token, "q": query_id, "v": "3"}
    )
    response.raise_for_status()
    root = _parse_xml(response.text)

    status = (_text(root, "Status") or "").lower()
    if status != "success":
        code = _text(root, "ErrorCode") or ""
        message = _text(root, "ErrorMessage") or "unknown error"
        raise IBKRError(
            f"IBKR rejected the statement request (code {code}): {message}",
            retryable=code in NOT_READY_CODES,
        )

    reference_code = _text(root, "ReferenceCode")
    if not reference_code:
        raise IBKRError("IBKR response contained no ReferenceCode")
    return reference_code


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
) -> list[tuple[str, ET.Element]]:
    """Fetch every configured query, returning (query_id, root) pairs.

    Queries run sequentially rather than concurrently: the Flex service is
    rate-limited per token, and firing several handshakes at once earns a
    throttling error rather than a faster sync.

    A failure on any query raises. Skipping the failed one and returning what
    succeeded would report a healthy sync that quietly omitted a date range --
    the same silent-loss failure mode that hid the TradeConfirm bug. Ingestion
    is idempotent, so failing loudly and retrying costs nothing.
    """
    if token is None or query_ids is None:
        token, query_ids = get_credentials()

    statements: list[tuple[str, ET.Element]] = []
    for query_id in query_ids:
        try:
            statements.append((query_id, await fetch_statement(token, query_id)))
        except IBKRError as exc:
            raise IBKRError(
                f"IBKR query {query_id} failed: {exc}", retryable=exc.retryable
            ) from exc
    return statements
