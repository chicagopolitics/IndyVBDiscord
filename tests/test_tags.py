"""Forum tag derivation and mapping tests."""
import json

import pytest

from indyvb.locations import Venue
from indyvb.models import Event
from indyvb.publish import (MAX_APPLIED_TAGS, MAX_THREAD_NAME,
                            ConsoleForumPublisher, ForumWebhookPublisher,
                            PublishError)
from indyvb.tags import (HARD_COURT, MAX_TAGS, TagLookupError, TagMap, derive_tags,
                         fetch_available_tags, surface_tags)

ALL_TAGS = {
    "Sand": "1", "Hard Court": "2", "Grass": "3", "Open Play": "4",
    "Doubles": "5", "Quads": "6", "Reverse Co-Ed": "7",
    "League": "8", "Tournament": "9",
}


def make_event(source="ibeach-leagues", name="Test League", **kwargs) -> Event:
    defaults = dict(
        source=source, source_name="Test", kind="league",
        source_id="1", name=name,
    )
    defaults.update(kwargs)
    return Event(**defaults)


class TestKindTags:
    def test_league(self):
        assert "League" in derive_tags(make_event(kind="league"))

    def test_tournament(self):
        tags = derive_tags(make_event(kind="tournament"))
        assert "Tournament" in tags and "League" not in tags

    def test_every_event_gets_at_least_one_tag(self):
        """The forum requires a tag, so an untagged post would be rejected."""
        assert derive_tags(make_event(name="", source="unknown"))


class TestFormatTags:
    @pytest.mark.parametrize("fmt,expected", [
        ("2s", "Doubles"),
        ("4s", "Quads"),
    ])
    def test_format_maps_to_tag(self, fmt, expected):
        assert expected in derive_tags(make_event(play_format=fmt))

    def test_sixes_has_no_format_tag(self):
        """The vocabulary has no Sixes tag, so 6s carries only kind/surface."""
        tags = derive_tags(make_event(play_format="6s"))
        assert "Doubles" not in tags and "Quads" not in tags


class TestModifierTags:
    def test_reverse_coed(self):
        event = make_event(name="Fall 2026 Wednesday Reverse Coed 4v4 Level A/BB")
        assert "Reverse Co-Ed" in derive_tags(event)

    def test_open_play(self):
        assert "Open Play" in derive_tags(make_event(name="Friday Adult Open Play"))

    def test_modifiers_absent_by_default(self):
        tags = derive_tags(make_event(name="Fall Coed 4v4 Level B"))
        assert "Reverse Co-Ed" not in tags and "Open Play" not in tags


