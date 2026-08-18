"""Discord scheduled events (the server's Events tab).

Creating a scheduled event is a plain REST call, so this needs a bot token but
no gateway connection and no discord.py dependency.

Discord assigns its own event ids, so to know which listing an existing event
came from, each description carries a hidden marker like
``[indyvb:cca:league:102721]``. Matching on that means the sync is stateless
and self-healing: no local mapping file to lose or corrupt.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from .ics import LOCAL_TZ
from .models import Event
from .utils import parse_time_range

log = logging.getLogger(__name__)

API_ROOT = "https://discord.com/api/v10"

# Discord's documented limits.
MAX_NAME = 100
MAX_DESCRIPTION = 1000
MAX_LOCATION = 100
MAX_EVENTS_PER_GUILD = 100

ENTITY_TYPE_EXTERNAL = 3
PRIVACY_LEVEL_GUILD_ONLY = 2

DEFAULT_DURATION = timedelta(hours=2)
# Where a listing has no start time, use a sensible evening slot rather than
# midnight, which reads as "yesterday" in most clients.
DEFAULT_START_TIME = 18

_MARKER_RE = re.compile(r"\[indyvb:([^\]]+)\]")


class DiscordEventError(RuntimeError):
    pass


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{len(self.created)} created, {len(self.updated)} updated, "
                f"{len(self.unchanged)} unchanged, {len(self.skipped)} skipped")


def marker_for(event: Event) -> str:
    return f"[indyvb:{event.uid}]"


def uid_from_description(description: str | None) -> str | None:
    match = _MARKER_RE.search(description or "")
    return match.group(1) if match else None


def event_window(event: Event) -> tuple[datetime, datetime] | None:
    """Absolute start/end for a listing, or None if it cannot be scheduled."""
    if not event.start_date:
        return None

    start_time, end_time = parse_time_range(event.times)
    if start_time:
        start = datetime.combine(event.start_date, start_time, tzinfo=LOCAL_TZ)
    else:
        start = datetime.combine(
            event.start_date, datetime.min.time(), tzinfo=LOCAL_TZ
        ).replace(hour=DEFAULT_START_TIME)

    if end_time:
        end = datetime.combine(event.start_date, end_time, tzinfo=LOCAL_TZ)
        if end <= start:
            end += timedelta(days=1)
    elif event.end_date and event.end_date != event.start_date:
        end = datetime.combine(
            event.end_date, datetime.min.time(), tzinfo=LOCAL_TZ
        ).replace(hour=22)
    else:
        end = start + DEFAULT_DURATION
    return start, end


def build_payload(event: Event) -> dict:
    """Map an Event onto Discord's scheduled-event schema."""
    window = event_window(event)
    if not window:
        raise ValueError("event has no usable date")
    start, end = window

    lines = []
    if event.days:
        line = f"Plays {', '.join(event.days)}"
        if event.times:
            line += f" at {event.times}"
        lines.append(line)
    if event.status_team and event.status_individual and \
            event.status_team != event.status_individual:
        lines.append(f"Teams: {event.status_team} | Individual: {event.status_individual}")
    elif event.status:
        lines.append(f"Status: {event.status}")
    prices = []
    if event.price_team:
        prices.append(f"Team {event.price_team}")
    if event.price_individual:
        prices.append(f"Individual {event.price_individual}")
    if prices:
        lines.append("Cost: " + ", ".join(prices))
    if event.registration_deadline:
        lines.append(f"Register by {event.registration_deadline:%a, %b %d}")
    if event.url:
        lines.append(f"Sign up: {event.url}")

    marker = marker_for(event)
    body = "\n".join(lines)
    # The marker must survive truncation, or the next sync creates a duplicate.
    budget = MAX_DESCRIPTION - len(marker) - 1
    if len(body) > budget:
        body = body[: budget - 1].rstrip() + "…"
    description = f"{body}\n{marker}" if body else marker

    location = event.location or "See listing"
    if event.venues and event.venues[0].address:
        combined = f"{event.venues[0].name} - {event.venues[0].address}"
        if len(combined) <= MAX_LOCATION:
            location = combined

    return {
        "name": event.name[:MAX_NAME],
        "description": description,
        "scheduled_start_time": start.astimezone(timezone.utc).isoformat(),
        "scheduled_end_time": end.astimezone(timezone.utc).isoformat(),
        "entity_type": ENTITY_TYPE_EXTERNAL,
        "privacy_level": PRIVACY_LEVEL_GUILD_ONLY,
        "entity_metadata": {"location": location[:MAX_LOCATION]},
    }


