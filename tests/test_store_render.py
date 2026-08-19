from datetime import date, timedelta

import pytest

from indyvb.locations import Venue
from indyvb.models import Event
from indyvb.render import (MAX_CHARS_PER_MESSAGE, MAX_EMBEDS_PER_MESSAGE,
                           chunk_embeds, digest_embeds, event_embed, truncate)
from indyvb.store import SeenStore

TODAY = date(2026, 8, 18)


def make_event(source_id="1", name="Test League", **kwargs) -> Event:
    defaults = dict(
        source="cca", source_name="CCA Sports", kind="league",
        source_id=source_id, name=name, url="https://example.com/l/1",
        start_date=TODAY + timedelta(days=7),
    )
    defaults.update(kwargs)
    return Event(**defaults)


class TestSeenStore:
    def test_everything_is_new_on_first_run(self, tmp_path):
        store = SeenStore(tmp_path / "seen.json")
        diff = store.diff([make_event("1"), make_event("2")])
        assert len(diff.new) == 2
        assert not diff.updated

    def test_recorded_events_are_unchanged_next_run(self, tmp_path):
        path = tmp_path / "seen.json"
        events = [make_event("1"), make_event("2")]
        store = SeenStore(path)
        store.record(events)
        store.save()

        diff = SeenStore(path).diff(events)
        assert not diff.new and not diff.updated
        assert len(diff.unchanged) == 2

    def test_changed_price_counts_as_updated(self, tmp_path):
        path = tmp_path / "seen.json"
        store = SeenStore(path)
        store.record([make_event("1", price_individual="$78.00")])
        store.save()

        diff = SeenStore(path).diff([make_event("1", price_individual="$85.00")])
        assert len(diff.updated) == 1
        assert not diff.new

    def test_cosmetic_description_change_is_ignored(self, tmp_path):
        """Upstream copy edits must not re-announce a listing."""
        path = tmp_path / "seen.json"
        store = SeenStore(path)
        store.record([make_event("1", description="Old blurb")])
        store.save()

        diff = SeenStore(path).diff([make_event("1", description="New blurb")])
        assert len(diff.unchanged) == 1
        assert not diff.updated

    def test_status_change_is_announced(self, tmp_path):
        path = tmp_path / "seen.json"
        store = SeenStore(path)
        store.record([make_event("1", status="Closed")])
        store.save()

        diff = SeenStore(path).diff([make_event("1", status="Open")])
        assert len(diff.updated) == 1

    def test_corrupt_state_file_does_not_crash(self, tmp_path):
        path = tmp_path / "seen.json"
        path.write_text("{ not valid json", encoding="utf-8")
        diff = SeenStore(path).diff([make_event("1")])
        assert len(diff.new) == 1

    def test_prune_drops_only_long_past_events(self, tmp_path):
        path = tmp_path / "seen.json"
        store = SeenStore(path)
        store.record([
            make_event("old", start_date=TODAY - timedelta(days=200)),
            make_event("recent", start_date=TODAY - timedelta(days=10)),
            make_event("future", start_date=TODAY + timedelta(days=30)),
        ])
        assert store.prune(TODAY) == 1
        assert len(store) == 2

    def test_save_is_atomic_and_reloadable(self, tmp_path):
        path = tmp_path / "nested" / "seen.json"
        store = SeenStore(path)
        store.record([make_event("1")])
        store.save()
        assert path.exists()
        assert len(SeenStore(path)) == 1


