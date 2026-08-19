"""GroupMe calendar-event source tests.

The fixture is built from the documented event schema rather than a captured
live response, since the calendar endpoints need a personal token. Field names
follow groupme-js/GroupMeCommunityDocs.
"""
import json
from datetime import date
from pathlib import Path

import pytest
import requests

from indyvb.sources.groupme import (GroupList, GroupMeClient, GroupMeError,
                                    GroupMeEvents, MonitoredGroup)
from indyvb.tags import derive_tags

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def raw_events():
    payload = json.loads((FIXTURES / "groupme_events.json").read_text(encoding="utf-8"))
    return payload["response"]["events"]


@pytest.fixture(scope="module")
def group():
    return MonitoredGroup(id="104857392", name="Indy Pickup Volleyball", enabled=True)


@pytest.fixture(scope="module")
def events(raw_events, group):
    source = GroupMeEvents(token="t")
    return [e for e in (source.parse_event(r, group) for r in raw_events) if e]


class StubFetcher:
    """Serves queued payloads and records the requests made."""

    def __init__(self, payloads=None, error=None):
        self.payloads = list(payloads or [])
        self.error = error
        self.calls = []

    def get_json(self, url, headers=None, log_url=None, **kwargs):
        self.calls.append({"url": url, "headers": headers or {}, "log_url": log_url})
        if self.error and len(self.calls) == 1:
            raise self.error
        return self.payloads.pop(0) if self.payloads else {"response": None}


class TestParsing:
    def test_skips_events_without_a_name(self, events):
        """The fixture has one nameless entry that must be dropped."""
        assert len(events) == 5
        assert all(e.name for e in events)

    def test_kind_and_uid(self, events):
        event = events[0]
        assert event.kind == "event"
        assert event.uid == "groupme:event:01HQ8Z4K2M3N4P5Q6R7S8T9U"

    def test_converts_utc_to_local_date_and_time(self, events):
        """23:00 UTC is 7pm the previous day in Indianapolis."""
        event = events[0]
        assert event.start_date == date(2026, 9, 10)
        assert event.times == "7:00 PM - 9:30 PM"

    def test_all_day_event_has_no_times(self, events):
        event = next(e for e in events if e.name == "Open Gym Weekend")
        assert event.times is None
        assert event.start_date == date(2026, 10, 3)
        assert event.end_date == date(2026, 10, 4)

    def test_single_day_event_has_no_end_date(self, events):
        assert events[0].end_date is None

    def test_venue_with_address_and_coordinates(self, events):
        venue = events[0].venues[0]
        assert venue.name == "iBeach31"
        assert venue.address.startswith("750 E 181st St")
        assert venue.latitude == pytest.approx(40.0505522)
        assert venue.map_url.startswith("https://www.google.com/maps/")

    def test_unusable_coordinates_are_dropped(self, events):
        """lat/lng arrive as strings and are not always numeric."""
        event = next(e for e in events if e.name == "Sunday Sand Session")
        venue = event.venues[0]
        assert venue.latitude is None and venue.longitude is None
        # Falls back to searching by name so the map link still works.
        assert venue.map_url is not None

    def test_venue_without_address(self, events):
        event = next(e for e in events if e.name == "Open Gym Weekend")
        assert event.venues[0].name == "Jordan YMCA"
        assert event.venues[0].address is None

    def test_rsvp_count_added_to_description(self, events):
        assert "4 going" in events[0].description

    def test_no_rsvp_line_when_nobody_is_going(self, events):
        event = next(e for e in events if e.name == "Grass Doubles Tourney")
        assert "going" not in (event.description or "")

    def test_play_format_detected(self, events):
        assert events[0].play_format == "4s"

    def test_source_name_includes_the_group(self, events):
        assert events[0].source_name == "GroupMe - Indy Pickup Volleyball"

    def test_missing_start_is_skipped(self, group):
        assert GroupMeEvents(token="t").parse_event(
            {"event_id": "x", "name": "No date"}, group) is None

    def test_unknown_timezone_falls_back(self, group):
        event = GroupMeEvents(token="t").parse_event({
            "event_id": "z", "name": "Odd TZ",
            "start_at": "2026-09-10T23:00:00Z", "timezone": "Mars/Olympus",
        }, group)
        assert event.start_date == date(2026, 9, 10)


class TestTagging:
    def test_pickup_session_is_open_play(self, events):
        assert "Open Play" in derive_tags(events[0])

    def test_sand_derived_from_venue(self, events):
        assert "Sand" in derive_tags(events[0])

    def test_grass_event_gets_grass_tag(self, events):
        """No scraped source ever produces Grass; GroupMe is where it comes from."""
        event = next(e for e in events if e.name == "Grass Doubles Tourney")
        assert "Grass" in derive_tags(event)

    def test_tourney_in_title_overrides_open_play(self, events):
        event = next(e for e in events if e.name == "Grass Doubles Tourney")
        tags = derive_tags(event)
        assert "Tournament" in tags
        assert "Open Play" not in tags

    def test_ymca_reads_as_indoor(self, events):
        event = next(e for e in events if e.name == "Open Gym Weekend")
        assert "Indoor" in derive_tags(event)

    def test_every_event_is_taggable(self, events):
        """The forum requires a tag, so none of these may come back empty."""
        for event in events:
            assert derive_tags(event)


