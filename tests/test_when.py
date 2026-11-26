from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from recall import when

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 24, 20, tzinfo=timezone.utc)   # Tuesday 2026-11-24, 12:00 in Los Angeles


@pytest.mark.parametrize("query, span, rest", [
    ("what did I work on yesterday", "2026-11-23", "what did I work on"),
    ("niri crash last week", "2026-11-16 to 2026-11-22", "niri crash"),
    ("descope in October", "2026-10-01 to 2026-10-31", "descope"),
    ("since 2026-11-20 recall", "since 2026-11-20", "recall"),
    ("what happened on Monday", "2026-11-23", "what happened"),
    ("hermes past 3 days", "2026-11-22 to 2026-11-24", "hermes"),
    ("tether between 2026-11-01 and 2026-11-05", "2026-11-01 to 2026-11-05", "tether"),
    ("in the last two weeks", "2026-11-11 to 2026-11-24", ""),
    ("notes from December", None, "notes from December"),        # "from" alone does not name a month
    ("in december", "2025-12-01 to 2025-12-31", ""),            # a month still ahead means last year's
])
def test_time_phrases_become_windows(query, span, rest):
    window, left = when.parse(query, NOW, LA)
    assert (window.describe(LA) if window else None) == span
    assert left == rest


def test_plain_queries_are_left_alone():
    for q in ["terminal", "may I ask about the vault", "2026 taxes", "the march of time"]:
        assert when.parse(q, NOW, LA) == (None, q)


def test_window_edges_are_local_midnights_in_utc():
    w, _ = when.parse("yesterday", NOW, LA)
    assert w.start == datetime(2026, 11, 23, 8, tzinfo=timezone.utc)
    assert w.end == datetime(2026, 11, 24, 8, tzinfo=timezone.utc)


def test_a_query_with_only_filler_has_no_subject():
    assert not when.topical("what did I do")
    assert not when.topical("")
    assert when.topical("niri crash")


@pytest.mark.parametrize("query, span, rest", [
    ("what did I do the day before yesterday", "2026-11-22", "what did I do"),     # not read as "yesterday"
    ("niri crash 3 days ago", "2026-11-21", "niri crash"),
    ("a day ago", "2026-11-23", ""),
    ("what happened a week ago", "2026-11-16 to 2026-11-22", "what happened"),     # the calendar week
    ("recall two weeks ago", "2026-11-09 to 2026-11-15", "recall"),
    ("a couple of months ago", "2026-09-01 to 2026-09-30", "a"),
    ("a year ago", "2025-01-01 to 2025-12-31", ""),
    ("notes from the past week", "2026-11-18 to 2026-11-24", "notes from"),        # counted back from today
    ("over the past month", "2026-10-26 to 2026-11-24", ""),
    ("decisions in the last 2 years", "2024-11-25 to 2026-11-24", "decisions"),
    ("what did I save last year", "2025-01-01 to 2025-12-31", "what did I save"),
    ("this year's decisions", "2026-01-01 to 2026-12-31", "decisions"),
    ("last week's sync bug", "2026-11-16 to 2026-11-22", "sync bug"),
    ("what broke in 2025", "2025-01-01 to 2025-12-31", "what broke"),
    ("over the weekend", "2026-11-21 to 2026-11-22", ""),
    ("vault work last weekend", "2026-11-21 to 2026-11-22", "vault work"),
    ("what did we decide this monday", "2026-11-23", "what did we decide"),
])
def test_more_time_phrases(query, span, rest):
    window, left = when.parse(query, NOW, LA)
    assert (window.describe(LA) if window else None) == span
    assert left == rest


def test_words_that_only_look_like_a_time_are_left_alone():
    for q in ["black friday deals", "what did we decide monday", "the weekend warrior article", "an hour ago",
              "week ago", "2026 taxes", "thisweek"]:
        assert when.parse(q, NOW, LA) == (None, q)


def test_ago_counts_months_across_a_year_and_weekends_from_inside_one():
    january = datetime(2026, 1, 15, 20, tzinfo=timezone.utc)
    assert when.parse("2 months ago", january, LA)[0].describe(LA) == "2025-11-01 to 2025-11-30"
    assert when.parse("12 months ago", january, LA)[0].describe(LA) == "2025-01-01 to 2025-01-31"
    saturday = datetime(2026, 11, 28, 20, tzinfo=timezone.utc)
    assert when.parse("over the weekend", saturday, LA)[0].describe(LA) == "2026-11-28 to 2026-11-29"
    assert when.parse("last weekend", saturday, LA)[0].describe(LA) == "2026-11-21 to 2026-11-22"


def test_earlier_today_has_no_subject_left():
    window, rest = when.parse("earlier today", NOW, LA)
    assert window.describe(LA) == "2026-11-24" and not when.topical(rest)
