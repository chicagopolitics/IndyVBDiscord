"""Venue directory tests, including the join back onto league listings."""
from pathlib import Path

import pytest

from indyvb.locations import (LocationDirectory, Venue, parse_id_list,
                              summarize)
from indyvb.sources.leaguelab import CCALeagues, IBeachLeagues

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


class StubFetcher:
    """Serves a fixture instead of hitting the network."""

    def __init__(self, text="", fail=False):
        self.text = text
        self.fail = fail
        self.calls = 0

    def get_text(self, url, **kwargs):
        self.calls += 1
        if self.fail:
            raise OSError("network down")
        return self.text


@pytest.fixture(scope="module")
def cca_venues():
    return LocationDirectory.parse(read("cca_locations.html"))


@pytest.fixture(scope="module")
def ibeach_venues():
    return LocationDirectory.parse(read("ibeach_locations.html"))


class TestParsing:
    def test_parses_all_venues(self, cca_venues, ibeach_venues):
        assert len(cca_venues) > 100
        assert len(ibeach_venues) == 8

    def test_reads_name_and_address(self, cca_venues):
        venue = cca_venues["6648"]
        assert venue.name == "First Baptist Fieldhouse"
        assert venue.address == "8600 N College Ave, Indianapolis, IN 46240"

    def test_coordinates_are_not_mistaken_for_an_address(self, cca_venues):
        """Some venues put "lat, lon" in the address field."""
        venue = cca_venues["4762"]
        assert venue.name == "Alexander Park"
        assert venue.address is None
        assert venue.latitude == pytest.approx(39.846047)
        assert venue.longitude == pytest.approx(-86.016085)

    def test_ignores_unrelated_html(self):
        assert LocationDirectory.parse("<html><body>nope</body></html>") == {}


class TestMapUrl:
    def test_prefers_coordinates(self):
        venue = Venue(name="Park", address="1 Main St", latitude=39.5, longitude=-86.1)
        assert "39.5%2C-86.1" in venue.map_url

    def test_falls_back_to_address(self):
        venue = Venue(name="Gym", address="8600 N College Ave")
        assert "8600+N+College+Ave" in venue.map_url

    def test_falls_back_to_name(self):
        assert "Some+Gym" in Venue(name="Some Gym").map_url

    def test_none_when_nothing_to_search(self):
        assert Venue().map_url is None

    def test_describe_avoids_duplicating_name(self):
        assert Venue(name="Gym", address="Gym").describe() == "Gym"
        assert Venue(name="Gym", address="1 Main St").describe() == "Gym - 1 Main St"


class TestDirectory:
    def test_loads_once(self):
        directory = LocationDirectory("https://example.com/locations")
        fetcher = StubFetcher(read("ibeach_locations.html"))
        directory.load(fetcher)
        directory.load(fetcher)
        assert fetcher.calls == 1
        assert len(directory) == 8

    def test_load_failure_is_not_fatal(self):
        """Venue detail is an enhancement; losing it must not break the scrape."""
        directory = LocationDirectory("https://example.com/locations")
        directory.load(StubFetcher(fail=True))
        assert len(directory) == 0
        assert directory.lookup(["6648"]) == []

    def test_lookup_skips_unknown_ids(self):
        directory = LocationDirectory("https://example.com/locations")
        directory.load(StubFetcher(read("ibeach_locations.html")))
        found = directory.lookup(["3711", "does-not-exist"])
        assert [v.name for v in found] == ["iBeach"]


class TestHelpers:
    @pytest.mark.parametrize("value,expected", [
        ("6392,4513", ["6392", "4513"]),
        ("6648", ["6648"]),
        ("", []),
        (None, []),
        (" 1 , , 2 ", ["1", "2"]),
    ])
    def test_parse_id_list(self, value, expected):
        assert parse_id_list(value) == expected

    def test_summarize_dedupes_and_joins(self):
        venues = [Venue(name="A"), Venue(name="B"), Venue(name="A")]
        assert summarize(venues) == "A, B"

    def test_summarize_empty(self):
        assert summarize([]) is None


class TestJoinedOntoEvents:
    """The whole point: turn venue ids on a listing into real addresses."""

    def test_ibeach_multiple_locations_is_replaced(self):
        source = IBeachLeagues()
        source.venues.load(StubFetcher(read("ibeach_locations.html")))
        events = source.parse(read("ibeach_widget.html"))

        league = next(e for e in events if e.source_id == "101701")
        assert league.location == "iBeach, iBeach Indoor Courts"
        assert league.address == "750 E 181st, Westfield, IN 46074"
        assert league.map_url.startswith("https://www.google.com/maps/")
        # The unhelpful placeholder must be gone everywhere.
        assert not any(e.location == "Multiple Locations" for e in events)

    def test_cca_resolves_multi_venue_league(self):
        source = CCALeagues()
        source.venues.load(StubFetcher(read("cca_locations.html")))
        events = source.parse(read("cca_leagues.html"))

        league = next(e for e in events if e.source_id == "101595")
        assert [v.name for v in league.venues] == [
            "Sense Charter School", "The Salvation Army - Fountain Square"]
        assert league.venues[0].address == "1601 Barth Ave, Indianapolis, IN 46203"

    def test_every_cca_league_resolves_a_venue(self):
        source = CCALeagues()
        source.venues.load(StubFetcher(read("cca_locations.html")))
        for event in source.parse(read("cca_leagues.html")):
            assert event.venues, f"no venue resolved for {event.name}"
            assert event.map_url

    def test_falls_back_to_listing_text_without_directory(self):
        """With no directory loaded, behaviour matches the pre-venue version."""
        events = CCALeagues().parse(read("cca_leagues.html"))
        league = next(e for e in events if e.source_id == "102721")
        assert league.venues == []
        assert league.location == "First Baptist Fieldhouse"