def _needs_update(existing: dict, payload: dict) -> bool:
    """Compare only the fields we manage, ignoring Discord's extra metadata."""
    if existing.get("name") != payload["name"]:
        return True
    if (existing.get("description") or "") != payload["description"]:
        return True
    for key in ("scheduled_start_time", "scheduled_end_time"):
        old, new = existing.get(key), payload[key]
        if old is None or _parse_iso(old) != _parse_iso(new):
            return True
    old_location = (existing.get("entity_metadata") or {}).get("location")
    return old_location != payload["entity_metadata"]["location"]


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class ScheduledEventSync:
    """Creates and updates scheduled events in one guild."""

    def __init__(self, token: str, guild_id: str,
                 session: requests.Session | None = None):
        if not token:
            raise DiscordEventError(
                "No bot token configured. Set DISCORD_BOT_TOKEN in your .env."
            )
        if not guild_id:
            raise DiscordEventError(
                "No guild id configured. Set DISCORD_GUILD_ID in your .env."
            )
        self.guild_id = str(guild_id)
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "IndyVB (https://github.com/, 0.1)",
        })

    @property
    def _base(self) -> str:
        return f"{API_ROOT}/guilds/{self.guild_id}/scheduled-events"

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        for attempt in range(1, 6):
            resp = self.session.request(method, url, timeout=30, **kwargs)
            if resp.status_code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(resp.json().get("retry_after", 1.0))
                except Exception:  # noqa: BLE001
                    pass
                log.warning("rate limited, sleeping %.2fs", retry_after)
                time.sleep(retry_after)
                continue
            if 500 <= resp.status_code < 600 and attempt < 5:
                time.sleep(2 ** attempt)
                continue
            return resp
        raise DiscordEventError("Gave up after repeated rate limiting.")

    def list_existing(self) -> list[dict]:
        resp = self._request("GET", self._base)
        if resp.status_code == 401:
            raise DiscordEventError("Discord rejected the bot token (401).")
        if resp.status_code == 403:
            raise DiscordEventError(
                "The bot lacks the Manage Events permission in this server (403)."
            )
        if resp.status_code == 404:
            raise DiscordEventError(
                "Guild not found (404). Check DISCORD_GUILD_ID and that the bot "
                "has been invited to that server."
            )
        if resp.status_code != 200:
            raise DiscordEventError(
                f"Could not list events ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def create(self, payload: dict) -> dict:
        resp = self._request("POST", self._base, json=payload)
        if resp.status_code not in (200, 201):
            raise DiscordEventError(
                f"Could not create event ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def update(self, event_id: str, payload: dict) -> dict:
        resp = self._request("PATCH", f"{self._base}/{event_id}", json=payload)
        if resp.status_code != 200:
            raise DiscordEventError(
                f"Could not update event ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def sync(self, events: list[Event], dry_run: bool = False,
             now: datetime | None = None) -> SyncResult:
        """Create or update a scheduled event for each listing."""
        now = now or datetime.now(timezone.utc)
        result = SyncResult()

        existing = self.list_existing()
        by_uid = {}
        for item in existing:
            uid = uid_from_description(item.get("description"))
            if uid:
                by_uid[uid] = item
        headroom = MAX_EVENTS_PER_GUILD - len(existing)

        for event in sorted(events, key=lambda e: e.sort_key()):
            window = event_window(event)
            if not window:
                result.skipped.append((event.name, "no date"))
                continue
            # Discord rejects a start time in the past.
            if window[0] <= now:
                result.skipped.append((event.name, "already started"))
                continue

            payload = build_payload(event)
            current = by_uid.get(event.uid)

            if current:
                if _needs_update(current, payload):
                    if not dry_run:
                        self.update(current["id"], payload)
                    result.updated.append(event.name)
                else:
                    result.unchanged.append(event.name)
                continue

            if headroom <= 0:
                result.skipped.append((event.name, "guild is at the 100-event cap"))
                continue
            if not dry_run:
                self.create(payload)
            headroom -= 1
            result.created.append(event.name)

        return result