class TestSurfaceTags:
    def test_cca_is_hard_court(self):
        assert surface_tags(make_event(source="cca")) == {HARD_COURT}

    def test_ibeach_sand_only_venue(self):
        event = make_event(venues=[Venue(id="1", name="iBeach")])
        assert surface_tags(event) == {"Sand"}

    def test_ibeach_indoor_courts_do_not_add_indoor(self):
        """iBeach is Sand only; its indoor courts are incidental overflow."""
        event = make_event(venues=[Venue(id="1", name="iBeach"),
                                   Venue(id="2", name="iBeach Indoor Courts")])
        assert surface_tags(event) == {"Sand"}

    def test_ibeach_tournaments_are_sand(self):
        event = make_event(source="ibeach-tournaments", kind="tournament",
                           venues=[Venue(id="1", name="iBeach31")])
        assert surface_tags(event) == {"Sand"}

    def test_grass_detected_from_text(self):
        event = make_event(source="cca", name="Summer Grass Doubles")
        assert "Grass" in surface_tags(event)

    def test_hard_court_wording_applies_to_non_fixed_sources(self):
        event = make_event(source="other", name="Winter League in the gym",
                           venues=[Venue(id="1", name="Some Gym")])
        assert HARD_COURT in surface_tags(event)

    def test_grass_wins_over_the_source_default(self):
        """A grass event at a sand organiser is Grass, not both."""
        event = make_event(name="Summer Grass Doubles")
        assert surface_tags(event) == {"Grass"}

    def test_indoor_sand_is_sand_not_hard_court(self):
        """Indoor says where, not what. iBeach has an indoor sand facility."""
        event = make_event(source="groupme", name="Winter Indoor Sand League",
                           venues=[Venue(id="1", name="Some Indoor Sand Facility")])
        assert surface_tags(event) == {"Sand"}

    def test_surface_is_never_contradictory(self):
        """Sand and Indoor are different surfaces, never both."""
        event = make_event(source="groupme", name="Indoor Sand Doubles at the gym")
        assert len(surface_tags(event)) <= 1

    def test_known_venue_overrides_an_unhelpful_name(self):
        """"The Academy Volleyball Club" is hard court despite the name."""
        event = make_event(source="groupme", name="Adult Open Play",
                           description="Location: The Academy Volleyball Club")
        assert surface_tags(event) == {HARD_COURT}

    def test_hard_court_signals(self):
        for venue in ["Jordan YMCA", "First Baptist Fieldhouse", "Some Gymnasium"]:
            event = make_event(source="groupme", name="Play",
                               venues=[Venue(id="1", name=venue)])
            assert surface_tags(event) == {HARD_COURT}, venue

    def test_unknown_venue_yields_no_surface(self):
        """Better to emit no surface than to guess wrong."""
        event = make_event(source="groupme", name="Pickup night",
                           venues=[Venue(id="1", name="Unknown Place")])
        assert surface_tags(event) == set()

    def test_ibeach_with_no_venues_defaults_to_sand(self):
        assert "Sand" in surface_tags(make_event(venues=[]))


class TestTagLimit:
    def test_never_exceeds_discord_maximum(self):
        event = make_event(
            name="Reverse Coed Open Play Grass Indoor 2v2",
            play_format="2s",
            venues=[Venue(id="1", name="iBeach"),
                    Venue(id="2", name="iBeach Indoor Courts")],
        )
        assert len(derive_tags(event)) <= MAX_TAGS

    def test_kind_survives_trimming(self):
        """Kind is first in priority, so it is never the tag that gets cut."""
        event = make_event(
            kind="tournament", name="Reverse Coed Grass 2v2 Showdown",
            play_format="2s",
            venues=[Venue(id="1", name="iBeach"),
                    Venue(id="2", name="iBeach Indoor Courts")],
        )
        assert "Tournament" in derive_tags(event)

    def test_open_play_in_the_title_beats_the_source_kind(self):
        """iBeach sells drop-in sessions as leagues; they are still open play."""
        event = make_event(kind="league", name="Friday Adult Open Play")
        tags = derive_tags(event)
        assert "Open Play" in tags
        assert "League" not in tags

    def test_source_kind_wins_when_the_title_is_ambiguous(self):
        """Only Open Play overrides the source; a stray word must not."""
        event = make_event(kind="tournament", name="Summer League Showdown")
        assert "Tournament" in derive_tags(event)


class TestTagMap:
    def test_resolves_names_to_ids(self):
        assert TagMap(ALL_TAGS).ids_for(["League", "Sand"]) == ["8", "1"]

    def test_is_case_insensitive(self):
        assert TagMap(ALL_TAGS).ids_for(["league"]) == ["8"]

    def test_unknown_names_are_skipped_not_fatal(self):
        assert TagMap(ALL_TAGS).ids_for(["League", "Nonexistent"]) == ["8"]

    def test_missing_reports_unmapped_names(self):
        assert TagMap({"League": "8"}).missing(["League", "Sand"]) == ["Sand"]

    def test_round_trips_through_a_file(self, tmp_path):
        path = tmp_path / "forum_tags.json"
        TagMap.save(ALL_TAGS, path)
        loaded = TagMap.load(path)
        assert len(loaded) == len(ALL_TAGS)
        assert loaded.ids_for(["Quads"]) == ["6"]

    def test_missing_file_explains_the_fix(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="forum-tags"):
            TagMap.load(tmp_path / "nope.json")

    def test_accepts_a_bare_mapping_file(self, tmp_path):
        """A hand-written file without the "tags" wrapper still loads."""
        path = tmp_path / "flat.json"
        path.write_text(json.dumps({"League": "8"}), encoding="utf-8")
        assert TagMap.load(path).ids_for(["League"]) == ["8"]


