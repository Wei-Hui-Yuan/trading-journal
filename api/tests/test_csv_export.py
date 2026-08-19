"""The CSV exports: what a spreadsheet gets, and what it must never get.

Four things here are load-bearing, and each has a test that fails without it:

  * The file has to arrive COMPLETE. Two separate mechanisms could silently
    truncate it -- a conditional request turning the export into a 304, and a
    page limit leaking in from the endpoint it borrows -- and both would produce
    a valid-looking CSV with trades missing. A short export is worse than a
    failed one, because nothing about it looks wrong.

  * Numbers have to survive. The ledger stores NUMERIC; a float round-trip
    would quietly restate prices, and an empty cell has to stay distinguishable
    from a zero, or every average taken over the column is wrong.

  * Rows have to line up with the header. The round-trip export is 43 columns
    assembled by hand, which is exactly the shape that drifts by one.

  * Text must not execute. An export is the one artifact that leaves this app
    and gets opened by something else.
"""

import asyncio
import csv
import io
import math
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from auth import verify_clerk_token  # noqa: E402
from services import csv_export as fmt  # noqa: E402

from conftest import db_session, db_transaction, requires_db  # noqa: E402


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_a_spreadsheet_can_tell_the_file_is_utf8():
    """Excel on Windows reads a BOM-less UTF-8 CSV as the system codepage, and
    this journal's owner is on Windows. Without the BOM a thesis containing an
    em dash arrives as mojibake -- readable enough to be shipped, wrong enough
    to be worthless."""
    rendered = fmt.render(["a"], [["1"]])

    assert rendered.startswith(fmt.BOM)
    assert fmt.BOM == "﻿"


def test_an_empty_export_still_names_its_columns():
    """A book with nothing in it should download a file listing its columns, not
    a zero-byte file indistinguishable from a download that failed."""
    rendered = fmt.render(["ticker", "quantity"], [])

    assert rendered == fmt.BOM + "ticker,quantity\r\n"


@pytest.mark.parametrize("leader", ["=", "+", "@", "\t", "\r"])
def test_a_note_that_looks_like_a_formula_is_not_one(leader):
    """`=cmd|...` in a review note is a formula to Excel, not prose."""
    guarded = fmt.text(f"{leader}HYPERLINK(\"http://x\")")

    assert guarded.startswith("'")


def test_a_note_starting_with_a_dash_is_left_alone():
    """Pins a deliberate omission, so nobody "completes" the guard later.

    A dash is a bullet: `- sized too big` is ordinary journal prose and common.
    Guarding it would put a stray apostrophe in front of real text, where the
    worst an unguarded leading dash does is render #NAME? in one cell. Cosmetic
    damage to frequent input is a bad trade against a display glitch on rare
    input."""
    assert fmt.text("- sized too big, chased the entry") == (
        "- sized too big, chased the entry"
    )


def test_a_price_keeps_the_scale_the_database_holds():
    """`actual_entry` is NUMERIC(10,4). Rendering through float would print
    342.15 and disagree with the ledger about what the fill cost."""
    assert fmt.number(Decimal("342.1500")) == "342.1500"


@pytest.mark.parametrize(
    "value, expected",
    [
        (Decimal("0.00000001"), "0.00000001"),
        (Decimal("0.0000001"), "0.0000001"),
        (Decimal("1E+9"), "1000000000"),
        (1e-07, "0.0000001"),
        (1e20, "100000000000000000000"),
    ],
)
def test_a_small_quantity_is_not_exported_in_scientific_notation(value, expected):
    """Caught by this test rather than by a spreadsheet later.

    `str(Decimal("0.00000001"))` is "1E-8", and `quantity` is NUMERIC(18,8) on
    an account where fractional fills are ordinary -- migration 010 exists
    because rounding them destroyed sub-half-share positions outright. Exporting
    a real share count as "1E-8" hands a figure to every downstream tool that
    reads an exponent as text."""
    rendered = fmt.number(value)

    assert rendered == expected
    assert "E" not in rendered.upper()


