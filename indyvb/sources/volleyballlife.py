"""VolleyballLife tournaments.

ibeachvolleyball.volleyballlife.com is a single-page app that reads from a
public JSON API, so this source talks to that API directly instead of trying
to scrape the rendered page.
"""
from __future__ import annotations

from ..http import Fetcher
from ..locations import Venue
from ..models import Event
from ..utils import clean, detect_format, parse_date
from .base import Source


def _coordinates(coordinates: list, index: int) -> dict:
    """Parse the API's space-separated "lat lon" string at the given index."""
    if index >= len(coordinates):
        return {}
    parts = str(coordinates[index] or "").split()
    if len(parts) != 2:
        return {}
    try:
        return {"latitude": float(parts[0]), "longitude": float(parts[1])}
    except ValueError:
        return {}

# Observed values. 100 appears on events that have already been played; the
# meaning of other codes is unconfirmed, so they are reported as unknown
# rather than guessed at.
_STATUS_BY_ID = {100: "Completed"}

# Tags the API attaches that describe the play format.
_FORMAT_TAGS = {"1s", "2s", "3s", "4s", "6s"}


class VolleyballLifeTournaments(Source):
    slug = "ibeach-tournaments"
    name = "iBeach Volleyball (Tournaments)"
    org = "ibeachvolleyball"
    homepage = "https://ibeachvolleyball.volleyballlife.com"
    api = "https://api-v8.volleyballlife.com/tournament/summaries"

    @property
    def url(self) -> str:
        return f"{self.api}?filter=upcoming&includeDivisionMeta=true"

    def fetch(self, fetcher: Fetcher) -> list[Event]:
        payload = fetcher.get_json(
            self.url,
            headers={
                # The API is public, but it expects to be called from the app.
                "Origin": self.homepage,
                "Referer": f"{self.homepage}/",
            },
        )
        return self.parse(payload)

    def parse(self, payload: list[dict]) -> list[Event]:
        events = []
        for item in payload or []:
            event = self._parse_item(item)
            if event:
                events.append(event)
        return events

    def _parse_item(self, item: dict) -> Event | None:
        tid = item.get("id")
        name = clean(item.get("name"))
        if tid is None or not name:
            return None

        # The endpoint is not scoped to one organization, so filter explicitly
        # rather than assuming every result belongs to iBeach.
        org = item.get("organization") or {}
        if org.get("username") and org["username"] != self.org:
            return None

        if item.get("isPublic") is False:
            return None

        locations = [clean(x) for x in (item.get("locations") or []) if clean(x)]
        addresses = [clean(x) for x in (item.get("locationAddresses") or []) if clean(x)]
        tags = [clean(t) for t in (item.get("tags") or []) if clean(t)]

        # This API is the only source that hands us exact coordinates, which
        # make for a more reliable map link than an address string.
        venues = [
            Venue(
                id=f"{tid}-{index}",
                name=name_,
                address=addresses[index] if index < len(addresses) else None,
                **_coordinates(item.get("coordinates") or [], index),
            )
            for index, name_ in enumerate(locations)
        ]

        play_format = next((t for t in tags if t.lower() in _FORMAT_TAGS), None)
        if not play_format:
            formats = [clean(d.get("format")) for d in (item.get("divisionMeta") or [])]
            play_format = detect_format(name, *formats)

        # Drop the noisier auto-generated tags (year, month, weekday, ids, and
        # the org name) so what reaches Discord stays readable.
        noisy = {self.org, clean(org.get("name")).lower(), str(tid), "volleyball"}
        keep = [
            t for t in tags
            if t.lower() not in noisy
            and not t.isdigit()
            and t not in locations
        ]

        return Event(
            source=self.slug,
            source_name=self.name,
            kind="tournament",
            source_id=str(tid),
            name=name,
            url=f"{self.homepage}/event/{tid}",
            start_date=parse_date(item.get("startDate")),
            end_date=parse_date(item.get("endDate")),
            location=", ".join(locations) or None,
            address=addresses[0] if addresses else None,
            venues=venues,
            status=_STATUS_BY_ID.get(item.get("statusId")),
            divisions=[clean(d) for d in (item.get("divisionNames") or []) if clean(d)],
            play_format=play_format,
            tags=keep[:8],
        )
