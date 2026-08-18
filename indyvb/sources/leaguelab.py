"""LeagueLab-backed sources.

Both CCA Sports (white-labeled at ccasports.com) and iBeach Volleyball run on
LeagueLab, but they expose it two different ways:

* CCA renders listings into its own site as ``div.league-listing``.
* iBeach embeds the hosted widget, which renders a table of
  ``tr.widget-league-listing`` rows. The widget ``sport`` query parameter is
  applied client-side, so a single request already returns every sport.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..http import Fetcher
from ..locations import LocationDirectory, parse_id_list, summarize
from ..models import Event
from ..utils import clean, detect_format, label_value, parse_date, parse_money
from .base import Source


def _division_label(opt) -> str:
    """Name of a division block, e.g. Recreational / Intermediate / Sub List."""
    heading = opt.find(["h4", "h5", "strong"])
    if heading:
        label = clean(heading.get_text(" "))
        if label:
            return label
    # No heading: the name is the text before the first "Level:" label.
    return clean(opt.get_text(" ")).split("Level:")[0].strip()


def _is_open(li) -> bool:
    """LeagueLab puts the availability straight on the class list."""
    return any(c.lower() == "open" for c in (li.get("class") or []))


def _slot_status(li) -> str:
    """Distinguish a full listing from one that has not opened for signups yet.

    Both render as a non-open class, so only the wording separates them.
    """
    if _is_open(li):
        return "Open"
    return "Sold Out" if "sold out" in clean(li.get_text(" ")).lower() else "Closed"


def _combine_status(statuses: list[str]) -> str | None:
    """Roll per-division availability into one headline status."""
    for preferred in ("Open", "Sold Out", "Closed"):
        if preferred in statuses:
            return preferred
    return None


def _total_price(text: str) -> str | None:
    """The full team cost, ignoring any smaller deposit figure alongside it."""
    m = re.search(r"\$([\d,]+(?:\.\d{2})?)\s*Total", text, re.I)
    return f"${m.group(1)}" if m else parse_money(text)


def _price_range(prices: list[str]) -> str | None:
    """Collapse per-division prices into one value, or a low - high range."""
    uniq = sorted(set(prices), key=lambda p: float(p.lstrip("$").replace(",", "")))
    if not uniq:
        return None
    return uniq[0] if len(uniq) == 1 else f"{uniq[0]} - {uniq[-1]}"


class _CCAListingPage(Source):
    """Shared parser for any CCA page built out of ``div.league-listing`` blocks.

    Their league index and their tournaments page use identical markup and
    differ only in the ``data-sport`` value, so both run through this.
    """

    homepage = "https://www.ccasports.com"
    url: str = ""
    sport: str = ""          # matched against data-sport
    event_kind: str = "league"

    def __init__(self):
        self.venues = LocationDirectory(f"{self.homepage}/locations")

    def fetch(self, fetcher: Fetcher) -> list[Event]:
        html = fetcher.get_text(self.url)
        self.venues.load(fetcher)
        return self.parse(html)

    def parse(self, html: str) -> list[Event]:
        soup = BeautifulSoup(html, "lxml")
        events = []
        for div in soup.select("div.league-listing[data-leagueid]"):
            if (div.get("data-sport") or "").strip().lower() != self.sport:
                continue
            event = self._parse_listing(div)
            if event:
                events.append(event)
        return events

    def _parse_listing(self, div) -> Event | None:
        league_id = clean(div.get("data-leagueid"))
        name = clean(div.get("data-leaguename"))
        if not league_id or not name:
            return None

        # "Day: Wednesday | Starts: Wednesday, August 19 | Deadline: ..."
        # Past events say "Started:" instead of "Starts:".
        days_el = div.select_one("li.days")
        meta = clean(days_el.get_text(" ")) if days_el else ""
        start = parse_date(label_value(meta, "Starts") or label_value(meta, "Started"))
        deadline = parse_date(label_value(meta, "Deadline"))

        location = None
        loc_el = div.select_one("li.locations")
        if loc_el:
            location = clean(loc_el.get_text(" "))
            for prefix in ("Locations:", "Location:"):
                location = location.removeprefix(prefix).strip()
            location = location or None

        # Resolve venue ids to names and street addresses where we can; fall
        # back to whatever the listing itself printed.
        venues = self.venues.lookup(parse_id_list(div.get("data-locationids")))
        location = summarize(venues) or location
        address = venues[0].address if venues else None

        # The blurb above the title describes the league type and roster size.
        description = None
        sport_el = div.select_one("div.sport")
        if sport_el:
            text = clean(sport_el.get_text(" "))
            # The block repeats the sport label before the real blurb.
            description = re.sub(rf"^{re.escape(self.sport)}\s*", "", text,
                                 flags=re.I).strip(" -") or None

        divisions, team_prices, indiv_prices = [], [], []
        team_statuses, indiv_statuses = [], []
        for opt in div.select("li.division-option"):
            label = _division_label(opt)
            if label and label.lower() not in {x.lower() for x in divisions}:
                divisions.append(label)
            # A "Sub List" is a cheap fill-in spot, not the league price, so it
            # would badly skew the headline cost if included.
            is_sub = "sub" in (label or "").lower()

            for li in opt.select("li.team"):
                team_statuses.append(_slot_status(li))
                if not is_sub:
                    price = _total_price(clean(li.get_text(" ")))
                    if price:
                        team_prices.append(price)
            for li in opt.select("li.individual"):
                indiv_statuses.append(_slot_status(li))
                if not is_sub:
                    price = parse_money(clean(li.get_text(" ")))
                    if price:
                        indiv_prices.append(price)

        status_team = _combine_status(team_statuses)
        status_individual = _combine_status(indiv_statuses)
        status = _combine_status(team_statuses + indiv_statuses)

        team_price = _price_range(team_prices)
        indiv_price = _price_range(indiv_prices)

        dates_el = div.select_one("p.dates")
        session_dates = clean(dates_el.get_text(" ")) if dates_el else None
        if session_dates:
            description = f"{description}\n{session_dates}" if description else session_dates

        days = [d.strip() for d in clean(div.get("data-days")).split(",") if d.strip()]

        return Event(
            source=self.slug,
            source_name=self.name,
            kind=self.event_kind,
            source_id=league_id,
            name=name,
            url=f"{self.homepage}/league/{league_id}/details",
            start_date=start,
            days=days,
            location=location,
            address=address,
            venues=venues,
            status=status,
            status_team=status_team,
            status_individual=status_individual,
            registration_deadline=deadline,
            price_team=team_price,
            price_individual=indiv_price,
            divisions=divisions,
            play_format=detect_format(name, description),
            description=description,
        )


class CCALeagues(_CCAListingPage):
    slug = "cca"
    name = "CCA Sports"
    url = "https://www.ccasports.com/leagues?v=upcoming&sport=Volleyball"
    sport = "volleyball"
    event_kind = "league"


class CCATournaments(_CCAListingPage):
    """CCA one-night tournaments.

    These live on a hand-curated page rather than the league index, but LeagueLab
    renders them with the same markup, tagged as ``data-sport="Tournament"``.
    """

    slug = "cca-tournaments"
    name = "CCA Sports (Tournaments)"
    url = "https://www.ccasports.com/page/Indoor-Volleyball-Tournaments"
    sport = "tournament"
    event_kind = "tournament"


class IBeachLeagues(Source):
    """The iBeach LeagueLab widget, which ibeachvolleyball.com embeds in an iframe.

    Fetched directly because ibeachvolleyball.com itself returns 403 to
    non-browser clients, and the widget is the actual data source anyway.
    """

    slug = "ibeach-leagues"
    name = "iBeach Volleyball (Leagues)"
    homepage = "https://ibeachvolleyball.leaguelab.com"
    url = "https://widget.leaguelab.com/v1/ibeachvolleyball/league-listing"

    def __init__(self):
        self.venues = LocationDirectory(f"{self.homepage}/locations")

    def fetch(self, fetcher: Fetcher) -> list[Event]:
        html = fetcher.get_text(self.url)
        self.venues.load(fetcher)
        return self.parse(html)

    def parse(self, html: str) -> list[Event]:
        soup = BeautifulSoup(html, "lxml")
        events = []
        for row in soup.select("tr.widget-league-listing"):
            event = self._parse_row(row, soup)
            if event:
                events.append(event)
        return events

    def _parse_row(self, row, soup) -> Event | None:
        row_id = row.get("id", "")
        m = re.search(r"widget-league-listing-(\d+)", row_id)
        if not m:
            return None
        league_id = m.group(1)

        # Cells are self-describing: each carries an inline-head label, so read
        # them by label instead of by fragile column position.
        fields: dict[str, str] = {}
        for cell in row.find_all("td"):
            head = cell.select_one("span.inline-head")
            if not head:
                continue
            head_text = clean(head.get_text(" "))
            label = head_text.rstrip(":").strip()
            value = clean(cell.get_text(" "))
            if value.startswith(head_text):
                value = value[len(head_text):].strip()
            if label:
                fields[label.lower()] = value

        name = fields.get("league name") or ""
        if not name:
            return None

        link = row.select_one("a.view-link, a.registration-link")
        url = link.get("href") if link else f"{self.homepage}/league/{league_id}/details"

        desc_row = soup.find(id=f"{row_id}-shortDescription")
        description = clean(desc_row.get_text(" ")) if desc_row else None

        days = [d.strip() for d in clean(row.get("data-days")).split(",") if d.strip()]
        sport = fields.get("sport") or clean(row.get("data-sports"))

        # The widget prints a useless "Multiple Locations" whenever a league
        # spans venues, so prefer the resolved names.
        venues = self.venues.lookup(parse_id_list(row.get("data-locations")))
        location = summarize(venues) or fields.get("location") or None

        return Event(
            source=self.slug,
            source_name=self.name,
            kind="league",
            source_id=league_id,
            name=name,
            url=url,
            start_date=parse_date(fields.get("start date")),
            days=days,
            times=fields.get("start time(s)") or None,
            location=location,
            address=venues[0].address if venues else None,
            venues=venues,
            status=fields.get("status") or clean(row.get("data-statuss")) or None,
            registration_deadline=parse_date(fields.get("signup deadline")),
            price_team=parse_money(fields.get("team price")),
            price_individual=parse_money(fields.get("individual price")),
            play_format=detect_format(sport, name, description),
            description=description,
            tags=[t for t in [sport, clean(row.get("data-genders"))] if t],
        )