class TestGroupList:
    def test_new_groups_arrive_disabled(self, tmp_path):
        """Discovery must never silently opt a group in."""
        group_list = GroupList()
        added, total = group_list.merge([
            MonitoredGroup("1", "Volleyball"), MonitoredGroup("2", "Work Chat")])
        assert (added, total) == (2, 2)
        assert group_list.enabled == []

    def test_merge_preserves_existing_choices(self, tmp_path):
        path = tmp_path / "groups.json"
        group_list = GroupList([MonitoredGroup("1", "Volleyball", enabled=True)])
        group_list.merge([MonitoredGroup("1", "Volleyball"), MonitoredGroup("2", "New")])
        assert [g.id for g in group_list.enabled] == ["1"]

    def test_merge_refreshes_renamed_groups(self):
        group_list = GroupList([MonitoredGroup("1", "Old Name", enabled=True)])
        group_list.merge([MonitoredGroup("1", "New Name")])
        assert group_list.groups[0].name == "New Name"
        assert group_list.groups[0].enabled is True

    def test_round_trips_through_a_file(self, tmp_path):
        path = tmp_path / "groups.json"
        GroupList([MonitoredGroup("1", "Volleyball", enabled=True),
                   MonitoredGroup("2", "Work", enabled=False)]).save(path)
        loaded = GroupList.load(path)
        assert [g.id for g in loaded.enabled] == ["1"]

    def test_missing_file_explains_the_fix(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="groupme-groups"):
            GroupList.load(tmp_path / "nope.json")


class TestSource:
    def test_no_enabled_groups_yields_nothing(self, tmp_path):
        path = tmp_path / "groups.json"
        GroupList([MonitoredGroup("1", "Volleyball", enabled=False)]).save(path)
        source = GroupMeEvents(token="t", groups_config=path)
        assert source.fetch(StubFetcher()) == []

    def test_only_enabled_groups_are_queried(self, tmp_path, raw_events):
        """The allowlist is the guardrail on a token that can read everything."""
        path = tmp_path / "groups.json"
        GroupList([MonitoredGroup("111", "Watched", enabled=True),
                   MonitoredGroup("222", "Ignored", enabled=False)]).save(path)

        fetcher = StubFetcher([{"response": {"events": raw_events}}])
        events = GroupMeEvents(token="t", groups_config=path).fetch(fetcher)

        assert len(fetcher.calls) == 1
        assert "/conversations/111/events/list" in fetcher.calls[0]["url"]
        assert "222" not in fetcher.calls[0]["url"]
        assert len(events) == 5

    def test_allows_being_empty(self):
        """`health` must not treat a quiet week as a broken parser."""
        assert GroupMeEvents(token="t").allow_empty is True


class TestClient:
    def test_token_goes_in_the_header_not_the_url(self):
        fetcher = StubFetcher([{"response": []}])
        GroupMeClient("secret-token", fetcher).list_groups()
        call = fetcher.calls[0]
        assert call["headers"]["X-Access-Token"] == "secret-token"
        assert "secret-token" not in call["url"]

    def test_missing_token_is_reported(self):
        with pytest.raises(GroupMeError, match="GROUPME_ACCESS_TOKEN"):
            GroupMeClient("", StubFetcher())

    def test_falls_back_to_query_token_on_401(self):
        response = requests.Response()
        response.status_code = 401
        error = requests.HTTPError(response=response)

        fetcher = StubFetcher([{"response": []}], error=error)
        GroupMeClient("secret-token", fetcher).list_groups()

        assert len(fetcher.calls) == 2
        # The retry carries the token, but the logged URL must not.
        assert "secret-token" in fetcher.calls[1]["url"]
        assert "secret-token" not in fetcher.calls[1]["log_url"]
        assert "REDACTED" in fetcher.calls[1]["log_url"]

    def test_other_errors_are_not_retried(self):
        response = requests.Response()
        response.status_code = 500
        fetcher = StubFetcher(error=requests.HTTPError(response=response))
        with pytest.raises(GroupMeError, match="500"):
            GroupMeClient("t", fetcher).list_groups()

    def test_group_listing_pages_until_exhausted(self):
        page = [{"id": str(i), "name": f"Group {i}"} for i in range(100)]
        fetcher = StubFetcher([
            {"response": page},
            {"response": [{"id": "extra", "name": "Last"}]},
        ])
        groups = GroupMeClient("t", fetcher).list_groups()
        assert len(groups) == 101
        assert len(fetcher.calls) == 2


