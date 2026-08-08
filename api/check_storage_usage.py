"""Report how much of Supabase's free-tier quotas this database actually
uses, and project when either one runs out at the current growth rate.

READ ONLY. Every query is a SELECT against pg_catalog/information_schema or
this app's own tables. Runs inside one transaction that is rolled back.

WHY THIS EXISTS. "The free tier will run out eventually" is a reasonable
worry and a bad basis for a design decision -- Supabase splits Postgres
(500 MB) and Storage (1 GB) into separate quotas, this app's Postgres rows
are almost entirely small numeric/short-text columns, and the one thing that
genuinely eats Storage (plan chart screenshots) is already downscaled to
~100 KB each before upload. Whether either quota is a near-term concern is an
empirical question. This answers it.

    python check_storage_usage.py

Two figures matter most: PERCENT OF QUOTA USED, and YEARS TO FULL AT CURRENT
RATE. The second is a straight-line projection from historical growth and
says nothing about whether trading activity will speed up or slow down --
treat it as an order-of-magnitude sanity check, not a forecast.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import main

# Supabase free tier, as documented. If this ever moves to a paid plan these
# numbers are the only thing in this file that goes stale.
POSTGRES_QUOTA_BYTES = 500 * 1024 * 1024
STORAGE_QUOTA_BYTES = 1024 * 1024 * 1024

# Every table this app owns, in the schema. Listed explicitly rather than
# discovered from information_schema.tables so a stray table Supabase itself
# creates (e.g. anything under supabase_migrations) can never inflate the
# total this script reports as "this app's data".
APP_TABLES = [
    "strategies", "disciplines", "position_disciplines", "app_settings",
    "timeframe_presets", "suppressed_executions", "trades", "planned_trades",
    "positions", "position_fills", "realized_legs", "ibkr_executions",
    "schema_migrations", "data_version",
    "investment_transactions", "investment_holdings",
    "investment_valuation_inputs", "investment_sector_colors",
]

TABLE_SIZE_QUERY = """
    SELECT
        c.relname AS table_name,
        pg_total_relation_size(c.oid) AS total_bytes,
        pg_relation_size(c.oid) AS table_bytes,
        s.n_live_tup AS row_estimate
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
    WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY(:tables)
