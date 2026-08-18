from datetime import date

import pytest

from indyvb.utils import (clean, detect_format, infer_year, label_value,
                          parse_date, parse_money)

TODAY = date(2026, 8, 18)


@pytest.mark.parametrize("text,expected", [
    ("2026-08-09", date(2026, 8, 9)),          # VolleyballLife API
    ("6/23/26", date(2026, 6, 23)),            # LeagueLab widget
    ("8/5/2026", date(2026, 8, 5)),
    ("Wednesday, August 19", date(2026, 8, 19)),   # CCA, no year
    ("August 19, 2026", date(2026, 8, 19)),
    ("Fri, Aug 21", date(2026, 8, 21)),
    ("Starts: Friday, December 4", date(2026, 12, 4)),
])
def test_parse_date_formats(text, expected):
    assert parse_date(text, TODAY) == expected


@pytest.mark.parametrize("text", ["", None, "garbage", "no date here"])
def test_parse_date_rejects_junk(text):
    assert parse_date(text, TODAY) is None


def test_undated_month_rolls_to_next_year():
    """A January date seen in August belongs to next season, not this one."""
    assert parse_date("January 5", TODAY) == date(2027, 1, 5)


def test_recent_past_date_keeps_current_year():
    """Something a few weeks old should not jump forward a year."""
    assert parse_date("July 4", TODAY) == date(2026, 7, 4)


def test_infer_year_handles_leap_day():
    # Feb 29 does not exist in 2027, so it must not raise.
    assert infer_year(2, 29, TODAY) in (2026, 2028)


@pytest.mark.parametrize("text,expected", [
    ("$438.00 Total", "$438.00"),
    ("Cost: $78.00", "$78.00"),
    ("$8.00 - $78.00", "$8.00 - $78.00"),
    ("$1,250.00", "$1,250.00"),
    ("free", None),
    (None, None),
])
def test_parse_money(text, expected):
    assert parse_money(text) == expected


def test_label_value_stops_at_next_label():
    meta = "Day: Wednesday Starts: Wednesday, August 19 Deadline: Thursday, August 20"
    assert label_value(meta, "Starts") == "Wednesday, August 19"
    assert label_value(meta, "Day") == "Wednesday"
    assert label_value(meta, "Deadline") == "Thursday, August 20"


def test_label_value_missing_label():
    assert label_value("Day: Monday", "Deadline") is None


@pytest.mark.parametrize("text,expected", [
    ("Coed 6's Volleyball League", "6s"),
    ("Bluefire Quads Classic", "4s"),
    ("Mens 2s and Womens 2s", "2s"),
    ("Coed 4v4 Level B", "4s"),
    ("6 v 6 Indoor Volleyball Tournament", "6s"),
    ("Open Play", None),
])
def test_detect_format(text, expected):
    assert detect_format(text) == expected


def test_clean_strips_decorative_separators():
    # CCA renders non-ASCII diamonds between metadata fields.
    assert clean("Day: Wednesday ⬥ Starts: Aug 19") == "Day: Wednesday Starts: Aug 19"