class TestRender:
    def test_embed_has_required_discord_fields(self):
        embed = event_embed(make_event(), TODAY)
        assert embed["title"] and embed["description"]
        assert isinstance(embed["color"], int)
        assert embed["url"].startswith("http")

    def test_embed_shows_split_availability(self):
        embed = event_embed(
            make_event(status_team="Sold Out", status_individual="Open"), TODAY)
        assert "Teams: Sold Out" in embed["description"]
        assert "Individual: Open" in embed["description"]

    def test_embed_shows_countdown(self):
        embed = event_embed(make_event(start_date=TODAY + timedelta(days=1)), TODAY)
        assert "tomorrow" in embed["description"]

    def test_embed_handles_missing_date(self):
        embed = event_embed(make_event(start_date=None), TODAY)
        assert "Date TBD" in embed["description"]

    def test_chunks_respect_embed_count_limit(self):
        embeds = [event_embed(make_event(str(i)), TODAY) for i in range(25)]
        batches = chunk_embeds(embeds)
        assert all(len(b) <= MAX_EMBEDS_PER_MESSAGE for b in batches)
        assert sum(len(b) for b in batches) == 25

    def test_chunks_respect_character_limit(self):
        """A few huge embeds must split by size, not just by count."""
        big = [{"title": "t" * 200, "description": "d" * 2000} for _ in range(6)]
        batches = chunk_embeds(big)
        for batch in batches:
            total = sum(len(e["title"]) + len(e["description"]) for e in batch)
            assert total <= MAX_CHARS_PER_MESSAGE

    def test_venue_renders_as_a_map_link(self):
        event = make_event(venues=[
            Venue(id="1", name="First Baptist Fieldhouse",
                  address="8600 N College Ave, Indianapolis, IN 46240"),
        ])
        description = event_embed(event, TODAY)["description"]
        assert "First Baptist Fieldhouse - 8600 N College Ave" in description
        assert "https://www.google.com/maps/" in description

    def test_all_venues_listed_for_multi_venue_league(self):
        event = make_event(venues=[
            Venue(id="1", name="Sense Charter School", address="1601 Barth Ave"),
            Venue(id="2", name="Salvation Army", address="1337 Shelby St"),
        ])
        description = event_embed(event, TODAY)["description"]
        assert "Sense Charter School" in description
        assert "Salvation Army" in description

    def test_many_venues_are_summarized(self):
        venues = [Venue(id=str(i), name=f"Gym {i}") for i in range(6)]
        description = event_embed(make_event(venues=venues), TODAY)["description"]
        assert "+3 more location(s)" in description

    def test_falls_back_to_plain_location(self):
        """Sources without resolvable venues must still show something."""
        event = make_event(venues=[], location="Some Gym")
        assert "Some Gym" in event_embed(event, TODAY)["description"]

    def test_truncate_adds_ellipsis(self):
        assert truncate("abcdef", 4) == "abc…"
        assert truncate("abc", 10) == "abc"

    def test_digest_groups_by_source(self):
        events = [
            make_event("1", source="cca", source_name="CCA Sports"),
            make_event("2", source="cca", source_name="CCA Sports"),
            make_event("3", source="ibeach-leagues", source_name="iBeach"),
        ]
        embeds = digest_embeds(events, TODAY)
        assert len(embeds) == 2
        assert "(2)" in embeds[0]["title"]

    def test_digest_stays_within_description_limit(self):
        events = [make_event(str(i), name="Long league name " * 6) for i in range(60)]
        for embed in digest_embeds(events, TODAY):
            assert len(embed["description"]) <= 4096


class TestEventModel:
    def test_upcoming_excludes_finished_events(self):
        assert not make_event(start_date=TODAY - timedelta(days=1)).is_upcoming(TODAY)
        assert make_event(start_date=TODAY).is_upcoming(TODAY)

    def test_undated_event_counts_as_upcoming(self):
        """Dropping undated listings would hide real events."""
        assert make_event(start_date=None).is_upcoming(TODAY)

    def test_multi_day_event_upcoming_until_end(self):
        event = make_event(start_date=TODAY - timedelta(days=2),
                           end_date=TODAY + timedelta(days=1))
        assert event.is_upcoming(TODAY)

    def test_sort_key_tolerates_missing_dates(self):
        events = [make_event("1", start_date=None), make_event("2")]
        assert sorted(events, key=lambda e: e.sort_key())[0].source_id == "2"

    def test_round_trip_through_dict(self):
        event = make_event(days=["Monday"], divisions=["Rec"])
        assert Event.from_dict(event.to_dict()).to_dict() == event.to_dict()

    def test_round_trip_preserves_venues(self):
        event = make_event(venues=[Venue(id="1", name="Gym", address="1 Main St",
                                         latitude=39.5, longitude=-86.1)])
        restored = Event.from_dict(event.to_dict())
        assert restored.venues == event.venues
        assert restored.map_url == event.map_url

    def test_to_dict_exports_map_urls(self):
        event = make_event(venues=[Venue(id="1", name="Gym", address="1 Main St")])
        d = event.to_dict()
        assert d["map_url"].startswith("https://www.google.com/maps/")
        assert d["venues"][0]["map_url"] == d["map_url"]


class TestKindEmoji:
    def test_every_kind_has_its_own_emoji(self):
        """A missing entry silently falls back, so assert each is distinct."""
        from indyvb.models import Kind
        from typing import get_args
        from indyvb.render import KIND_EMOJI
        kinds = set(get_args(Kind))
        assert kinds <= set(KIND_EMOJI), f"no emoji for {kinds - set(KIND_EMOJI)}"
        assert len(set(KIND_EMOJI[k] for k in kinds)) == len(kinds)

    def test_one_off_event_uses_the_calendar_emoji(self):
        embed = event_embed(make_event(kind="event"), TODAY)
        assert embed["title"].startswith("\U0001f4c5")
