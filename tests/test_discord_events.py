"""Discord scheduled-event sync tests, with the REST API stubbed out."""
from datetime import date, datetime, timedelta, timezone

import pytest

from indyvb.discord_events import (MAX_DESCRIPTION, MAX_LOCATION, MAX_NAME,
                                   DiscordEventError, ScheduledEventSync,
                                   build_payload, event_window, marker_for,
                                   uid_from_description)
from indyvb.ics import LOCAL_TZ
from indyvb.locations import Venue
from indyvb.models import Event

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 8, 18)


def make_event(source_id="1", name="Test League", **kwargs) -> Event:
    defaults = dict(
        source="cca", source_name="CCA Sports", kind="league",
        source_id=source_id, name=name, url="https://example.com/l/1",
        start_date=TODAY + timedelta(days=7),
    )
    defaults.update(kwargs)
    return Event(**defaults)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    """Records requests and replays queued responses per HTTP method."""

    def __init__(self, existing=None, responses=None):
        self.headers = {}
        self.existing = existing if existing is not None else []
        self.responses = responses or {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        if method in self.responses:
            queued = self.responses[method]
            return queued.pop(0) if isinstance(queued, list) else queued
        if method == "GET":
            return FakeResponse(200, self.existing)
        return FakeResponse(200, {"id": "999"})


def make_sync(existing=None, responses=None):
    session = FakeSession(existing, responses)
    sync = ScheduledEventSync("token", "guild123", session=session)
    return sync, session


class TestConfiguration:
    def test_missing_token_is_reported_clearly(self):
        with pytest.raises(DiscordEventError, match="No bot token"):
            ScheduledEventSync("", "guild")

    def test_missing_guild_is_reported_clearly(self):
        with pytest.raises(DiscordEventError, match="No guild id"):
            ScheduledEventSync("token", "")

    def test_sets_bot_authorization_header(self):
        sync, session = make_sync()
        assert session.headers["Authorization"] == "Bot token"


class TestMarker:
    def test_marker_round_trips(self):
        event = make_event()
        assert uid_from_description(f"stuff\n{marker_for(event)}") == event.uid

    def test_no_marker_returns_none(self):
        assert uid_from_description("just a description") is None
        assert uid_from_description(None) is None

    def test_marker_survives_description_truncation(self):
        """Losing the marker would make every sync create duplicates."""
        event = make_event(description="x" * 5000,
                           divisions=[f"Division {i}" for i in range(200)],
                           url="https://example.com/" + "y" * 900)
        payload = build_payload(event)
        assert len(payload["description"]) <= MAX_DESCRIPTION
        assert uid_from_description(payload["description"]) == event.uid


class TestEventWindow:
    def test_uses_listed_times(self):
        event = make_event(start_date=date(2026, 9, 1), times="8:20 PM - 9:40 PM")
        start, end = event_window(event)
        assert start == datetime(2026, 9, 1, 20, 20, tzinfo=LOCAL_TZ)
        assert end == datetime(2026, 9, 1, 21, 40, tzinfo=LOCAL_TZ)

    def test_defaults_to_evening_when_no_time(self):
        """Midnight would display as the previous day in many clients."""
        start, _ = event_window(make_event(start_date=date(2026, 9, 1)))
        assert start.hour == 18

    def test_default_duration_without_end_time(self):
        start, end = event_window(make_event(times="7:45 PM"))
        assert end - start == timedelta(hours=2)

    def test_overnight_session(self):
        start, end = event_window(make_event(times="11:00 PM - 12:30 AM"))
        assert end > start

    def test_multi_day_tournament(self):
        event = make_event(start_date=date(2026, 9, 12), end_date=date(2026, 9, 13))
        start, end = event_window(event)
        assert end.date() == date(2026, 9, 13)

    def test_no_date_returns_none(self):
        assert event_window(make_event(start_date=None)) is None


class TestPayload:
    def test_required_fields(self):
        payload = build_payload(make_event())
        assert payload["entity_type"] == 3          # EXTERNAL
        assert payload["privacy_level"] == 2        # GUILD_ONLY
        assert payload["scheduled_start_time"] < payload["scheduled_end_time"]
        assert payload["entity_metadata"]["location"]

    def test_respects_discord_length_limits(self):
        event = make_event(
            name="L" * 300,
            venues=[Venue(id="1", name="V" * 200, address="A" * 200)],
        )
        payload = build_payload(event)
        assert len(payload["name"]) <= MAX_NAME
        assert len(payload["description"]) <= MAX_DESCRIPTION
        assert len(payload["entity_metadata"]["location"]) <= MAX_LOCATION

    def test_location_prefers_venue_address(self):
        event = make_event(venues=[Venue(id="1", name="FBA", address="8600 N College")])
        assert build_payload(event)["entity_metadata"]["location"] == \
            "FBA - 8600 N College"

    def test_location_falls_back_when_address_too_long(self):
        event = make_event(location="Short Name",
                           venues=[Venue(id="1", name="N" * 90, address="A" * 90)])
        assert build_payload(event)["entity_metadata"]["location"] == "Short Name"

    def test_description_includes_signup_url(self):
        assert "https://example.com/l/1" in build_payload(make_event())["description"]

    def test_undated_event_rejected(self):
        with pytest.raises(ValueError):
            build_payload(make_event(start_date=None))


class TestSync:
    def test_creates_new_events(self):
        sync, session = make_sync(existing=[])
        result = sync.sync([make_event("1"), make_event("2")], now=NOW)
        assert len(result.created) == 2
        assert sum(1 for c in session.calls if c["method"] == "POST") == 2

    def test_skips_events_already_present_and_unchanged(self):
        event = make_event()
        payload = build_payload(event)
        existing = [{"id": "55", **payload}]
        sync, session = make_sync(existing=existing)
        result = sync.sync([event], now=NOW)
        assert result.unchanged == [event.name]
        assert not any(c["method"] in ("POST", "PATCH") for c in session.calls)

    def test_updates_changed_events(self):
        event = make_event(price_individual="$78.00")
        existing = [{"id": "55", **build_payload(event)}]
        changed = make_event(price_individual="$85.00")

        sync, session = make_sync(existing=existing)
        result = sync.sync([changed], now=NOW)
        assert result.updated == [changed.name]
        patches = [c for c in session.calls if c["method"] == "PATCH"]
        assert len(patches) == 1
        assert patches[0]["url"].endswith("/55")

    def test_skips_events_that_already_started(self):
        """Discord rejects a start time in the past."""
        past = make_event(start_date=TODAY - timedelta(days=1))
        sync, _ = make_sync(existing=[])
        result = sync.sync([past], now=NOW)
        assert result.created == []
        assert result.skipped[0][1] == "already started"

    def test_skips_undated_events(self):
        sync, _ = make_sync(existing=[])
        result = sync.sync([make_event(start_date=None)], now=NOW)
        assert result.skipped[0][1] == "no date"

    def test_respects_the_hundred_event_cap(self):
        existing = [{"id": str(i), "description": f"[indyvb:other:{i}]"}
                    for i in range(100)]
        sync, session = make_sync(existing=existing)
        result = sync.sync([make_event("new")], now=NOW)
        assert result.created == []
        assert "cap" in result.skipped[0][1]
        assert not any(c["method"] == "POST" for c in session.calls)

    def test_dry_run_changes_nothing(self):
        sync, session = make_sync(existing=[])
        result = sync.sync([make_event()], dry_run=True, now=NOW)
        assert len(result.created) == 1
        assert not any(c["method"] == "POST" for c in session.calls)


class TestErrorHandling:
    @pytest.mark.parametrize("status,message", [
        (401, "token"),
        (403, "Manage Events"),
        (404, "Guild not found"),
    ])
    def test_api_errors_are_explained(self, status, message):
        sync, _ = make_sync(responses={"GET": FakeResponse(status, text="nope")})
        with pytest.raises(DiscordEventError, match=message):
            sync.sync([make_event()], now=NOW)

    def test_create_failure_is_reported(self):
        sync, _ = make_sync(
            existing=[],
            responses={"GET": FakeResponse(200, []),
                       "POST": FakeResponse(400, text="bad payload")},
        )
        with pytest.raises(DiscordEventError, match="Could not create"):
            sync.sync([make_event()], now=NOW)

    def test_rate_limit_is_retried(self, monkeypatch):
        monkeypatch.setattr("indyvb.discord_events.time.sleep", lambda *_: None)
        sync, session = make_sync(
            existing=[],
            responses={"GET": [FakeResponse(429, {"retry_after": 0.01}),
                               FakeResponse(200, [])]},
        )
        result = sync.sync([make_event()], dry_run=True, now=NOW)
        assert len(result.created) == 1
