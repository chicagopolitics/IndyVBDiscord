"""Text and date helpers shared by the source parsers."""
from __future__ import annotations

import re
from datetime import date, time, datetime

# CCA renders dates as "Wednesday, August 19" with no year. Anything that parses
# to more than this many days in the past is assumed to belong to next year.
_PAST_TOLERANCE_DAYS = 120

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_MONTHS.update({k[:3]: v for k, v in _MONTHS.items()})
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))


def clean(text: str | None) -> str:
    """Collapse whitespace and strip the decorative separators CCA injects."""
    if not text:
        return ""
    text = text.replace("\u00a0", " ")
    # Strip non-ASCII bullet/diamond separators used between metadata fields.
    text = re.sub(r"[^\x00-\x7f]+", " ", text)
    return " ".join(text.split()).strip()


def infer_year(month: int, day: int, today: date | None = None) -> int:
    """Pick the year that makes a month/day land in the plausible near future."""
    today = today or date.today()
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue  # e.g. Feb 29 in a non-leap year
        if (today - candidate).days <= _PAST_TOLERANCE_DAYS:
            return year
    return today.year


def parse_date(text: str | None, today: date | None = None) -> date | None:
    """Parse the date formats these sites use. Returns None if nothing matches.

    Handles: '2026-08-09', '6/23/26', '8/5/2026', 'Wednesday, August 19',
    'August 19, 2026', 'Fri, Aug 21'.
    """
    if not text:
        return None
    s = clean(text)
    if not s:
        return None

    # ISO first (VolleyballLife API)
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # Numeric M/D/YY or M/D/YYYY (LeagueLab widget)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
    if m:
        mo, d, y = (int(g) for g in m.groups())
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    # Month-name form, optional trailing year
    m = re.search(rf"\b({_MONTH_RE})\w*\.?\s+(\d{{1,2}})(?:\s*,?\s*(\d{{4}}))?", s, re.I)
    if m:
        mo = _MONTHS[m.group(1).lower()[:3]]
        d = int(m.group(2))
        y = int(m.group(3)) if m.group(3) else infer_year(mo, d, today)
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def parse_time(text: str | None) -> time | None:
    """Parse a single clock time like '8:20 PM' or '7 PM'."""
    if not text:
        return None
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?", clean(text))
    if not m:
        return None
    hour = int(m.group(1)) % 12
    minute = int(m.group(2) or 0)
    if m.group(3).lower() == "p":
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def parse_time_range(text: str | None) -> tuple[time | None, time | None]:
    """Parse '8:20 PM - 9:40 PM' into start and end times.

    A single time yields (start, None); the caller decides how long to assume
    the event runs.
    """
    if not text:
        return None, None
    parts = re.split(r"\s*(?:-|–|—|to)\s*", clean(text), maxsplit=1)
    start = parse_time(parts[0]) if parts else None
    end = parse_time(parts[1]) if len(parts) > 1 else None
    return start, end


def parse_money(text: str | None) -> str | None:
    """Pull a price (or price range) out of free text."""
    if not text:
        return None
    prices = re.findall(r"\$[\d,]+(?:\.\d{2})?", text)
    if not prices:
        return None
    if len(prices) > 1 and prices[0] != prices[-1]:
        return f"{prices[0]} - {prices[-1]}"
    return prices[0]


def label_value(text: str, label: str) -> str | None:
    """Extract 'Label: value' out of a run-together metadata string.

    Stops at the next 'Word:' label so values don't swallow the next field.
    """
    m = re.search(
        rf"{re.escape(label)}\s*:?\s*(.*?)(?=\s+[A-Z][A-Za-z ]{{2,20}}:|$)",
        clean(text),
        re.I,
    )
    if not m:
        return None
    return m.group(1).strip(" :-,") or None


def detect_format(*texts: str | None) -> str | None:
    """Identify the play format (2s/4s/6s) from any of the given strings."""
    blob = " ".join(clean(t) for t in texts if t).lower()
    patterns = [
        (r"\b(6['\u2019]?s|6v6|6 v 6|sixes)\b", "6s"),
        (r"\b(4['\u2019]?s|4v4|4 v 4|quads)\b", "4s"),
        (r"\b(3['\u2019]?s|3v3|3 v 3|triples)\b", "3s"),
        (r"\b(2['\u2019]?s|2v2|2 v 2|doubles)\b", "2s"),
    ]
    for pattern, label in patterns:
        if re.search(pattern, blob):
            return label
    return None
