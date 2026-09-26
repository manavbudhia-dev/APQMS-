"""
Shared date helpers used across the dashboard and the quotations/proforma
invoices/packing lists list pages.

Document dates (quotations, proforma invoices, packing lists) are stored
as free-text "DD - MM - YYYY" strings — entered via a JS date-icon-picker
that converts a native <input type=date> selection into this display
format, but the underlying field stays a plain editable text input, so it
is never guaranteed to be well-formed. These helpers convert that format
to/from ISO ("YYYY-MM-DD") so it can be compared against a date range,
and place a date within an Indian financial year (1 April - 31 March).
"""
import re
from datetime import datetime

_DATE_RE = re.compile(r'^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{4})\s*$')


def date_to_iso(date_str):
    """Best-effort parse of the app's free-text "DD - MM - YYYY" document
    date into an ISO "YYYY-MM-DD" string. Dates that don't match the
    expected shape (hand-typed oddities) come back as '' rather than being
    guessed at, and are simply excluded from any range match."""
    if not date_str:
        return ''
    m = _DATE_RE.match(date_str)
    if not m:
        return ''
    day, month, year = m.groups()
    try:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except ValueError:
        return ''


def date_in_range(date_str, start_iso='', end_iso=''):
    """Whether the app's free-text document date falls within [start_iso,
    end_iso] (either bound optional). An unparseable date never matches a
    bound, i.e. it's excluded rather than assumed to be in range."""
    iso = date_to_iso(date_str)
    if not iso:
        return False
    if start_iso and iso < start_iso:
        return False
    if end_iso and iso > end_iso:
        return False
    return True


def date_in_fy(date_str, fy_row):
    """Whether a document's date falls inside the given financial_years
    row (start_date/end_date, both ISO). `fy_row=None` means "All
    Financial Years" — everything matches, including rows whose date
    can't be parsed at all, so nothing is hidden from the unfiltered view."""
    if fy_row is None:
        return True
    return date_in_range(date_str, fy_row['start_date'], fy_row['end_date'])


def fy_start_year(dt=None):
    """The Indian financial-year start year (e.g. 2025 for FY 2025-26)
    containing the given date (default: today). Indian FYs run 1 April to
    31 March, so Jan-Mar belongs to the FY that started the previous
    calendar year."""
    dt = dt or datetime.now()
    return dt.year if dt.month >= 4 else dt.year - 1


def fy_bounds(start_year):
    """(start_date_iso, end_date_iso, label) for the Indian financial year
    beginning 1 April of `start_year` and ending 31 March of
    `start_year + 1` — e.g. fy_bounds(2025) -> ('2025-04-01',
    '2026-03-31', '2025-26')."""
    start_year = int(start_year)
    start = f"{start_year:04d}-04-01"
    end = f"{start_year + 1:04d}-03-31"
    label = f"{start_year:04d}-{str(start_year + 1)[-2:]}"
    return start, end, label
