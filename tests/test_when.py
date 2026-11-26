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
