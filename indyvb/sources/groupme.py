"""GroupMe calendar events.

GroupMe groups can hold first-class calendar events (groupme.com/events) with a
name, start and end time, and a location including coordinates. Those are
structured records, so this is an ordinary source adapter - no text extraction
from chat messages is involved, and chat messages are never read.

Only groups explicitly enabled in ``data/groupme_groups.json`` are queried. The
access token can technically read every group and DM on the account, so the
allowlist is what keeps this tool scoped to what was deliberately chosen.

The calendar endpoints are absent from the official v3 documentation; they are
described by the community project at groupme-js/GroupMeCommunityDocs.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from ..http import Fetcher
from ..locations import Venue
from ..models import Event
from ..utils import clean, detect_format
from .base import Source

log = logging.getLogger(__name__)

API_ROOT = "https://api.groupme.com/v3"
DEFAULT_GROUPS_CONFIG = Path("data/groupme_groups.json")

# Events are played locally, so fall back to Indianapolis when an event carries
# no timezone of its own.
FALLBACK_TZ = ZoneInfo("America/Indiana/Indianapolis")

# `limit` is required by the endpoint; GroupMe defaults to 20 and we want the
# full upcoming picture in one call.
EVENT_LIMIT = 100
GROUP_PAGE_SIZE = 100


class GroupMeError(RuntimeError):
    pass


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        log.debug("unparseable GroupMe timestamp %r", value)
        return None


def _zone_for(name: str | None):
    if not name:
        return FALLBACK_TZ
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.debug("unknown timezone %r, using local", name)
        return FALLBACK_TZ


def _clock(moment: datetime) -> str:
    """12-hour time without a platform-specific strftime directive."""
    hour = moment.hour % 12 or 12
    suffix = "AM" if moment.hour < 12 else "PM"
    return f"{hour}:{moment.minute:02d} {suffix}"


@dataclass
class MonitoredGroup:
    id: str
    name: str
    enabled: bool = False


class GroupList:
    """The hand-curated allowlist of groups to monitor."""

    def __init__(self, groups: list[MonitoredGroup] | None = None):
        self.groups = groups or []

    @classmethod
    def load(cls, path: Path | str = DEFAULT_GROUPS_CONFIG) -> "GroupList":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"No GroupMe group list at {path}. Run "
                f"`python -m indyvb.cli groupme-groups --save` first, then set "
                f"\"enabled\": true on the groups you want monitored."
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls([
            MonitoredGroup(id=str(g["id"]), name=g.get("name", ""),
                           enabled=bool(g.get("enabled", False)))
            for g in raw.get("groups", [])
        ])

    def save(self, path: Path | str = DEFAULT_GROUPS_CONFIG) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"groups": [
            {"id": g.id, "name": g.name, "enabled": g.enabled}
            for g in sorted(self.groups, key=lambda g: g.name.lower())
        ]}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def merge(self, discovered: list[MonitoredGroup]) -> tuple[int, int]:
        """Fold freshly discovered groups into the list.

        New groups arrive disabled, so re-running discovery can never silently
        opt a group in. Existing choices are preserved and names refreshed.
        """
        by_id = {g.id: g for g in self.groups}
        added = 0
        for group in discovered:
            existing = by_id.get(group.id)
            if existing:
                existing.name = group.name or existing.name
            else:
                by_id[group.id] = MonitoredGroup(group.id, group.name, enabled=False)
                added += 1
        self.groups = list(by_id.values())
        return added, len(self.groups)

    @property
    def enabled(self) -> list[MonitoredGroup]:
        return [g for g in self.groups if g.enabled]


class GroupMeClient:
    """Minimal GroupMe API client.

    The token is sent in the ``X-Access-Token`` header, which is what the
    official docs require and keeps it out of logs and cache filenames. Some
    of the undocumented calendar endpoints are reported to accept only a
    ``token`` query parameter, so a 401 triggers one retry that way, with the
    token redacted from the logged URL.
    """

    def __init__(self, token: str, fetcher: Fetcher):
        if not token:
            raise GroupMeError(
                "No GroupMe token configured. Set GROUPME_ACCESS_TOKEN in your "
                ".env (get it by signing in at https://dev.groupme.com)."
            )
        self.token = token
        self.fetcher = fetcher

    def get(self, path: str, params: dict | None = None):
        url = f"{API_ROOT}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            payload = self.fetcher.get_json(
                url, headers={"X-Access-Token": self.token})
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status != 401:
                raise GroupMeError(f"GroupMe request failed ({status}): {path}") from exc
            # Retry with the token in the query string, redacted when logged.
            joiner = "&" if params else "?"
            payload = self.fetcher.get_json(
                f"{url}{joiner}token={quote(self.token)}",
                log_url=f"{url}{joiner}token=REDACTED",
            )
        return (payload or {}).get("response")

    def list_groups(self) -> list[MonitoredGroup]:
        """Every group the token can see, paged until exhausted."""
        found: list[MonitoredGroup] = []
        page = 1
        while True:
            batch = self.get("/groups", {
                "page": page, "per_page": GROUP_PAGE_SIZE, "omit": "memberships",
            }) or []
            for group in batch:
                found.append(MonitoredGroup(
                    id=str(group.get("id")), name=clean(group.get("name"))))
            if len(batch) < GROUP_PAGE_SIZE:
                return found
            page += 1

    def list_events(self, group_id: str, since: datetime | None = None) -> list[dict]:
        since = since or datetime.now(timezone.utc)
        # The parameter is named end_at, but per the community docs it bounds
        # which upcoming events are returned rather than acting as an end date.
        response = self.get(f"/conversations/{group_id}/events/list", {
            "end_at": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "limit": EVENT_LIMIT,
        })
        if isinstance(response, dict):
            return response.get("events") or []
        return response or []


class GroupMeEvents(Source):
    slug = "groupme"
    name = "GroupMe"
    homepage = "https://groupme.com"
    url = f"{API_ROOT}/conversations/:group_id/events/list"
    event_kind = "event"
    # A quiet week genuinely has no scheduled events, unlike a scraper whose
    # empty result means its markup changed.
    allow_empty = True

    def __init__(self, token: str | None = None,
                 groups_config: Path | str = DEFAULT_GROUPS_CONFIG):
        import os
        self.token = token if token is not None else os.getenv("GROUPME_ACCESS_TOKEN", "")
        self.groups_config = groups_config

    def fetch(self, fetcher: Fetcher) -> list[Event]:
        # Not being set up is a normal state, not a failure. Raising here would
        # make every other command noisy, and would abort `post --new-only`,
        # which refuses to record state when any source errors.
        if not self.token:
            log.info("GROUPME_ACCESS_TOKEN not set; skipping GroupMe")
            return []
        try:
            group_list = GroupList.load(self.groups_config)
        except FileNotFoundError:
            log.info("no GroupMe group list at %s; skipping GroupMe",
                     self.groups_config)
            return []

        enabled = group_list.enabled
        if not enabled:
            log.info("no GroupMe groups enabled in %s", self.groups_config)
            return []

        client = GroupMeClient(self.token, fetcher)
        events: list[Event] = []
        for group in enabled:
            raw = client.list_events(group.id)
            log.info("groupme %s (%s): %d events", group.name, group.id, len(raw))
            for item in raw:
                event = self.parse_event(item, group)
                if event:
                    events.append(event)
        return events

    def parse_event(self, item: dict, group: MonitoredGroup | None = None) -> Event | None:
        event_id = item.get("event_id") or item.get("id")
        name = clean(item.get("name"))
        if not event_id or not name:
            return None

        zone = _zone_for(item.get("timezone"))
        start = _parse_iso(item.get("start_at"))
        end = _parse_iso(item.get("end_at"))
        if not start:
            return None
        start = start.astimezone(zone)
        end = end.astimezone(zone) if end else None

        is_all_day = bool(item.get("is_all_day"))
        times = None
        if not is_all_day:
            times = _clock(start)
            if end and end > start:
                times = f"{times} - {_clock(end)}"

        end_date = end.date() if end and end.date() != start.date() else None

        venues = []
        location = item.get("location") or {}
        venue_name = clean(location.get("name"))
        address = clean(location.get("address")) or None
        if venue_name or address:
            venues.append(Venue(
                id=f"groupme-{event_id}",
                name=venue_name or address,
                address=address if address != venue_name else None,
                **_coordinates(location),
            ))

        description = clean(item.get("description")) or None
        rsvp = len(item.get("going") or [])
        if rsvp:
            note = f"{rsvp} going"
            description = f"{description}\n{note}" if description else note

        return Event(
            source=self.slug,
            source_name=f"{self.name} - {group.name}" if group and group.name else self.name,
            kind=self.event_kind,
            source_id=str(event_id),
            name=name,
            url=f"{self.homepage}/events",
            start_date=start.date(),
            end_date=end_date,
            times=times,
            location=venue_name or address or None,
            address=address,
            venues=venues,
            play_format=detect_format(name, description),
            description=description,
            tags=[group.name] if group and group.name else [],
        )


def _coordinates(location: dict) -> dict:
    """GroupMe sends lat/lng as strings; drop them if they are unusable."""
    try:
        return {
            "latitude": float(location["lat"]),
            "longitude": float(location["lng"]),
        }
    except (KeyError, TypeError, ValueError):
        return {}