def test_a_missing_number_is_empty_not_zero():
    """The journal draws this distinction everywhere -- an unscoreable R is
    None, a watchlist entry has no market value. A CSV printing 0.00 for "never
    recorded" feeds a real-looking figure into every average over the column."""
    assert fmt.number(None) == ""
    assert fmt.number(Decimal("0")) == "0"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_number_that_is_not_finite_is_empty(value):
    """Neither is a figure a spreadsheet should be invited to average."""
    assert fmt.number(value) == ""


def test_a_decimal_nan_is_also_empty():
    """Decimal carries its own NaN, and it reaches here from division in the
    valuation engine rather than from the database."""
    assert fmt.number(Decimal("NaN")) == ""


def test_a_list_cell_does_not_split_on_commas():
    """`mistakes` is a list. Joined with commas it would read as extra columns
    to anything that splits before it honours quoting."""
    assert fmt.joined(["FOMO", "Chased"]) == "FOMO; Chased"
    assert fmt.joined([]) == ""
    assert fmt.joined(None) == ""


def test_a_timestamp_keeps_its_offset():
    """Rows are stamped in UTC and read in Singapore about a market in New
    York. A naive wall-clock time is ambiguous between all three."""
    stamped = datetime(2026, 3, 4, 14, 31, tzinfo=timezone.utc)

    assert fmt.timestamp(stamped) == "2026-03-04T14:31:00+00:00"
    assert fmt.timestamp(None) == ""


def test_a_boolean_is_spelled_the_way_excel_reads_it():
    assert fmt.flag(True) == "TRUE"
    assert fmt.flag(False) == "FALSE"
    # Unknown is not False. `broker_realized_pnl` being absent means the broker
    # never said, which is the same shape of distinction.
    assert fmt.flag(None) == ""


def test_a_rendered_export_reads_back_as_the_rows_that_went_in():
    """The round trip that matters: whatever quoting was applied, a CSV reader
    has to recover the original cells."""
    rows = [
        ["CRWD", "a note, with a comma", "342.1500"],
        ["MSFT", 'a note with "quotes"', ""],
        ["NVDA", "a note\nspanning lines", "0"],
    ]
    rendered = fmt.render(["ticker", "note", "price"], rows)

    parsed = list(csv.reader(io.StringIO(rendered.lstrip(fmt.BOM))))

    assert parsed[0] == ["ticker", "note", "price"]
    assert parsed[1:] == rows


# ---------------------------------------------------------------------------
# The contract with the endpoints the exports borrow
# ---------------------------------------------------------------------------


def test_the_round_trip_export_asks_for_every_row_with_no_validator():
    """Two silent-truncation risks in one assertion, both of which produce a
    CSV that looks fine.

    `limit` must stay None: the journal page's default ceiling would cap the
    export at one page and nothing about the file would say so.

    The Request must carry no If-None-Match: `list_round_trips` honours it, so
    passing the browser's real request would let a validator the browser holds
    for the JOURNAL turn this export into a 304 -- a header row with no trades
    beneath it, indistinguishable from an account that never traded."""
    captured = {}

    async def _spy(*, request, response, limit, offset, session):
        captured["if_none_match"] = request.headers.get("if-none-match")
        captured["header_count"] = len(request.headers)
        captured["limit"] = limit
        captured["offset"] = offset
        return []

    class _StubSession:
        """Answers only the strategy-name lookup the builder makes alongside."""

        async def execute(self, _stmt):
            return []

    original = main.list_round_trips
    main.list_round_trips = _spy
    try:
        asyncio.run(main._export_round_trips(_StubSession()))
    finally:
        main.list_round_trips = original

    assert captured["if_none_match"] is None
    assert captured["header_count"] == 0
    assert captured["limit"] is None, "the export must not inherit a page limit"
    assert captured["offset"] == 0


def test_the_synthetic_request_carries_nothing_to_match_on():
    """The guard above, at its source."""
    request = main._headerless_request()

    assert request.headers.get("if-none-match") is None
    assert len(request.headers) == 0


