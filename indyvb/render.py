"""Turn Events into Discord embeds (and plain text for previewing locally).

Discord enforces hard limits that silently reject a whole message when broken,
so the chunking here is deliberate rather than incidental:

* 10 embeds per message
* 6000 characters total across all embeds in a message
* 4096 per description, 1024 per field value, 256 per title
"""
from __future__ import annotations

from datetime import date

from .models import Event

MAX_EMBEDS_PER_MESSAGE = 10
MAX_CHARS_PER_MESSAGE = 6000
MAX_TITLE = 256
MAX_DESC = 4096
MAX_FIELD_VALUE = 1024

# Per-source accent colors, so the channel is scannable at a glance.
SOURCE_COLORS = {
    "cca": 0x990000,              # CCA red
    "cca-tournaments": 0xCC3311,
    "ibeach-leagues": 0x004276,   # iBeach navy
    "ibeach-tournaments": 0x0088CC,
}
DEFAULT_COLOR = 0x5865F2

KIND_EMOJI = {"league": "\U0001f3d0", "tournament": "\U0001f3c6"}  # volleyball, trophy

STATUS_EMOJI = {
    "open": "\U0001f7e2",       # green circle
    "sold out": "\U0001f534",   # red circle
    "closed": "⚪",         # white circle
    "completed": "⚪",
}


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fmt(value: date) -> str:
    # strftime("%-d") is not portable to Windows, so add the day number by hand.
    return f"{value.strftime('%a, %b')} {value.day}"


def date_line(event: Event, today: date | None = None) -> str:
    """Human date plus a countdown, e.g. 'Wed, Aug 19 (in 3 days)'."""
    if not event.start_date:
        return "Date TBD"
    today = today or date.today()
    text = _fmt(event.start_date)
    if event.end_date and event.end_date != event.start_date:
        text += f" – {_fmt(event.end_date)}"

    days = (event.start_date - today).days
    if days == 0:
        text += " (today)"
    elif days == 1:
        text += " (tomorrow)"
    elif 1 < days <= 60:
        text += f" (in {days} days)"
    elif days < 0:
        text += " (started)"
    return text


def status_line(event: Event) -> str:
    """Availability, splitting team vs individual when they differ."""
    if event.status_team and event.status_individual and \
            event.status_team != event.status_individual:
        return (f"{_status_chip(event.status_team)} Teams: {event.status_team}  ·  "
                f"{_status_chip(event.status_individual)} Individual: {event.status_individual}")
    status = event.status or event.status_team or event.status_individual
    return f"{_status_chip(status)} {status}" if status else ""


def _status_chip(status: str | None) -> str:
    return STATUS_EMOJI.get((status or "").strip().lower(), "⚪")


def venue_lines(event: Event, max_venues: int = 3) -> list[str]:
    """Location lines: venue name, address, and a directions link.

    Falls back to the plain location string for sources whose venues we cannot
    resolve, so nothing is ever worse than before.
    """
    if not event.venues:
        return [f"\U0001f4cd {event.location}"] if event.location else []

    lines = []
    for venue in event.venues[:max_venues]:
        label = venue.name
        if venue.address:
            label += f" - {venue.address}"
        if venue.map_url:
            label = f"[{label}]({venue.map_url})"
        lines.append(f"\U0001f4cd {label}")
    if len(event.venues) > max_venues:
        lines.append(f"   +{len(event.venues) - max_venues} more location(s)")
    return lines


def price_line(event: Event) -> str:
    parts = []
    if event.price_team:
        parts.append(f"Team {event.price_team}")
    if event.price_individual:
        parts.append(f"Individual {event.price_individual}")
    return "  ·  ".join(parts)


def event_embed(event: Event, today: date | None = None) -> dict:
    """One rich embed for a single listing."""
    emoji = KIND_EMOJI.get(event.kind, "\U0001f3d0")
    title = f"{emoji} {event.name}"

    lines = [f"**{date_line(event, today)}**"]
    if event.days and not event.end_date:
        lines.append(f"Plays {', '.join(event.days)}"
                     + (f" · {event.times}" if event.times else ""))
    lines.extend(venue_lines(event))
    if status := status_line(event):
        lines.append(status)
    if prices := price_line(event):
        lines.append(f"\U0001f4b5 {prices}")
    if event.registration_deadline:
        lines.append(f"⏰ Register by {_fmt(event.registration_deadline)}")
    if event.divisions:
        lines.append(f"Divisions: {', '.join(event.divisions[:6])}")

    embed = {
        "title": truncate(title, MAX_TITLE),
        "description": truncate("\n".join(lines), MAX_DESC),
        "color": SOURCE_COLORS.get(event.source, DEFAULT_COLOR),
        "footer": {"text": truncate(event.source_name, 2048)},
    }
    if event.url:
        embed["url"] = event.url
    return embed


def digest_embeds(events: list[Event], today: date | None = None) -> list[dict]:
    """Compact view: one embed per source, each listing as a single line."""
    today = today or date.today()
    by_source: dict[str, list[Event]] = {}
    for event in sorted(events, key=lambda e: e.sort_key()):
        by_source.setdefault(event.source, []).append(event)

    embeds = []
    for slug, group in by_source.items():
        lines = []
        for event in group:
            emoji = KIND_EMOJI.get(event.kind, "\U0001f3d0")
            chip = _status_chip(event.status)
            name = f"[{truncate(event.name, 90)}]({event.url})" if event.url \
                else truncate(event.name, 90)
            detail = " · ".join(x for x in [
                _fmt(event.start_date) if event.start_date else "TBD",
                event.location,
                event.price_individual or event.price_team,
            ] if x)
            lines.append(f"{emoji} {chip} {name}\n {detail}")

        embeds.append({
            "title": truncate(f"{group[0].source_name} ({len(group)})", MAX_TITLE),
            "description": truncate("\n".join(lines), MAX_DESC),
            "color": SOURCE_COLORS.get(slug, DEFAULT_COLOR),
        })
    return embeds


def _embed_size(embed: dict) -> int:
    """Approximate the character count Discord charges against the 6000 cap."""
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    for field in embed.get("fields", []):
        total += len(field.get("name", "")) + len(field.get("value", ""))
    return total


def chunk_embeds(embeds: list[dict]) -> list[list[dict]]:
    """Split embeds into batches that fit Discord per-message limits."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for embed in embeds:
        embed_size = _embed_size(embed)
        too_many = len(current) >= MAX_EMBEDS_PER_MESSAGE
        too_big = size + embed_size > MAX_CHARS_PER_MESSAGE
        if current and (too_many or too_big):
            batches.append(current)
            current, size = [], 0
        current.append(embed)
        size += embed_size
    if current:
        batches.append(current)
    return batches


def to_text(events: list[Event], today: date | None = None) -> str:
    """Plain-text rendering for terminal preview."""
    if not events:
        return "(no events)"
    today = today or date.today()
    out = []
    by_source: dict[str, list[Event]] = {}
    for event in sorted(events, key=lambda e: e.sort_key()):
        by_source.setdefault(event.source_name, []).append(event)

    for source_name, group in by_source.items():
        out.append(f"\n{source_name}  ({len(group)})")
        out.append("-" * max(24, len(source_name) + 8))
        for event in group:
            head = f"  {date_line(event, today)}  |  {event.name}"
            out.append(head)
            bits = [b for b in [
                event.location,
                ", ".join(event.days) if event.days else None,
                event.times,
                price_line(event),
                (event.status or ""),
            ] if b]
            if bits:
                out.append("      " + "  |  ".join(bits))
            if event.url:
                out.append(f"      {event.url}")
    return "\n".join(out)
