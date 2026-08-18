"""iCalendar export tests.

Output is validated with the third-party `icalendar` parser as well as by
inspection, so the format is checked by something other than the code that
produced it.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from icalendar import Calendar

from indyvb.ics import (LOCAL_TZ, escape_text, fold_line, to_ics)
from indyvb.locations import Venue
from indyvb.models import Event

TODAY = date(2026, 8, 18)
STAMP = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def make_event(source_id="1", name="Test League", **kwargs) -> Event:
    defaults = dict(
        source="cca", source_name="CCA Sports", kind="league",
        source_id=source_id, name=name, url="https://example.com/l/1",
        start_date=TODAY + timedelta(days=7),
    )
    defaults.update(kwargs)
    return Event(**defaults)


def parse(text: str) -> Calendar:
    return Calendar.from_ical(text.encode("utf-8"))


def vevents(text: str) -> list:
    return list(parse(text).walk("VEVENT"))


class TestEscaping:
    @pytest.mark.parametrize("raw,expected", [
        ("a,b", "a\\,b"),
        ("a;b", "a\\;b"),
        ("a\\b", "a\\\\b"),
        ("a\nb", "a\\nb"),
        ("a\r\nb", "a\\nb"),
        ("plain", "plain"),
    ])
    def test_escape_text(self, raw, expected):
        assert escape_text(raw) == expected

    def test_escaping_round_trips_through_a_parser(self):
        event = make_event(name="Coed 6s, Level B; Rec")
        text = to_ics([event], now=STAMP)
        assert str(vevents(text)[0]["summary"]) == "Coed 6s, Level B; Rec"


class TestFolding:
    def test_short_line_untouched(self):
        assert fold_line("SHORT:value") == ["SHORT:value"]

    def test_long_line_is_split_within_limit(self):
        line = "DESCRIPTION:" + "x" * 300
        parts = fold_line(line)
        assert len(parts) > 1
        assert all(len(p.encode()) <= 75 for p in parts)
        assert "".join(parts) == line

    def test_multibyte_characters_are_not_split(self):
        """Folding is measured in octets but must not cut a character in half."""
        line = "SUMMARY:" + "\U0001f3d0" * 60  # 4-byte emoji
        parts = fold_line(line)
        for part in parts:
            part.encode("utf-8").decode("utf-8")  # would raise if split
        assert "".join(parts) == line

    def test_folded_output_is_reassembled_by_parser(self):
        event = make_event(name="A very long league name " * 6)
        text = to_ics([event], now=STAMP)
        assert str(vevents(text)[0]["summary"]) == event.name


class TestCalendarStructure:
    def test_has_required_headers(self):
        cal = parse(to_ics([make_event()], now=STAMP))
        assert cal.get("version") == "2.0"
        assert "IndyVB" in str(cal.get("prodid"))

    def test_calendar_name_is_configurable(self):
        cal = parse(to_ics([make_event()], calendar_name="Indy VB", now=STAMP))
        assert str(cal.get("x-wr-calname")) == "Indy VB"

    def test_crlf_line_endings(self):
        """RFC 5545 requires CRLF; some clients reject bare LF."""
        text = to_ics([make_event()], now=STAMP)
        assert "\r\n" in text
        assert not text.replace("\r\n", "").count("\n")

    def test_uids_are_stable_and_unique(self):
        events = [make_event("1"), make_event("2", registration_deadline=TODAY)]
        uids = [str(e["uid"]) for e in vevents(to_ics(events, now=STAMP))]
        assert len(set(uids)) == len(uids)
        assert "cca:league:1@indyvb" in uids

    def test_uid_is_stable_across_runs(self):
        """Re-publishing must update entries, not duplicate them."""
        first = vevents(to_ics([make_event()], now=STAMP))[0]["uid"]
        later = vevents(to_ics([make_event()], now=STAMP + timedelta(days=1)))[0]["uid"]
        assert str(first) == str(later)


class TestTiming:
    def test_all_day_when_no_time_known(self):
        event = vevents(to_ics([make_event(times=None)], now=STAMP))[0]
        assert not isinstance(event.decoded("dtstart"), datetime)
        assert event.decoded("dtstart") == TODAY + timedelta(days=7)

    def test_all_day_dtend_is_exclusive(self):
        event = vevents(to_ics([make_event()], now=STAMP))[0]
        span = event.decoded("dtend") - event.decoded("dtstart")
        assert span == timedelta(days=1)

    def test_timed_event_uses_local_time(self):
        event = make_event(start_date=date(2026, 8, 18), times="8:20 PM - 9:40 PM")
        parsed = vevents(to_ics([event], now=STAMP))[0]
        start = parsed.decoded("dtstart")
        assert start == datetime(2026, 8, 18, 20, 20, tzinfo=LOCAL_TZ)
        assert parsed.decoded("dtend") == datetime(2026, 8, 18, 21, 40, tzinfo=LOCAL_TZ)

    def test_daylight_saving_is_handled(self):
        """The same clock time maps to a different UTC offset by season."""
        summer = make_event(start_date=date(2026, 7, 1), times="8:00 PM - 9:00 PM")
        winter = make_event(start_date=date(2026, 1, 7), times="8:00 PM - 9:00 PM")
        summer_utc = vevents(to_ics([summer], now=STAMP))[0].decoded("dtstart")
        winter_utc = vevents(to_ics([winter], now=STAMP))[0].decoded("dtstart")
        assert summer_utc.astimezone(timezone.utc).hour == 0   # EDT, UTC-4
        assert winter_utc.astimezone(timezone.utc).hour == 1   # EST, UTC-5

    def test_default_duration_when_end_time_missing(self):
        event = make_event(times="7:45 PM")
        parsed = vevents(to_ics([event], now=STAMP))[0]
        assert parsed.decoded("dtend") - parsed.decoded("dtstart") == timedelta(hours=2)

    def test_session_crossing_midnight(self):
        """An end time before the start means play runs past midnight."""
        event = make_event(times="11:00 PM - 12:30 AM")
        parsed = vevents(to_ics([event], now=STAMP))[0]
        assert parsed.decoded("dtend") > parsed.decoded("dtstart")

    def test_multi_day_tournament_spans_both_days(self):
        event = make_event(kind="tournament", start_date=date(2026, 9, 12),
                           end_date=date(2026, 9, 13))
        parsed = vevents(to_ics([event], now=STAMP))[0]
        assert parsed.decoded("dtend") == date(2026, 9, 14)  # exclusive


class TestDeadlines:
    def test_deadline_creates_a_second_entry(self):
        event = make_event(registration_deadline=TODAY + timedelta(days=3))
        events = vevents(to_ics([event], now=STAMP))
        assert len(events) == 2
        deadline = next(e for e in events if "deadline" in str(e["uid"]))
        assert "Signup deadline" in str(deadline["summary"])
        assert deadline.decoded("dtstart") == TODAY + timedelta(days=3)

    def test_deadlines_can_be_disabled(self):
        event = make_event(registration_deadline=TODAY + timedelta(days=3))
        assert len(vevents(to_ics([event], include_deadlines=False, now=STAMP))) == 1

    def test_deadline_entry_is_transparent(self):
        """A deadline should not make the day look busy."""
        event = make_event(registration_deadline=TODAY)
        deadline = next(e for e in vevents(to_ics([event], now=STAMP))
                        if "deadline" in str(e["uid"]))
        assert str(deadline["transp"]) == "TRANSPARENT"

    def test_deadline_only_event_still_appears(self):
        event = make_event(start_date=None, registration_deadline=TODAY)
        assert len(vevents(to_ics([event], now=STAMP))) == 1


class TestContent:
    def test_location_uses_full_address(self):
        event = make_event(venues=[
            Venue(id="1", name="First Baptist Fieldhouse",
                  address="8600 N College Ave, Indianapolis, IN 46240")])
        parsed = vevents(to_ics([event], now=STAMP))[0]
        assert "8600 N College Ave" in str(parsed["location"])

    def test_geo_from_coordinates(self):
        event = make_event(venues=[Venue(id="1", name="iBeach31",
                                         latitude=40.05, longitude=-86.14)])
        parsed = vevents(to_ics([event], now=STAMP))[0]
        assert float(parsed["geo"].latitude) == pytest.approx(40.05)
        assert float(parsed["geo"].longitude) == pytest.approx(-86.14)

    def test_categories_parse_as_a_list(self):
        """Commas separate categories and must not be escaped away."""
        event = make_event(kind="tournament", play_format="4s")
        parsed = vevents(to_ics([event], now=STAMP))[0]
        cats = [str(c) for c in parsed["categories"].cats]
        assert "tournament" in cats and "4s" in cats

    def test_description_carries_signup_link_and_price(self):
        event = make_event(price_individual="$78.00", status="Open")
        body = str(vevents(to_ics([event], now=STAMP))[0]["description"])
        assert "$78.00" in body
        assert "https://example.com/l/1" in body

    def test_sold_out_event_is_transparent(self):
        busy = vevents(to_ics([make_event(status="Open")], now=STAMP))[0]
        free = vevents(to_ics([make_event(status="Sold Out")], now=STAMP))[0]
        assert str(busy["transp"]) == "OPAQUE"
        assert str(free["transp"]) == "TRANSPARENT"


class TestEdgeCases:
    def test_undated_event_is_skipped(self):
        event = make_event(start_date=None, registration_deadline=None)
        assert vevents(to_ics([event], now=STAMP)) == []

    def test_empty_calendar_is_still_valid(self):
        cal = parse(to_ics([], now=STAMP))
        assert cal.get("version") == "2.0"
        assert list(cal.walk("VEVENT")) == []

    def test_events_are_sorted_by_date(self):
        events = [
            make_event("late", start_date=date(2026, 12, 1)),
            make_event("early", start_date=date(2026, 9, 1)),
        ]
        uids = [str(e["uid"]) for e in vevents(to_ics(events, now=STAMP))]
        assert uids[0].startswith("cca:league:early")
