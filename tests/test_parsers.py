"""Parser tests against saved copies of the real pages.

Fixtures are captured responses, so these run offline and catch regressions in
our parsing. They cannot detect the sites changing their markup - that is what
`indyvb health` is for.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from indyvb.sources.leaguelab import CCALeagues, CCATournaments, IBeachLeagues
from indyvb.sources.volleyballlife import VolleyballLifeTournaments

FIXTURES = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def cca_leagues():
    return CCALeagues().parse(read("cca_leagues.html"))


@pytest.fixture(scope="module")
def cca_tournaments():
    return CCATournaments().parse(read("cca_tournaments.html"))


@pytest.fixture(scope="module")
def ibeach_leagues():
    return IBeachLeagues().parse(read("ibeach_widget.html"))


@pytest.fixture(scope="module")
def vbl_tournaments():
    payload = json.loads((FIXTURES / "vbl.json").read_text(encoding="utf-8"))
    return VolleyballLifeTournaments().parse(payload)


class TestCCALeagues:
    def test_finds_only_volleyball(self, cca_leagues):
        # The page also lists other sports; they must be filtered out.
        assert len(cca_leagues) == 20
        assert all(e.kind == "league" for e in cca_leagues)
        assert all(e.source == "cca" for e in cca_leagues)

    def test_every_league_has_core_fields(self, cca_leagues):
        for e in cca_leagues:
            assert e.name and e.source_id and e.url
            assert e.start_date is not None, f"no start date: {e.name}"
            assert e.price_individual, f"no individual price: {e.name}"

    def test_parses_a_known_league(self, cca_leagues):
        league = next(e for e in cca_leagues if e.source_id == "102721")
        assert league.name.startswith("Fall 2026 Wednesday")
        assert league.start_date == date(2026, 8, 19)
        assert league.registration_deadline == date(2026, 8, 19)
        assert league.days == ["Wednesday"]
        assert league.location == "First Baptist Fieldhouse"
        assert league.play_format == "6s"
        assert league.divisions == ["Recreational", "Intermediate", "Sub List"]
        assert league.url.endswith("/league/102721/details")

    def test_team_and_individual_status_tracked_apart(self, cca_leagues):
        """Teams sold out while individual spots remain is the common case."""
        league = next(e for e in cca_leagues if e.source_id == "102721")
        assert league.status_team == "Sold Out"
        assert league.status_individual == "Open"
        assert league.status == "Open"

    def test_sub_list_price_excluded_from_headline(self, cca_leagues):
        """The cheap sub-list slot must not distort the advertised price."""
        league = next(e for e in cca_leagues if e.source_id == "102721")
        assert league.price_individual == "$78.00"
        assert league.price_team == "$438.00"


class TestCCATournaments:
    def test_finds_tournaments(self, cca_tournaments):
        assert len(cca_tournaments) == 6
        assert all(e.kind == "tournament" for e in cca_tournaments)

    def test_parses_past_started_wording(self, cca_tournaments):
        """Past events say 'Started:' where upcoming ones say 'Starts:'."""
        june = next(e for e in cca_tournaments if e.source_id == "102363")
        assert june.start_date == date(2026, 6, 19)

    def test_open_tournament(self, cca_tournaments):
        august = next(e for e in cca_tournaments if e.source_id == "102365")
        assert august.start_date == date(2026, 8, 21)
        assert august.status_team == "Open"
        assert august.price_team == "$150.00"
        assert august.play_format == "6s"

    def test_not_yet_open_is_closed_not_sold_out(self, cca_tournaments):
        """Registration that has not opened must not read as 'Sold Out'."""
        october = next(e for e in cca_tournaments if e.source_id == "102366")
        assert october.status_team == "Closed"


class TestIBeachLeagues:
    def test_parses_widget_rows(self, ibeach_leagues):
        assert len(ibeach_leagues) == 48
        assert all(e.source == "ibeach-leagues" for e in ibeach_leagues)

    def test_reads_fields_by_label(self, ibeach_leagues):
        league = next(e for e in ibeach_leagues if e.source_id == "101701")
        assert league.start_date == date(2026, 6, 23)
        assert league.registration_deadline == date(2026, 8, 5)
        assert league.days == ["Tuesday"]
        assert league.times == "9:00 PM - 9:40 PM"
        assert league.price_team == "$162.00"
        assert league.price_individual == "$81.00"
        assert league.status == "Closed"
        assert league.play_format == "2s"

    def test_all_rows_have_dates_and_urls(self, ibeach_leagues):
        assert all(e.start_date for e in ibeach_leagues)
        assert all(e.url and e.url.startswith("http") for e in ibeach_leagues)


class TestVolleyballLife:
    def test_parses_api_payload(self, vbl_tournaments):
        assert len(vbl_tournaments) == 17
        assert all(e.kind == "tournament" for e in vbl_tournaments)

    def test_parses_a_known_tournament(self, vbl_tournaments):
        t = next(e for e in vbl_tournaments if e.source_id == "40178")
        assert t.name == "Bluefire Quads Classic"
        assert t.start_date == date(2026, 9, 12)
        assert t.location == "iBeach31"
        assert t.address.startswith("750 E 181st St")
        assert t.play_format == "4s"
        assert t.divisions == ["Coed A", "Coed BB", "Coed B"]
        assert t.url.endswith("/event/40178")

    def test_filters_other_organizations(self):
        """The endpoint is not org-scoped, so foreign events must be dropped."""
        payload = [{
            "id": 1, "name": "Someone Else's Event", "startDate": "2026-09-01",
            "organization": {"username": "another-org"}, "isPublic": True,
        }]
        assert VolleyballLifeTournaments().parse(payload) == []

    def test_skips_private_events(self):
        payload = [{
            "id": 2, "name": "Private", "startDate": "2026-09-01",
            "organization": {"username": "ibeachvolleyball"}, "isPublic": False,
        }]
        assert VolleyballLifeTournaments().parse(payload) == []

    def test_survives_missing_optional_fields(self):
        payload = [{
            "id": 3, "name": "Bare Minimum",
            "organization": {"username": "ibeachvolleyball"},
        }]
        events = VolleyballLifeTournaments().parse(payload)
        assert len(events) == 1
        assert events[0].start_date is None


def test_uids_are_unique_across_all_sources(
        cca_leagues, cca_tournaments, ibeach_leagues, vbl_tournaments):
    """Dedupe depends on uids never colliding between sources."""
    events = cca_leagues + cca_tournaments + ibeach_leagues + vbl_tournaments
    uids = [e.uid for e in events]
    assert len(uids) == len(set(uids))


def test_parsers_return_empty_on_unrelated_html():
    """A redesigned page should yield nothing, not raise."""
    assert CCALeagues().parse("<html><body><p>hi</p></body></html>") == []
    assert IBeachLeagues().parse("<html><body><p>hi</p></body></html>") == []