"""


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _bar(fraction: float, width: int = 30) -> str:
    filled = min(width, round(fraction * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


async def postgres_usage(session) -> None:
    from sqlalchemy import text

    total_db_bytes = (
        await session.scalar(text("SELECT pg_database_size(current_database())"))
    ) or 0

    rows = (
        await session.execute(text(TABLE_SIZE_QUERY), {"tables": APP_TABLES})
    ).mappings()
    by_name = {r["table_name"]: r for r in rows}

    print("=" * 72)
    print("POSTGRES  (Supabase free tier: 500 MB)")
    print("=" * 72)

    app_total = 0
    total_rows = 0
    ranked = []
    for name in APP_TABLES:
        row = by_name.get(name)
        if row is None:
            continue  # table not yet migrated on this database
        app_total += row["total_bytes"]
        total_rows += row["row_estimate"] or 0
        ranked.append(row)

    ranked.sort(key=lambda r: r["total_bytes"], reverse=True)
    print(f"\n{'table':<28} {'rows':>10} {'size (w/ indexes)':>20}")
    print("-" * 62)
    for row in ranked:
        if row["total_bytes"] == 0 and (row["row_estimate"] or 0) == 0:
            continue
        print(
            f"{row['table_name']:<28} {row['row_estimate'] or 0:>10,} "
            f"{_fmt_bytes(row['total_bytes']):>20}"
        )

    fraction = app_total / POSTGRES_QUOTA_BYTES
    print(f"\nThis app's tables:   {_fmt_bytes(app_total):>12}  ({total_rows:,} rows total)")
    print(f"Whole database:      {_fmt_bytes(total_db_bytes):>12}  (includes Supabase's own auth/storage schemas)")
    print(f"Quota:               {_fmt_bytes(POSTGRES_QUOTA_BYTES):>12}")
    print(f"{_bar(fraction)}  {fraction * 100:.2f}% of quota used by this app's tables")


async def growth_rate(session) -> tuple[float, float]:
    """Rows added per day, and average bytes per row, from historical dates.

    Returns (rows_per_day, avg_row_bytes) -- both 0.0 if there is not enough
    history to say anything. Used only for the projection below; the table
    listing above already reports the real, current numbers.
    """
    from sqlalchemy import text

    span = (
        await session.execute(
            text(
                "SELECT MIN(d), MAX(d), COUNT(*) FROM ("
                "  SELECT entry_date::date AS d FROM trades"
                "  UNION ALL"
                "  SELECT transaction_date::date FROM investment_transactions"
                ") AS dates"
            )
        )
    ).one()
    earliest, latest, count = span
    if not earliest or not latest or count < 2:
        return 0.0, 0.0

    days = max((latest - earliest).days, 1)
    return count / days, 0.0


async def storage_usage(session) -> None:
    from sqlalchemy import text

    row = (
        await session.execute(
            text(
                "SELECT COUNT(*) AS n, "
                "COALESCE(SUM(chart_bytes), 0) AS total_bytes, "
                "MIN(chart_uploaded_at) AS earliest, "
                "MAX(chart_uploaded_at) AS latest "
                "FROM planned_trades WHERE chart_bytes IS NOT NULL"
            )
        )
    ).one()
    count, total_bytes, earliest, latest = row

    print()
    print("=" * 72)
    print("STORAGE  (Supabase free tier: 1 GB -- plan chart screenshots only)")
    print("=" * 72)

    fraction = total_bytes / STORAGE_QUOTA_BYTES
    avg = total_bytes / count if count else 0
    print(f"\nCharts uploaded:     {count:,}")
    print(f"Total size:          {_fmt_bytes(total_bytes)}")
    print(f"Average per chart:   {_fmt_bytes(avg)}" if count else "")
    print(f"Quota:               {_fmt_bytes(STORAGE_QUOTA_BYTES)}")
    print(f"{_bar(fraction)}  {fraction * 100:.2f}% of quota used")

    if count >= 2 and earliest and latest and earliest != latest:
        days = max((latest - earliest).days, 1)
        per_day = count / days
        if per_day > 0:
            remaining = STORAGE_QUOTA_BYTES - total_bytes
            days_left = remaining / (per_day * avg) if avg > 0 else float("inf")
            years_left = days_left / 365.25
            print(
                f"\nAt the current upload rate (~{per_day:.2f} charts/day since "
                f"{earliest.date()}), Storage reaches quota in "
                f"~{years_left:,.0f} years."
            )


async def postgres_projection(session, rows_per_day: float) -> None:
    if rows_per_day <= 0:
        print(
            "\n(Not enough date history across trades/investment_transactions "
            "to project a Postgres growth rate.)"
        )
        return

    from sqlalchemy import text

    total_bytes = (
        await session.execute(
            text(TABLE_SIZE_QUERY), {"tables": APP_TABLES}
        )
    ).mappings()
    app_total = sum(r["total_bytes"] for r in total_bytes)
    total_rows = (
        await session.scalar(
            text(
                "SELECT COALESCE(SUM(n_live_tup), 0) FROM pg_stat_user_tables "
                "WHERE schemaname = 'public' AND relname = ANY(:tables)"
            ),
            {"tables": APP_TABLES},
        )
    ) or 0

    if total_rows <= 0:
        return

    # asyncpg returns SUM(n_live_tup) as a Decimal (it is COALESCEd against a
    # literal 0, which nudges Postgres to pick a numeric result type). Cast
    # explicitly rather than let a Decimal meet a float three lines down --
    # Python raises on that mix rather than coercing it.
    bytes_per_row = app_total / float(total_rows)
    bytes_per_day = rows_per_day * bytes_per_row
    if bytes_per_day <= 0:
        return

    remaining = POSTGRES_QUOTA_BYTES - app_total
    years_left = (remaining / bytes_per_day) / 365.25
    print(
        f"\nAt the current rate (~{rows_per_day:.2f} new trade/transaction "
        f"rows/day, ~{_fmt_bytes(bytes_per_row)}/row including indexes), "
        f"Postgres reaches quota in ~{years_left:,.0f} years."
    )


async def run() -> int:
    async with main.engine.connect() as conn:
        outer = await conn.begin()
        try:
            session = main.AsyncSession(bind=conn, expire_on_commit=False)
            await postgres_usage(session)
            rows_per_day, _ = await growth_rate(session)
            await postgres_projection(session, rows_per_day)
            await storage_usage(session)
        finally:
            await outer.rollback()
            await main.engine.dispose()

    print()
    print("=" * 72)
    print(
        "Both projections are straight-line extrapolations from history, not "
        "forecasts -- read them as order-of-magnitude, not a countdown clock."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