class TestForumPublisher:
    class FakeResponse:
        status_code = 204
        text = ""

        def json(self):
            return {}

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, timeout=None):
            self.calls.append(json)
            return TestForumPublisher.FakeResponse()

    def make(self):
        session = self.FakeSession()
        pub = ForumWebhookPublisher(
            "https://discord.com/api/webhooks/1/abc", session=session)
        return pub, session

    def test_sends_thread_name_and_tags(self, monkeypatch):
        monkeypatch.setattr("indyvb.publish.time.sleep", lambda *_: None)
        pub, session = self.make()
        pub.send_thread("Fall League", [{"title": "x"}], applied_tags=["8", "1"])
        sent = session.calls[0]
        assert sent["thread_name"] == "Fall League"
        assert sent["applied_tags"] == ["8", "1"]

    def test_truncates_long_thread_names(self, monkeypatch):
        monkeypatch.setattr("indyvb.publish.time.sleep", lambda *_: None)
        pub, session = self.make()
        pub.send_thread("L" * 300, [{"title": "x"}], applied_tags=["8"])
        assert len(session.calls[0]["thread_name"]) <= MAX_THREAD_NAME

    def test_caps_applied_tags(self, monkeypatch):
        monkeypatch.setattr("indyvb.publish.time.sleep", lambda *_: None)
        pub, session = self.make()
        pub.send_thread("x", [{"title": "x"}], applied_tags=list("123456789"))
        assert len(session.calls[0]["applied_tags"]) <= MAX_APPLIED_TAGS

    def test_plain_send_is_rejected(self):
        """A forum message without a thread name would be refused by Discord."""
        pub, _ = self.make()
        with pytest.raises(PublishError, match="thread name"):
            pub.send([{"title": "x"}])

    def test_mentions_still_suppressed(self, monkeypatch):
        monkeypatch.setattr("indyvb.publish.time.sleep", lambda *_: None)
        pub, session = self.make()
        pub.send_thread("x", [{"title": "x"}], applied_tags=["8"])
        assert session.calls[0]["allowed_mentions"] == {"parse": []}

    def test_console_publisher_records_without_sending(self):
        pub = ConsoleForumPublisher()
        pub.send_thread("Fall League", [{"title": "x"}], applied_tags=["8"])
        assert pub.messages[0]["thread_name"] == "Fall League"


class TestFetchAvailableTags:
    class Resp:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    class Session:
        def __init__(self, responses):
            self.responses = list(responses)

        def get(self, url, **kwargs):
            return self.responses.pop(0)

    def test_requires_a_bot_token(self):
        with pytest.raises(TagLookupError, match="bot token"):
            fetch_available_tags("", "https://discord.com/api/webhooks/1/a")

    def test_reads_tags_via_the_webhook_channel(self):
        session = self.Session([
            self.Resp(200, {"channel_id": "555"}),
            self.Resp(200, {"type": 15, "name": "leagues", "available_tags": [
                {"id": "1", "name": "Sand"}, {"id": "2", "name": "Indoor"}]}),
        ])
        tags = fetch_available_tags("token", "https://discord.com/api/webhooks/1/a",
                                    session=session)
        assert tags == {"Sand": "1", "Indoor": "2"}

    def test_non_forum_channel_is_explained(self):
        session = self.Session([
            self.Resp(200, {"channel_id": "555"}),
            self.Resp(200, {"type": 0, "name": "general"}),
        ])
        with pytest.raises(TagLookupError, match="not a forum channel"):
            fetch_available_tags("token", "https://discord.com/api/webhooks/1/a",
                                 session=session)

    def test_forbidden_is_explained(self):
        session = self.Session([
            self.Resp(200, {"channel_id": "555"}),
            self.Resp(403),
        ])
        with pytest.raises(TagLookupError, match="Invite it to the server"):
            fetch_available_tags("token", "https://discord.com/api/webhooks/1/a",
                                 session=session)
