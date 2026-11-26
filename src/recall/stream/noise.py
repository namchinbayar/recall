"""Decide which desktop notifications are worth keeping.

Two checks, used by `recall stream notify` (before anything leaves the machine),
by `recall stream sink` (for whatever reaches Kafka anyway), and by
`recall stream purge` (for what is already stored):

- Drop rules: regexes on the app name, the summary, and the body. Built-in
  defaults cover screenshots, saved logins, app-update notices, and test popups;
  ~/.config/recall/noise.toml (or $RECALL_NOISE) adds more.
- Repeats: the same app + text within REPEAT_WINDOW counts once. Tether replays
  the phone's notification history on every reconnect, which once made two
  thirds of all stored notifications exact copies.

noise.toml:

    [[drop]]
    app = "Tether"                # regexes, case-insensitive, all given ones must match
    summary = "^Credit Karma$"
    reason = "finance"            # optional, shown by `recall stream purge`

    repeat_window_days = 7        # optional, top level
"""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

CONFIG = Path(os.environ.get("RECALL_NOISE") or Path.home() / ".config" / "recall" / "noise.toml")
REPEAT_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class Rule:
    app: re.Pattern | None = None
    summary: re.Pattern | None = None
    body: re.Pattern | None = None
    reason: str = ""

    def matches(self, app: str, summary: str, body: str) -> bool:
        checks = [(self.app, app), (self.summary, summary), (self.body, body)]
        given = [(p, v) for p, v in checks if p is not None]
        return bool(given) and all(p.search(v or "") for p, v in given)


def _rule(app: str | None = None, summary: str | None = None, body: str | None = None, reason: str = "") -> Rule:
    comp = lambda s: re.compile(s, re.I) if s else None  # noqa: E731
    return Rule(comp(app), comp(summary), comp(body), reason)


DEFAULT_RULES: tuple[Rule, ...] = (
    _rule(summary=r"^screenshot (captured|saved|taken)", reason="screenshot"),
    _rule(app=r"chrome|chromium|firefox", summary=r"^(login|password) saved", reason="browser chrome"),
    _rule(summary=r"is now up to date$|^update(s)? (installed|available)$", reason="app update"),
    _rule(app=r"^(notify-send|popup test|claudetest)$", reason="test"),
    _rule(app=r"^1password-secret-service$", reason="keyring prompt"),
    _rule(summary=r"\b\d{1,2}% off\b|\bup to \d{1,2}% off\b", reason="promo"),
    _rule(body=r"\b\d{1,2}% off\b", reason="promo"),
)


@dataclass(frozen=True)
class Policy:
    rules: tuple[Rule, ...]
    repeat_window: timedelta = REPEAT_WINDOW

    def drop_reason(self, app: str, summary: str, body: str) -> str | None:
        """The reason to drop this notification, or None to keep it."""
        for rule in self.rules:
            if rule.matches(app, summary, body):
                return rule.reason or "rule"
        return None


def load(path: Path = CONFIG) -> Policy:
    if not path.is_file():
        return Policy(DEFAULT_RULES)
    data = tomllib.loads(path.read_text())
    extra = []
    for entry in data.get("drop", []):
        fields = {k: entry.get(k) for k in ("app", "summary", "body")}
        if not any(fields.values()):
            raise SystemExit(f"{path}: a [[drop]] entry needs app, summary, or body")
        extra.append(_rule(**fields, reason=entry.get("reason", "")))
    days = data.get("repeat_window_days")
    window = timedelta(days=float(days)) if days is not None else REPEAT_WINDOW
    return Policy(DEFAULT_RULES + tuple(extra), window)


def content_key(app: str, text: str) -> str:
    """Identity of a notification's content, independent of when it arrived."""
    norm = " ".join((text or "").split()).lower()
    return hashlib.sha256(f"{(app or '').lower()}\0{norm}".encode()).hexdigest()[:32]


class RecentKeys:
    """Content keys seen within the repeat window, for dropping repeats in a running process."""

    def __init__(self, window: timedelta = REPEAT_WINDOW):
        self.window_s = window.total_seconds()
        self.seen: dict[str, float] = {}

    def seen_recently(self, key: str, now_s: float) -> bool:
        """True if key was seen within the window; records it either way."""
        if len(self.seen) > 50_000:
            self.seen = {k: t for k, t in self.seen.items() if now_s - t <= self.window_s}
        last = self.seen.get(key)
        self.seen[key] = now_s
        return last is not None and now_s - last <= self.window_s
