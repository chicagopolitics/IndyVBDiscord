"""Venue lookup for LeagueLab-hosted sites.

League listings only carry venue *ids* (``data-locationids="6648"``), which is
why iBeach leagues otherwise show up as the useless "Multiple Locations". Every
LeagueLab site publishes a ``/locations`` page containing one
``div.infoWindow#details_<id>`` block per venue, with its name and street
address, so one extra request per site resolves them all.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from .http import Fetcher
from .utils import clean

log = logging.getLogger(__name__)

# Some venues store "lat, lon" in the address field instead of a street address.
_COORD_RE = re.compile(r"^(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)$")


@dataclass
class Venue:
    """A physical place a league or tournament is played."""

    id: str = ""
    name: str = ""
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    @property
    def map_url(self) -> str | None:
        """A Google Maps link, preferring exact coordinates when we have them."""
        if self.latitude is not None and self.longitude is not None:
            query = f"{self.latitude},{self.longitude}"
        elif self.address:
            query = self.address
        elif self.name:
            query = self.name
        else:
            return None
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"

    def describe(self) -> str:
        """Name plus address, for a one-line display."""
        if self.address and self.address != self.name:
            return f"{self.name} - {self.address}"
        return self.name


@dataclass
class LocationDirectory:
    """Lazily-loaded venue lookup for one LeagueLab site."""

    url: str
    _venues: dict[str, Venue] = field(default_factory=dict)
    _loaded: bool = False

    def load(self, fetcher: Fetcher) -> None:
        """Fetch and parse the venue directory once per run.

        A failure here must not break the scrape: venue detail is an
        enhancement, and events are still perfectly usable without it.
        """
        if self._loaded:
            return
        self._loaded = True
        try:
            self._venues = self.parse(fetcher.get_text(self.url))
            log.info("loaded %d venues from %s", len(self._venues), self.url)
        except Exception as exc:  # noqa: BLE001 - enhancement only, never fatal
            log.warning("could not load venues from %s: %s", self.url, exc)
            self._venues = {}

    @staticmethod
    def parse(html: str) -> dict[str, Venue]:
        soup = BeautifulSoup(html, "lxml")
        venues: dict[str, Venue] = {}
        for block in soup.select("div.infoWindow[id^=details_]"):
            venue_id = block.get("id", "").removeprefix("details_")
            heading = block.find(["h3", "h2", "h4"])
            name = clean(heading.get_text(" ")) if heading else ""
            if not venue_id or not name:
                continue

            address, latitude, longitude = None, None, None
            address_el = block.select_one(".address")
            if address_el:
                raw = clean(address_el.get_text(" "))
                coords = _COORD_RE.match(raw)
                if coords:
                    latitude, longitude = float(coords.group(1)), float(coords.group(2))
                elif raw:
                    address = raw

            venues[venue_id] = Venue(id=venue_id, name=name, address=address,
                                     latitude=latitude, longitude=longitude)
        return venues

    def lookup(self, ids: list[str]) -> list[Venue]:
        """Resolve venue ids, skipping any the directory does not know."""
        found = []
        for venue_id in ids:
            venue = self._venues.get(venue_id)
            if venue:
                found.append(venue)
            elif self._venues:
                log.debug("unknown venue id %s at %s", venue_id, self.url)
        return found

    def __len__(self) -> int:
        return len(self._venues)


def parse_id_list(value: str | None) -> list[str]:
    """Split a comma-separated id attribute, ignoring blanks."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def summarize(venues: list[Venue]) -> str | None:
    """Join venue names for the single-line ``location`` field."""
    names = [v.name for v in venues if v.name]
    return ", ".join(dict.fromkeys(names)) or None