class TestUnconfigured:
    """Not being set up must never break the other sources or commands."""

    def test_no_token_is_inert(self, tmp_path):
        source = GroupMeEvents(token="", groups_config=tmp_path / "groups.json")
        fetcher = StubFetcher()
        assert source.fetch(fetcher) == []
        assert fetcher.calls == []

    def test_missing_group_file_is_inert(self, tmp_path):
        source = GroupMeEvents(token="t", groups_config=tmp_path / "nope.json")
        assert source.fetch(StubFetcher()) == []

    def test_safe_fetch_reports_no_error_when_unconfigured(self, tmp_path):
        """A source failure aborts `post --new-only`, so this must stay clean."""
        source = GroupMeEvents(token="", groups_config=tmp_path / "nope.json")
        events, error = source.safe_fetch(StubFetcher())
        assert events == []
        assert error is None


class TestRealWorldShape:
    """Cases taken from live API responses rather than the documented schema."""

    @pytest.fixture
    def open_play(self, events):
        return next(e for e in events if e.name == "Adult Open Play 9/25")

    def test_uses_the_share_url_deep_link(self, open_play):
        assert open_play.url.startswith("https://groupme.com/join_event/")

    def test_falls_back_when_no_share_url(self, events):
        assert events[0].url == "https://groupme.com/events"

    def test_null_location_is_survivable(self, open_play):
        """Many real events carry location: null, with the venue only in text."""
        assert open_play.venues == []
        assert open_play.location is None

    def test_still_taggable_without_a_venue(self, open_play):
        """The forum requires a tag, so a venue-less event must still get one."""
        assert derive_tags(open_play) == ["Open Play"]

    def test_going_count_preferred_over_the_going_list(self, open_play):
        """going_count is authoritative; the going list can be trimmed."""
        assert "58 going" in open_play.description

    def test_offset_timestamps_parse(self, open_play):
        assert open_play.start_date == date(2026, 9, 25)
        assert open_play.times == "7:00 PM - 11:00 PM"


class TestSubgroups:
    """GroupMe channels hold their own events under their own conversation id."""

    def test_channels_are_discovered_under_their_parent(self):
        fetcher = StubFetcher([
            {"response": [{"id": "1", "name": "Volleyball", "children_count": 2}]},
            {"response": [{"id": "10", "name": "Outside Events"},
                          {"id": "11", "name": "Off-topic"}]},
        ])
        groups = GroupMeClient("t", fetcher).list_groups()

        assert [g.id for g in groups] == ["1", "10", "11"]
        assert groups[1].parent == "Volleyball"
        assert groups[1].label == "Volleyball / Outside Events"
        assert "/groups/1/subgroups" in fetcher.calls[1]["url"]

    def test_no_request_when_a_group_has_no_channels(self):
        """children_count avoids a pointless call for most groups."""
        fetcher = StubFetcher([
            {"response": [{"id": "1", "name": "Plain", "children_count": 0}]},
        ])
        groups = GroupMeClient("t", fetcher).list_groups()
        assert len(groups) == 1
        assert len(fetcher.calls) == 1

    def test_channel_listing_failure_is_not_fatal(self):
        """Losing channels must not lose the parent groups too."""
        class Flaky(StubFetcher):
            def get_json(self, url, headers=None, log_url=None, **kwargs):
                if "subgroups" in url:
                    response = requests.Response()
                    response.status_code = 500
                    raise requests.HTTPError(response=response)
                return super().get_json(url, headers, log_url, **kwargs)

        fetcher = Flaky([{"response": [{"id": "1", "name": "V", "children_count": 3}]}])
        groups = GroupMeClient("t", fetcher).list_groups()
        assert [g.id for g in groups] == ["1"]

    def test_channels_are_enabled_independently(self, tmp_path):
        """A channel is opted in separately from its parent."""
        path = tmp_path / "groups.json"
        GroupList([
            MonitoredGroup("1", "Volleyball", enabled=False),
            MonitoredGroup("10", "Outside Events", enabled=True, parent="Volleyball"),
        ]).save(path)
        assert [g.id for g in GroupList.load(path).enabled] == ["10"]

    def test_parent_survives_a_round_trip(self, tmp_path):
        path = tmp_path / "groups.json"
        GroupList([MonitoredGroup("10", "Chan", enabled=True, parent="Parent")]).save(path)
        assert GroupList.load(path).groups[0].parent == "Parent"

    def test_merge_keeps_channel_choices(self):
        group_list = GroupList([
            MonitoredGroup("10", "Outside Events", enabled=True, parent="Volleyball")])
        group_list.merge([
            MonitoredGroup("1", "Volleyball"),
            MonitoredGroup("10", "Outside Events", parent="Volleyball"),
        ])
        assert [g.id for g in group_list.enabled] == ["10"]

    def test_source_name_shows_the_channel(self, raw_events):
        channel = MonitoredGroup("10", "Outside Events", True, parent="Volleyball")
        event = GroupMeEvents(token="t").parse_event(raw_events[0], channel)
        assert event.source_name == "GroupMe - Volleyball / Outside Events"
