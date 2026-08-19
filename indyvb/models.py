"""Normalized event model shared by every source adapter."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Literal

from .locations import Venue

# "event" covers one-off sessions (e.g. GroupMe pickup games) that are
# neither a multi-week league nor a bracketed tournament.
Kind = Literal["league", "tournament", "event"]

# Fields that, when changed, mean the listing is meaningfully different and
# worth re-announcing. Cosmetic fields (description, tags) are deliberately
# excluded so copy edits upstream don't spam the channel.
_FINGERPRINT_FIELDS = (
    "name", "start_date", "end_date", "status", "status_team",
    "status_individual", "registration_deadline", "price_team",
    "price_individual", "location", "days", "times", "divisions",
)


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class Event:
    """A single league or tournament listing, normalized across sources."""

    source: str                  # short slug, e.g. "cca"
    source_name: str             # display name, e.g. "CCA Sports"
    kind: Kind
    source_id: str               # stable id from the upstream system
    name: str
    url: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    days: list[str] = field(default_factory=list)
    times: str | None = None
    location: str | None = None          # venue name(s), for one-line display
    address: str | None = None
    # Structured venues, when the source lets us resolve them. Richer than the
    # flat fields above: a bot can offer a map link per venue.
    venues: list[Venue] = field(default_factory=list)
    status: str | None = None            # Open / Closed / Sold Out / ...
    # LeagueLab sells team and individual spots separately, and it is common for
    # teams to sell out while individual spots stay open. Tracked apart so the
    # Discord post can say which one is still available.
    status_team: str | None = None
    status_individual: str | None = None
    registration_deadline: date | None = None
    price_team: str | None = None
    price_individual: str | None = None
    divisions: list[str] = field(default_factory=list)
    play_format: str | None = None       # "2s", "4s", "6s", "Coed 6's", ...
    description: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def uid(self) -> str:
        """Globally unique, stable across runs. Used as the dedupe key."""
        return f"{self.source}:{self.kind}:{self.source_id}"

    @property
    def fingerprint(self) -> str:
        """Hash of the fields worth re-announcing on change."""
        payload = {f: getattr(self, f) for f in _FINGERPRINT_FIELDS}
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def is_open(self) -> bool:
        return (self.status or "").strip().lower() in {"open", "openings", "available"}

    def is_upcoming(self, today: date | None = None) -> bool:
        """True when this hasn't finished yet. Undated events count as upcoming."""
        today = today or date.today()
        end = self.end_date or self.start_date
        return end is None or end >= today

    @property
    def map_url(self) -> str | None:
        """Directions link for the primary venue, if one is known."""
        return self.venues[0].map_url if self.venues else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)  # asdict recurses into the Venue dataclasses
        for k in ("start_date", "end_date", "registration_deadline"):
            d[k] = _iso(getattr(self, k))
        d["uid"] = self.uid
        d["fingerprint"] = self.fingerprint
        # Derived values, exported so downstream consumers need no extra logic.
        d["map_url"] = self.map_url
        for venue, raw in zip(self.venues, d["venues"]):
            raw["map_url"] = venue.map_url
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        d = dict(d)
        for key in ("uid", "fingerprint", "map_url"):
            d.pop(key, None)
        for k in ("start_date", "end_date", "registration_deadline"):
            if d.get(k):
                d[k] = datetime.fromisoformat(d[k]).date()
            else:
                d[k] = None
        d["venues"] = [
            Venue(**{k: v for k, v in raw.items() if k != "map_url"})
            for raw in d.get("venues") or []
        ]
        return cls(**d)

    def sort_key(self) -> tuple:
        # Undated events sort last rather than crashing the comparison.
        return (self.start_date or date.max, self.source, self.name)
