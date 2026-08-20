"""Turning rows into a CSV that a spreadsheet reads back correctly.

Serialization only. WHAT goes into each export lives in `main.py`, beside the
models and endpoints the rows are read from -- a service that imported those
would be pointing back at the application which imports it.

Four things this does that a bare `csv.writer` does not:

1. **A UTF-8 BOM.** Excel on Windows reads a BOM-less UTF-8 file as the system
   codepage, so a thesis containing an em dash or a currency symbol arrives as
   mojibake. Every other reader tolerates the BOM; Excel needs it, and this
   journal's owner is on Windows.

2. **A guard against formula injection.** A text cell beginning with `=`, `+`
   or `@` is a formula to Excel, not text. `-` is deliberately NOT guarded: a
   review note starting with a dash is an ordinary bullet, guarding it would
   put a stray apostrophe in front of real prose, and the worst an unguarded
   leading dash does is show `#NAME?` in one cell. Cosmetic damage to common
   input, traded against a display glitch on rare input.

3. **Decimals written as stored.** `str(Decimal("342.1500"))` preserves the
   scale the column holds. Rendering through float would print `342.15` and
   quietly disagree with the ledger -- prices and quantities are NUMERIC for
   exactly that reason, and an export is the last place to throw it away.

4. **Empty, not zero, for a missing number.** The journal draws that
   distinction everywhere else: an unscoreable R is None, a watchlist entry has
   no market value. A CSV printing 0.00 for "never recorded" would feed a
   real-looking figure into every average taken over the column.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Optional, Sequence, Union

# Prepended to every export. A character, not bytes -- the caller encodes once,
# at the response boundary. Written as an escape rather than the literal
# character, which is invisible in an editor and survives a copy-paste only by
# luck.
BOM = "\ufeff"

# See point 2 of the module docstring for why `-` is absent from this.
_FORMULA_LEADERS = ("=", "+", "@")

# A leading tab or carriage return can also open a formula, and neither has any
# business starting a cell.
_FORMULA_CONTROL = ("\t", "\r")

Number = Union[Decimal, float, int]


def text(value: object) -> str:
    """A free-text cell, neutered against Excel's formula parser."""
    if value is None:
        return ""
    rendered = str(value)
    if rendered.startswith(_FORMULA_LEADERS + _FORMULA_CONTROL):
        # Excel reads a leading apostrophe as "everything after this is text".
        return "'" + rendered
    return rendered


def number(value: Optional[Number]) -> str:
    """A numeric cell at the precision it is stored, or empty when unknown.

    ALWAYS fixed-point, never scientific. `str(Decimal("0.00000001"))` returns
    "1E-8", and `quantity` is NUMERIC(18,8) on an account where fractional
    shares are ordinary -- so the default rendering would have exported real
    share counts in a notation half the tools downstream read as text. Formatting
    with "f" keeps the scale the column holds and drops the exponent.

    Floats take the same route via their own shortest round-trip repr, which is
    what `str` would have printed: `Decimal(str(x))` introduces no digits the
    float did not already claim, and formatting from there avoids both `1e-07`
    and the six-decimal truncation a bare `format(x, "f")` would apply.

    NaN and infinity render empty for the same reason None does: neither is a
    figure a spreadsheet should be invited to average.
    """
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else ""
    if isinstance(value, float):
        return format(Decimal(str(value)), "f") if math.isfinite(value) else ""
    return str(value)


def timestamp(value: Optional[datetime]) -> str:
    """ISO 8601 including the offset, or empty.

    The offset is kept rather than stripped: these rows are timestamped in UTC
    and read by someone in Singapore about a market in New York, and a naive
    wall-clock time would be ambiguous between all three.
    """
    return "" if value is None else value.isoformat()


def day(value: Optional[date]) -> str:
    """A bare calendar date, which is what a pivot table groups by."""
    return "" if value is None else value.isoformat()


def flag(value: Optional[bool]) -> str:
    """TRUE/FALSE, the spelling Excel recognises as boolean. Empty for unknown."""
    if value is None:
        return ""
    return "TRUE" if value else "FALSE"


def joined(values: Optional[Sequence[str]]) -> str:
    """A list cell, semicolon-separated.

    Semicolons rather than commas so the value survives being read back by
    something that splits on commas before it honours quoting -- and so the
    cell stays legible in a spreadsheet, which is the point of the export.
    """
    if not values:
        return ""
    return text("; ".join(str(v) for v in values))


def render(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """Header plus rows as one RFC 4180 document.

    The header is written even when there are no rows. An export of an empty
    book should be a file that names its columns, not a zero-byte file
    indistinguishable from a download that failed.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return BOM + buffer.getvalue()