def test_every_dataset_is_registered():
    """A dataset in the enum with no builder behind it is a 500 waiting for
    whoever adds the fifth export."""
    assert set(main._EXPORTS) == set(main.ExportDataset)

    stems = [stem for _, stem in main._EXPORTS.values()]
    assert len(set(stems)) == len(stems), "two exports would download as one filename"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


HEADER = ["ticker", "note"]
ROWS = [["CRWD", "held through earnings"]]


@pytest.fixture
def client(monkeypatch):
    """A TestClient with auth, the session and every builder stubbed.

    The builders are replaced so these tests measure the transport -- filename,
    caching, content type, rejection -- without a database. What the builders
    put in the file is covered by the serialization tests above and the
    DB-backed ones below.
    """

    async def _build(_session):
        return HEADER, ROWS

    monkeypatch.setattr(
        main, "_EXPORTS", {d: (_build, f"stub-{d.value}") for d in main.ExportDataset}
    )

    async def _session():
        yield object()

    main.app.dependency_overrides[main.get_session] = _session
    main.app.dependency_overrides[verify_clerk_token] = lambda: None
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("dataset", [d.value for d in main.ExportDataset])
def test_each_dataset_downloads_as_a_csv(client, dataset):
    response = client.get(f"/api/export/{dataset}.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.startswith(fmt.BOM)
    assert "CRWD" in response.text


@pytest.mark.parametrize("dataset", [d.value for d in main.ExportDataset])
def test_the_download_is_named_after_its_dataset_and_the_market_date(client, dataset):
    """The date is MARKET date, not the server's. An export taken at breakfast
    in Singapore is still the previous session in New York, and a UTC-named file
    would be filed a day ahead of the trades inside it."""
    from services.analytics import market_today

    response = client.get(f"/api/export/{dataset}.csv")

    disposition = response.headers["content-disposition"]
    assert disposition == (
        f'attachment; filename="stub-{dataset}-{market_today().isoformat()}.csv"'
    )


def test_an_export_is_never_served_from_a_cache(client):
    """Every other read endpoint here revalidates against `data_version`, which
    is right for a page that should show what is current. An export is a
    snapshot asked for by name: a cached copy would hand back a file that
    silently predates the data it claims to hold."""
    response = client.get("/api/export/round-trips.csv")

    assert response.headers["Cache-Control"] == "no-store"
    assert "ETag" not in response.headers


def test_a_conditional_header_cannot_shorten_an_export(client):
    """Belt and braces over the synthetic-request test: even asked to
    revalidate, this endpoint returns the whole file."""
    response = client.get(
        "/api/export/round-trips.csv", headers={"If-None-Match": '"anything"'}
    )

    assert response.status_code == 200
    assert "CRWD" in response.text


def test_an_unknown_dataset_is_refused(client):
    """A typo must not fall through to an empty file that looks like an empty
    account."""
    response = client.get("/api/export/everything.csv")

    assert response.status_code == 422


def test_the_filename_is_exposed_to_the_browser():
    """Content-Disposition is not a CORS-safelisted response header. Without it
    on expose_headers the browser strips it, and the page has to guess the
    name -- which is how an export saves itself as "blob"."""
    cors = next(
        m for m in main.app.user_middleware if "CORSMiddleware" in str(m.cls)
    )
    exposed = cors.kwargs["expose_headers"]

    assert "Content-Disposition" in exposed
    # The one that was already there, which this must not have displaced.
    assert "ETag" in exposed


def test_an_export_requires_authentication():
    """No override installed: the dependency has to reject on its own."""
    with TestClient(main.app) as unauthenticated:
        response = unauthenticated.get("/api/export/round-trips.csv")

    assert response.status_code in (401, 500)


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


TICKER = "ZZEXPORT"
T0 = datetime.now(timezone.utc) - timedelta(days=3)


def _fill(direction, qty, price, hours):
    return main.Trade(
        id=uuid.uuid4(),
        ibkr_exec_id=f"REPAIR-{uuid.uuid4()}",
        ticker=TICKER,
        direction=direction,
        style="Unclassified",
        quantity=Decimal(qty),
        actual_entry=Decimal(price),
        entry_date=T0 + timedelta(hours=hours),
        commission=Decimal("0.35"),
        source_tag="Repair",
        # Free text carrying both hazards at once: a leading `=` and a comma.
        thesis="=SUM(A1:A2), and a comma",
    )


def _parse(rendered):
    return list(csv.reader(io.StringIO(rendered.lstrip(fmt.BOM))))


@requires_db
@pytest.mark.parametrize("dataset", [d.value for d in main.ExportDataset])
def test_every_row_lines_up_with_its_header(dataset):
    """The likeliest bug in a 43-column builder assembled by hand, and one a
    reader will not complain about -- it just shifts every value after the
    missing one into the wrong column."""

    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            build, _ = main._EXPORTS[main.ExportDataset(dataset)]
            header, rows = await build(session)

            assert header, "an export with no columns"
            for index, row in enumerate(rows):
                assert len(row) == len(header), (
                    f"{dataset} row {index} has {len(row)} cells "
                    f"for {len(header)} columns"
                )
                for cell in row:
                    assert isinstance(cell, str), (
                        f"{dataset} row {index} holds a non-string cell: {cell!r}"
                    )
            await session.close()

    asyncio.run(scenario())


@requires_db
def test_a_live_position_reaches_the_export():
    """Open exposure has no exit, no P&L and no review, so it is the row most
    easily dropped. Leaving it out would make the export the only surface in
    the app that pretends live exposure is not there -- the exact bug
    /api/round-trips was built to fix."""

    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            session.add(_fill("BUY", 10, 100, 0))
            await session.commit()

            header, rows = await main._export_round_trips(session)
            symbol = header.index("symbol")
            kind = header.index("kind")

            mine = [r for r in rows if r[symbol] == TICKER]
            assert len(mine) == 1, "the open position did not reach the export"
            assert mine[0][kind] == "open"
            assert mine[0][header.index("exit_price")] == ""
            assert mine[0][header.index("realized_pnl")] == ""
            await session.close()

    asyncio.run(scenario())


@requires_db
def test_the_executions_export_holds_the_whole_ledger():
    """Measured as a delta, because the export is global and the real ledger
    contains whatever it contains today."""

    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            _, before = await main._export_executions(session)

            session.add_all([_fill("BUY", 10, 100, 0), _fill("SELL", 10, 110, 1)])
            await session.commit()

            header, after = await main._export_executions(session)

            assert len(after) - len(before) == 2
            ticker = header.index("ticker")
            mine = [r for r in after if r[ticker] == TICKER]
            assert len(mine) == 2

            # The price survives at the scale the column holds, and the
            # commission keeps its sign.
            price = header.index("price")
            assert sorted(r[price] for r in mine) == ["100.0000", "110.0000"]
            assert {r[header.index("commission")] for r in mine} == {"0.350000"}

            # And the hostile thesis is neutralised without being mangled.
            thesis = header.index("thesis")
            assert all(r[thesis].startswith("'=SUM") for r in mine)
            await session.close()

    asyncio.run(scenario())


@requires_db
def test_a_real_export_survives_being_read_back():
    """End to end: through the builder, through the writer, back through a CSV
    reader, with the real ledger's own text in it."""

    async def scenario():
        async with db_transaction() as conn:
            session = db_session(conn)
            session.add_all([_fill("BUY", 10, 100, 0), _fill("SELL", 10, 110, 1)])
            await session.commit()

            header, rows = await main._export_executions(session)
            parsed = _parse(fmt.render(header, rows))

            assert parsed[0] == header
            assert len(parsed) == len(rows) + 1
            assert all(len(line) == len(header) for line in parsed)
            await session.close()

    asyncio.run(scenario())
