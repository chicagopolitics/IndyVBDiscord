"""iCalendar (RFC 5545) export.

Produces a .ics file that Google/Apple/Outlook Calendar can subscribe to by URL.
The fiddly parts of the format are handled deliberately:

* lines are folded at 75 octets, without splitting a multi-byte character
* TEXT values escape backslash, semicolon, comma and newline
* UIDs are derived from the stable upstream id, so re-publishing the feed
  updates existing entries instead of creating duplicates
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import Event
from .utils import parse_time_range

# Everything these sites list is played in the Indianapolis area.
LOCAL_TZ = ZoneInfo("America/Indiana/Indianapolis")

PRODID = "-//IndyVB//Volleyball Scraper//EN"
DEFAULT_CALENDAR_NAME = "Indy Volleyball"

# Assumed length of a session when a source gives a start time but no end.
DEFAULT_DURATION = timedelta(hours=2)

# How often subscribing clients are asked to refresh.
REFRESH_INTERVAL = "PT6H"

MAX_LINE_OCTETS = 75


def escape_text(value: str) -> str:
    """Escape a TEXT value per RFC 5545 section 3.3.11."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold_line(line: str) -> list[str]:
    """Split one content line into folded chunks of at most 75 octets.

    Folding is measured in octets, not characters, and a multi-byte character
    must never be split across the boundary.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= MAX_LINE_OCTETS:
        return [line]

    chunks: list[str] = []
    remaining = encoded
    limit = MAX_LINE_OCTETS
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining.decode("utf-8"))
            break
        cut = limit
        # Back off until the chunk ends on a valid character boundary.
        while cut > 0:
            try:
                chunk = remaining[:cut].decode("utf-8")
                break
            except UnicodeDecodeError:
                cut -= 1
        else:  # pragma: no cover - unreachable for valid input
            chunk = remaining[:limit].decode("utf-8", "ignore")
        chunks.append(chunk)
        remaining = remaining[cut:]
        # Continuation lines start with a space, which costs one octet.
        limit = MAX_LINE_OCTETS - 1
    return chunks


class _Builder:
    """Accumulates content lines and folds them on output."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, name: str, content: str, **params: str) -> None:
        """Add a property. Keyword args become parameters, e.g. value="DATE".

        The positional argument is named ``content`` rather than ``value`` so
        that a ``VALUE=`` parameter can be passed as a keyword.
        """
        if content is None or content == "":
            return
        prefix = name
        for key, param in params.items():
            prefix += f";{key.upper().replace('_', '-')}={param}"
        parts = fold_line(f"{prefix}:{content}")
        self.lines.append(parts[0])
        self.lines.extend(" " + part for part in parts[1:])

    def raw(self, line: str) -> None:
        self.lines.append(line)

    def render(self) -> str:
        # RFC 5545 requires CRLF line endings.
        return "\r\n".join(self.lines) + "\r\n"


def _utc_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _date_value(value: date) -> str:
    return value.strftime("%Y%m%d")


def _event_times(event: Event) -> tuple[datetime, datetime] | None:
    """Resolve a timed start/end, or None when this should be all-day."""
    if not event.start_date or not event.times:
        return None
    start_time, end_time = parse_time_range(event.times)
    if not start_time:
        return None

    start = datetime.combine(event.start_date, start_time, tzinfo=LOCAL_TZ)
    if end_time:
        end = datetime.combine(event.start_date, end_time, tzinfo=LOCAL_TZ)
        # An end before the start means the session runs past midnight.
        if end <= start:
            end += timedelta(days=1)
    else:
        end = start + DEFAULT_DURATION
    return start, end


def _description(event: Event) -> str:
    """Human-readable body, mirroring what the Discord embed shows."""
    lines = []
    if event.days:
        line = f"Plays {', '.join(event.days)}"
        if event.times:
            line += f" at {event.times}"
        lines.append(line)

    availability = []
    if event.status_team and event.status_individual and \
            event.status_team != event.status_individual:
        availability.append(f"Teams: {event.status_team}")
        availability.append(f"Individual: {event.status_individual}")
    elif event.status:
        availability.append(f"Status: {event.status}")
    lines.extend(availability)

    prices = []
    if event.price_team:
        prices.append(f"Team {event.price_team}")
    if event.price_individual:
        prices.append(f"Individual {event.price_individual}")
    if prices:
        lines.append("Cost: " + ", ".join(prices))

    if event.registration_deadline:
        lines.append(f"Register by {event.registration_deadline:%a, %b %d}")
    if event.divisions:
        lines.append("Divisions: " + ", ".join(event.divisions))
    # Venues at one site often repeat the same address; show each map once.
    for map_url in dict.fromkeys(v.map_url for v in event.venues if v.map_url):
        lines.append(f"Map: {map_url}")
    if event.url:
        lines.append(f"Sign up: {event.url}")
    lines.append(f"Source: {event.source_name}")
    return "\n".join(lines)


