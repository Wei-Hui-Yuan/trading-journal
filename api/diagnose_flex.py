"""Say exactly what IBKR refused, and whether waiting will fix it.

The app can only ever tell you that a query "did not return". This tells you
which of IBKR's error codes came back, what that code actually means, whether
retrying is worth anything, and what to do instead if it is not.

It also runs queries ONE AT A TIME, which the app cannot:
`services/ibkr_client.fetch_statements` always runs the configured queries as a
batch, so a query that only fails when it runs second is indistinguishable
there from one that is broken outright. Running a single query in isolation
separates those two.

READ ONLY. Fetches statements and parses them in memory. Touches no database,
writes nothing, and imports neither `main` nor any database module -- so it
needs no DATABASE_URL and cannot alter the ledger.

RATE LIMITS -- READ THIS. Every run spends real Flex requests against a token
IBKR throttles per token, and asking again mid-cooldown can EXTEND the lockout
rather than clear it (the same reason ibkr_client refuses to retry codes 1001
and 1018 in-request). The default mode therefore makes exactly ONE request.
Do not put this in a loop.

    cd api
    python diagnose_flex.py 1578941     # one query, alone -- START HERE
    python diagnose_flex.py --each      # every configured query, well spaced
    python diagnose_flex.py --sequence  # replay the real sync's batch

Credentials are read from api/.env (gitignored) or the environment:
IBKR_TOKEN (or IBKR_FLEX_TOKEN) and IBKR_QUERY_ID. The token is never printed,
so this script's output is safe to share.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

API_ROOT = Path(__file__).resolve().parent
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

try:  # Optional: the environment may already carry the vars.
    from dotenv import load_dotenv

    load_dotenv(API_ROOT / ".env")
except ImportError:  # pragma: no cover - convenience only
    pass

from services import ibkr_client, ibkr_parser  # noqa: E402

MARKET_TZ = ZoneInfo("America/New_York")

# Generous, because the point of --each is to prove a query works when it is
# NOT racing another one. Too short and the test reproduces the very condition
# it is trying to rule out.
SPACED_GAP_SECONDS = 90.0

# Whether retrying is worth anything, per category. The categories and the
# code table itself live in services/ibkr_client, NOT here -- the running app
# reports the same readings to the user through the sync toast, and two copies
# of this knowledge would eventually disagree about what a code means. This
# script is a second front end onto that one table.
WAITING_HELPS = {
    ibkr_client.FAILURE_WAIT: "yes -- but the wait can be hours, not minutes",
    ibkr_client.FAILURE_QUERY: "NO. Nothing changes until the query itself is fixed.",
    ibkr_client.FAILURE_TOKEN: "NO. Nothing changes until the credential is fixed.",
    ibkr_client.FAILURE_REQUEST: "NO. This one is a bug on our side, not IBKR's.",
}


def _credentials() -> tuple[str, list[str]]:
    token = os.environ.get("IBKR_FLEX_TOKEN") or os.environ.get("IBKR_TOKEN")
    raw = os.environ.get("IBKR_QUERY_ID", "")
    ids = [q.strip() for q in raw.split(",") if q.strip()]
    if not token:
        sys.exit(
            "IBKR_TOKEN (or IBKR_FLEX_TOKEN) is not set.\n"
            "Put it in api/.env (gitignored) or export it for this shell.\n"
            "Copy the values out of Northflank -- do not paste them into chat."
        )
    return token, ids


def _market_clock() -> tuple[str, str]:
    """Local and New York time, plus whether the US market is open.

    Printed on every run because time of day is a real variable here: an
    Activity query can be unserviceable at 03:00 ET and fine at 10:00 ET, and
    without the ET clock in front of you that reads as an intermittent bug.
    """
    now_local = datetime.now().astimezone()
    now_et = now_local.astimezone(MARKET_TZ)

    weekday = now_et.weekday() < 5
    minutes = now_et.hour * 60 + now_et.minute
    is_open = weekday and 9 * 60 + 30 <= minutes < 16 * 60
    if is_open:
        state = "market OPEN"
    elif not weekday:
        state = "weekend -- no statement activity"
    else:
        state = "market closed (opens 09:30 ET)"

    return (
        now_local.strftime("%Y-%m-%d %H:%M %Z"),
        f"{now_et.strftime('%Y-%m-%d %H:%M')} ET  <- {state}",
    )


async def _attempt(token: str, query_id: str) -> dict:
    """One full SendRequest -> GetStatement handshake. Never raises."""
    started = time.monotonic()
    try:
        root = await ibkr_client.fetch_statement(token, query_id)
    except ibkr_client.IBKRError as exc:
        message = str(exc)
        return {
            "query": query_id,
            "ok": False,
            "seconds": time.monotonic() - started,
            "detail": message,
            "diagnosis": ibkr_client.classify_failure(message),
            # What the app's RETRY logic concludes, which is coarser than the
            # code table -- worth surfacing where the two diverge.
            "app_calls_transient": ibkr_client.is_transient_failure(message),
        }

    fills = ibkr_parser.parse_statement(root)
    stamps = sorted(f.execution_time for f in fills if f.execution_time)
    return {
        "query": query_id,
        "ok": True,
        "seconds": time.monotonic() - started,
        "fills": len(fills),
        "non_tradeable": ibkr_parser.count_non_tradeable(root),
        "earliest": stamps[0] if stamps else None,
        "latest": stamps[-1] if stamps else None,
    }


def _report(result: dict) -> None:
    tag = "OK  " if result["ok"] else "FAIL"
    print(f"  [{tag}] query {result['query']}  ({result['seconds']:.1f}s)")

    if result["ok"]:
        print(f"         fills parsed: {result['fills']}")
        if result["non_tradeable"]:
            print(f"         non-tradeable rows skipped: {result['non_tradeable']}")
        if result["earliest"]:
            # The period a query actually covers is the thing most often
            # assumed rather than checked.
            print(f"         covers: {result['earliest']}")
            print(f"             ..: {result['latest']}")
        else:
            print(
                "         covers: nothing -- the query ran fine and returned no\n"
                "                 fills. Expected for a Trade Confirmation query\n"
                "                 outside trading hours; a problem for an\n"
                "                 Activity query that should carry history."
            )
        return

    # IBKR's own words first: everything below is our reading of them.
    print(f"         IBKR said: {result['detail']}")

    diagnosis = result["diagnosis"]
    code = f"code {diagnosis.code}" if diagnosis.code else "no IBKR code"
    print(f"         {code} -- {diagnosis.label}")
    print(f"         category: {diagnosis.category.upper()}")
    print(f"         does waiting help? {WAITING_HELPS[diagnosis.category]}")
    if diagnosis.guidance:
        for sentence in diagnosis.guidance.split(". "):
            if sentence.strip():
                print(f"         > {sentence.strip().rstrip('.')}.")

    # The app's RETRY classifier is coarser than the code table, and where the
    # two disagree it is the app's advice to the user that is wrong.
    waiting = diagnosis.category == ibkr_client.FAILURE_WAIT
    if not waiting and result["app_calls_transient"]:
        print("         NOTE: the app's retry logic calls this transient,")
        print("               which is wrong for this code.")
    elif waiting and not result["app_calls_transient"]:
        print("         NOTE: the app's retry logic calls this permanent,")
        print("               but it is not.")


def _verdict(results: list[dict], targets: list[str], spaced: bool) -> None:
    print("-" * 68)

    failed = [r for r in results if not r["ok"]]
    passed = [r for r in results if r["ok"]]

    if not failed:
        print("Every query tested succeeded on its own.")
        if len(targets) > 1 and spaced:
            print(
                "\nIf the app still fails one of these on every run, the cause is\n"
                "POSITIONAL: the second SendRequest lands inside the token's\n"
                f"cooldown, and BETWEEN_QUERIES_SECONDS "
                f"({ibkr_client.BETWEEN_QUERIES_SECONDS:.0f}s) is too short."
            )
        elif len(targets) == 1:
            print(
                "\nIf this same query fails inside a normal sync, it is not the\n"
                "query that is broken -- it is being refused for running second.\n"
                "Next: python diagnose_flex.py --each"
            )
        empty = [r for r in passed if r["fills"] == 0]
        if empty:
            print(
                f"\nNote: {len(empty)} of {len(passed)} returned zero fills. A query that\n"
                "succeeds but carries nothing feeds the ledger nothing -- check its\n"
                "period covers the days you expect, not just that it runs."
            )
        return

    # Grouped by what would actually fix them, so two queries refused for the
    # same reason are one instruction and two refused for different reasons are
    # never collapsed into one.
    by_category: dict[str, list[dict]] = {}
    for result in failed:
        by_category.setdefault(result["diagnosis"].category, []).append(result)

    advice = {
        ibkr_client.FAILURE_WAIT: (
            "IBKR-side and temporary. Nothing to fix in the query or the\n"
            "         token -- but 'temporary' can mean hours. Re-run at a\n"
            "         different time of day before changing anything."
        ),
        ibkr_client.FAILURE_QUERY: (
            "The saved query is at fault. Open it under Reports -> Flex\n"
            "         Queries and check its id, its account, its period, and\n"
            "         that it is exposed to the Flex Web Service."
        ),
        ibkr_client.FAILURE_TOKEN: (
            "The credential is at fault. Nothing about the queries will\n"
            "         change this."
        ),
        ibkr_client.FAILURE_REQUEST: (
            "Malformed request, or a code this app does not know -- a bug\n"
            "         on this side rather than something to configure."
        ),
    }

    for category, group in by_category.items():
        ids = ", ".join(r["query"] for r in group)
        print(f"{category.upper():<8} {ids}")
        print(f"         {advice[category]}")

    if passed:
        print(f"\nWorked: {', '.join(r['query'] for r in passed)}")
        print(
            "Since another query on the SAME token succeeded just now, the\n"
            "token is valid and is not throttled. Whatever is refusing the\n"
            "others is specific to them, not to your credentials."
        )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test IBKR Flex queries one at a time, and say what failed.",
    )
    parser.add_argument(
        "query_id",
        nargs="?",
        help="Test just this query, alone. Omit to use IBKR_QUERY_ID.",
    )
    parser.add_argument(
        "--each",
        action="store_true",
        help=f"Test every configured query, spaced {SPACED_GAP_SECONDS:.0f}s apart.",
    )
    parser.add_argument(
        "--sequence",
        action="store_true",
        help="Replay the real sync: every query, at the app's own spacing.",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=None,
        help="Override the spacing, in seconds.",
    )
    args = parser.parse_args()

    token, configured = _credentials()

    if args.query_id:
        targets = [args.query_id]
        gap = 0.0
        mode = "one query, alone"
    elif args.each or args.sequence:
        if not configured:
            sys.exit("IBKR_QUERY_ID is not set, so there is nothing to iterate.")
        targets = configured
        gap = args.gap if args.gap is not None else (
            ibkr_client.BETWEEN_QUERIES_SECONDS if args.sequence else SPACED_GAP_SECONDS
        )
        mode = (
            f"replaying the sync's batch, {gap:.0f}s apart"
            if args.sequence
            else f"each query alone, {gap:.0f}s apart"
        )
    else:
        sys.exit(
            "Give a query id, or pass --each / --sequence.\n"
            "Start with a single id: it costs one Flex request and is the\n"
            "decisive test for whether that query works when it runs first."
        )

    local, eastern = _market_clock()
    print(f"\n  local : {local}")
    print(f"  market: {eastern}")
    print(f"\n{mode}")
    print(f"queries: {', '.join(targets)}\n")

    results = []
    for index, query_id in enumerate(targets):
        if index and gap:
            print(f"  ... waiting {gap:.0f}s before the next query\n")
            await asyncio.sleep(gap)
        results.append(await _attempt(token, query_id))
        _report(results[-1])
        print()

    _verdict(results, targets, spaced=args.each)


if __name__ == "__main__":
    asyncio.run(main())
