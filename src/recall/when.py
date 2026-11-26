"""Time words in a search query: "yesterday", "last week", "3 days ago", "in October", "since 2026-11-20".

`what did I work on yesterday` is a question about a day, not about the words
"work" and "yesterday". `parse` finds one such phrase, turns it into a window
[start, end) in the user's time zone, and returns the query without it, so search
can filter on created_at and rank what is left by meaning.

Only phrases that clearly name a time are read; anything else stays in the
query. The time zone is RECALL_TZ, else the machine's own.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
DAYS = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}
UNITS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30, "year": 365, "years": 365}
NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10, "few": 3, "couple": 2, "couple of": 2}

_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_DAY = "|".join(DAYS)
_NUM = r"\d{1,3}|an?|one|two|three|four|five|six|seven|eight|nine|ten|few|couple(?: of)?"
_UNIT = r"days?|weeks?|months?|years?"
_ISO = r"\d{4}-\d{2}-\d{2}"

# Words that carry no topic once the time phrase is gone: "what did I do yesterday".
FILLER = set("""a an the i me my we our you he him his it its what which who when where how did do does done
doing was were is are be been have has had on in at of for to from about during with and or any anything
something stuff things thing happen happened happening going went work worked working up get got lately
recent recently tell show find list all there that this those these just so far earlier""".split())


@dataclass(frozen=True)
class Window:
    start: datetime | None      # inclusive, UTC
    end: datetime | None        # exclusive, UTC
    phrase: str                 # what was read, as written

    def describe(self, tz: tzinfo | None = None) -> str:
        tz = tz or zone()
        fmt = lambda d: d.astimezone(tz).strftime("%Y-%m-%d")  # noqa: E731
        if self.start and self.end:
            last = (self.end - timedelta(seconds=1))
            return fmt(self.start) if fmt(self.start) == fmt(last) else f"{fmt(self.start)} to {fmt(last)}"
        return f"since {fmt(self.start)}" if self.start else f"before {fmt(self.end)}"


def zone() -> tzinfo:
    name = os.environ.get("RECALL_TZ", "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _midnight(d: date, tz: tzinfo) -> datetime:
    return datetime.combine(d, time(), tz).astimezone(timezone.utc)


def _span(first: date, days: int, tz: tzinfo) -> tuple[datetime, datetime]:
    return _midnight(first, tz), _midnight(first + timedelta(days=days), tz)


def _month(year: int, month: int, tz: tzinfo) -> tuple[datetime, datetime]:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return _midnight(date(year, month, 1), tz), _midnight(nxt, tz)


def _year(year: int, tz: tzinfo) -> tuple[datetime, datetime]:
    return _midnight(date(year, 1, 1), tz), _midnight(date(year + 1, 1, 1), tz)


def _count(word: str) -> int:
    return int(word) if word.isdigit() else NUMBERS[word]


def _rules(today: date, tz: tzinfo):
    """(pattern, function of the match -> (start, end)). First match in the query wins."""
    monday = today - timedelta(days=today.weekday())

    def month_named(m: re.Match) -> tuple[datetime, datetime]:
        month = MONTHS[m["month"].lower()]
        year = int(m["year"]) if m["year"] else (today.year if month <= today.month else today.year - 1)
        return _month(year, month, tz)

    def weekday(m: re.Match) -> tuple[datetime, datetime]:
        back = (today.weekday() - DAYS[m["day"].lower()]) % 7 or 7     # the most recent one before today
        return _span(today - timedelta(days=back), 1, tz)

    def last_n(m: re.Match) -> tuple[datetime, datetime]:
        days = _count(m["n"].lower()) * UNITS[m["unit"].lower()]
        return _midnight(today - timedelta(days=days - 1), tz), _midnight(today + timedelta(days=1), tz)

    def iso_day(m: re.Match) -> tuple[datetime, datetime]:
        return _span(date.fromisoformat(m["d"]), 1, tz)

    def since(m: re.Match) -> tuple[datetime, None]:
        return _midnight(date.fromisoformat(m["d"]), tz), None

    def between(m: re.Match) -> tuple[datetime, datetime]:
        return _midnight(date.fromisoformat(m["a"]), tz), _midnight(date.fromisoformat(m["b"]) + timedelta(days=1), tz)

    def ago(m: re.Match) -> tuple[datetime, datetime]:
        """'3 days ago' is that day; 'two weeks ago', 'a month ago' and 'a year ago' are the
        calendar week, month or year that far back, since nobody means one day by them."""
        n, unit = _count(m["n"].lower()), m["unit"].lower().rstrip("s")
        if unit == "day":
            return _span(today - timedelta(days=n), 1, tz)
        if unit == "week":
            return _span(monday - timedelta(days=7 * n), 7, tz)
        if unit == "year":
            return _year(today.year - n, tz)
        months = today.year * 12 + today.month - 1 - n
        return _month(months // 12, months % 12 + 1, tz)

    def rolling(m: re.Match) -> tuple[datetime, datetime]:
        days = UNITS[m["unit"].lower()]
        return _midnight(today - timedelta(days=days - 1), tz), _midnight(today + timedelta(days=1), tz)

    def weekend(m: re.Match) -> tuple[datetime, datetime]:
        saturday = today - timedelta(days=(today.weekday() - 5) % 7)    # today, on a Saturday
        if (m["which"] or "").lower() == "last" and today.weekday() >= 5:   # said during a weekend: the one before
            saturday -= timedelta(days=7)
        return _span(saturday, 2, tz)

    last_month = (today.replace(day=1) - timedelta(days=1))
    return [
        (rf"\b(?:between|from)\s+(?P<a>{_ISO})\s+(?:and|to|until)\s+(?P<b>{_ISO})\b", between),
        (rf"\bsince\s+(?P<d>{_ISO})\b", since),
        (rf"\b(?:on\s+)?(?P<d>{_ISO})\b", iso_day),
        # Before "yesterday", which it contains.
        (r"\b(?:the\s+)?day before yesterday\b", lambda m: _span(today - timedelta(days=2), 1, tz)),
        (r"\btoday\b|\bthis morning\b|\bthis afternoon\b|\btonight\b", lambda m: _span(today, 1, tz)),
        (r"\byesterday\b|\blast night\b", lambda m: _span(today - timedelta(days=1), 1, tz)),
        (rf"\b(?P<n>{_NUM})\s+(?P<unit>{_UNIT})\s+ago\b", ago),
        (rf"\b(?:in\s+)?the\s+(?:last|past)\s+(?P<n>{_NUM})\s+(?P<unit>{_UNIT})\b", last_n),
        (rf"\b(?:last|past)\s+(?P<n>{_NUM})\s+(?P<unit>{_UNIT})\b", last_n),
        # "the past week" counts back from today; "last week" below is the calendar week.
        (r"\b(?:(?:in|over|during)\s+)?(?:the\s+)?past\s+(?P<unit>week|month|year)\b", rolling),
        (r"\bthis week(?:'s)?(?!\w)", lambda m: _span(monday, 7, tz)),
        (r"\blast week(?:'s)?(?!\w)", lambda m: _span(monday - timedelta(days=7), 7, tz)),
        (r"\bthis month(?:'s)?(?!\w)", lambda m: _month(today.year, today.month, tz)),
        (r"\blast month(?:'s)?(?!\w)", lambda m: _month(last_month.year, last_month.month, tz)),
        (r"\bthis year(?:'s)?(?!\w)", lambda m: _year(today.year, tz)),
        (r"\blast year(?:'s)?(?!\w)", lambda m: _year(today.year - 1, tz)),
        # "the weekend" alone is left to the query: it is as often a subject as a time.
        (r"\b(?:over|on|during)\s+the\s+weekend\b|\b(?P<which>this|last)\s+weekend\b", weekend),
        (rf"\b(?:on\s+|last\s+|this\s+)(?P<day>{_DAY})\b", weekday),
        (rf"\b(?:in|during)\s+(?P<month>{_MONTH})(?:\s+(?P<year>\d{{4}}))?\b", month_named),
        (rf"\b(?P<month>{_MONTH})\s+(?P<year>\d{{4}})\b", month_named),
        (r"\b(?:in|during)\s+(?P<y>(?:19|20)\d{2})\b", lambda m: _year(int(m["y"]), tz)),
    ]


def parse(query: str, now: datetime | None = None, tz: tzinfo | None = None) -> tuple[Window | None, str]:
    """(window, the query without its time phrase). (None, query) when no time is named."""
    tz = tz or zone()
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    for pattern, make in _rules(today, tz):
        m = re.search(pattern, query, re.IGNORECASE)
        if not m:
            continue
        try:
            start, end = make(m)
        except (ValueError, KeyError):      # 2026-13-40, an unknown number word
            continue
        rest = " ".join(f"{query[:m.start()]} {query[m.end():]}".split()).strip(" ,.?!")
        return Window(start, end, m.group(0)), rest
    return None, query


def topical(query: str) -> bool:
    """Whether a query still names a subject: 'what did I do' does not, 'niri crash' does."""
    return any(w not in FILLER for w in re.findall(r"[\w'-]+", query.lower()))