def _location(event: Event) -> str | None:
    """Prefer a full street address so calendar apps can offer directions."""
    if event.venues:
        parts = [v.describe() for v in event.venues if v.name]
        if parts:
            return "; ".join(parts)
    if event.address:
        return f"{event.location} - {event.address}" if event.location else event.address
    return event.location


def _vevent(event: Event, stamp: datetime) -> list[str]:
    builder = _Builder()
    builder.raw("BEGIN:VEVENT")
    builder.add("UID", f"{event.uid}@indyvb")
    builder.add("DTSTAMP", _utc_stamp(stamp))

    times = _event_times(event)
    if times:
        start, end = times
        builder.add("DTSTART", _utc_stamp(start))
        builder.add("DTEND", _utc_stamp(end))
    else:
        start_date = event.start_date
        # DTEND is exclusive for all-day events, so add a day.
        end_date = (event.end_date or start_date) + timedelta(days=1)
        builder.add("DTSTART", _date_value(start_date), value="DATE")
        builder.add("DTEND", _date_value(end_date), value="DATE")

    builder.add("SUMMARY", escape_text(event.name))
    builder.add("DESCRIPTION", escape_text(_description(event)))
    location = _location(event)
    if location:
        builder.add("LOCATION", escape_text(location))
    if event.url:
        builder.add("URL", event.url)

    # CATEGORIES is a comma-separated *list*, so each item is escaped
    # individually and the separating commas are left intact.
    categories = [c for c in [event.kind, event.play_format, event.source_name] if c]
    builder.add("CATEGORIES", ",".join(escape_text(c) for c in categories))

    venue = event.venues[0] if event.venues else None
    if venue and venue.latitude is not None and venue.longitude is not None:
        builder.add("GEO", f"{venue.latitude};{venue.longitude}")

    # Sold-out listings stay on the calendar but are marked free/transparent.
    builder.add("TRANSP", "OPAQUE" if event.is_open else "TRANSPARENT")
    builder.raw("END:VEVENT")
    return builder.lines


def _deadline_vevent(event: Event, stamp: datetime) -> list[str]:
    """An all-day entry on the signup deadline - the date people actually miss."""
    builder = _Builder()
    builder.raw("BEGIN:VEVENT")
    builder.add("UID", f"{event.uid}-deadline@indyvb")
    builder.add("DTSTAMP", _utc_stamp(stamp))
    deadline = event.registration_deadline
    builder.add("DTSTART", _date_value(deadline), value="DATE")
    builder.add("DTEND", _date_value(deadline + timedelta(days=1)), value="DATE")
    builder.add("SUMMARY", escape_text(f"Signup deadline: {event.name}"))

    body = [f"Registration closes today for {event.name}."]
    if event.start_date:
        body.append(f"League starts {event.start_date:%a, %b %d}.")
    if event.url:
        body.append(f"Sign up: {event.url}")
    builder.add("DESCRIPTION", escape_text("\n".join(body)))
    if event.url:
        builder.add("URL", event.url)
    builder.add("CATEGORIES", "deadline")
    builder.add("TRANSP", "TRANSPARENT")
    builder.raw("END:VEVENT")
    return builder.lines


def to_ics(events: list[Event], calendar_name: str = DEFAULT_CALENDAR_NAME,
           include_deadlines: bool = True, now: datetime | None = None) -> str:
    """Render events as a subscribable iCalendar document."""
    stamp = now or datetime.now(timezone.utc)

    builder = _Builder()
    builder.raw("BEGIN:VCALENDAR")
    builder.add("VERSION", "2.0")
    builder.add("PRODID", PRODID)
    builder.add("CALSCALE", "GREGORIAN")
    builder.add("METHOD", "PUBLISH")
    # X-WR-* are non-standard but are what Google and Apple read for the
    # display name and refresh cadence of a subscribed calendar.
    builder.add("X-WR-CALNAME", escape_text(calendar_name))
    builder.add("X-WR-TIMEZONE", str(LOCAL_TZ))
    builder.add("X-PUBLISHED-TTL", REFRESH_INTERVAL)
    builder.add("REFRESH-INTERVAL", REFRESH_INTERVAL, value="DURATION")

    for event in sorted(events, key=lambda e: e.sort_key()):
        if not event.start_date and not event.registration_deadline:
            continue  # nothing to place on a calendar
        if event.start_date:
            builder.lines.extend(_vevent(event, stamp))
        if include_deadlines and event.registration_deadline:
            builder.lines.extend(_deadline_vevent(event, stamp))

    builder.raw("END:VCALENDAR")
    return builder.render()
