"""Turn desktop notifications (org.freedesktop.Notifications.Notify) into events.

Same D-Bus source as ~/.local/bin/notify-to-phone. One-time codes, noise, and
repeats (see noise.py) are dropped before they leave the machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from collections.abc import Iterator

from . import noise

MATCH = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
SOURCE = "desktop-notification"

# A digit run of 4-8 next to words like "code" or "verification" is almost always a login code.
_OTP_WORDS = re.compile(r"\b(code|otp|passcode|verification|verify|2fa|one[- ]time|security)\b", re.I)
_OTP_DIGITS = re.compile(r"\b\d{4,8}\b|\b\d{3}[- ]\d{3}\b")

# Extra apps to skip, comma separated, matched case-insensitively against the app name.
SKIP_APPS = {a.strip().lower() for a in os.environ.get("RECALL_NOTIFY_SKIP_APPS", "").split(",") if a.strip()}

_comm_cache: dict[str, str] = {}


def _sender_comm(name: str) -> str:
    if name not in _comm_cache:
        try:
            out = subprocess.run(
                ["busctl", "--user", "status", name], capture_output=True, text=True, timeout=5
            ).stdout
            pid = next(line.split("=", 1)[1] for line in out.splitlines() if line.startswith("PID="))
            with open(f"/proc/{pid}/comm") as f:
                _comm_cache[name] = f.read().strip()
        except Exception:
            _comm_cache[name] = ""
    return _comm_cache[name]


def is_one_time_code(text: str) -> bool:
    return bool(_OTP_WORDS.search(text) and _OTP_DIGITS.search(text))


def to_event(app: str, summary: str, body: str, urgency: int, comm: str, now_ms: int) -> dict:
    text = f"{summary}\n{body}".strip() if body else summary
    digest = hashlib.sha256(f"{app}\0{summary}\0{body}\0{now_ms}".encode()).hexdigest()[:32]
    return {
        "event_id": digest,
        "source": SOURCE,
        "kind": app or None,
        "actor": comm or None,
        "occurred_at": now_ms,
        "body": text or None,
        "meta": json.dumps({
            "app": app, "summary": summary, "urgency": urgency, "comm": comm,
            "content_key": noise.content_key(app, text),
        }),
    }


class _Limiter:
    """At most `limit` events per `window` seconds, so a notification loop can't flood the topic."""

    def __init__(self, limit: int = 60, window: float = 60):
        self.limit, self.window, self.sent = limit, window, deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self.sent and now - self.sent[0] > self.window:
            self.sent.popleft()
        if len(self.sent) >= self.limit:
            return False
        self.sent.append(now)
        return True


def watch() -> Iterator[dict]:
    """Yield an event for each desktop notification, forever."""
    limiter = _Limiter()
    policy = noise.load()
    recent = noise.RecentKeys(policy.repeat_window)
    proc = subprocess.Popen(
        ["busctl", "--user", "monitor", "--json=short", "--match", MATCH],
        stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        try:
            msg = json.loads(line)
            if msg.get("member") != "Notify":
                continue
            app, _, _, summary, body, _, hints, _ = msg["payload"]["data"]
            if app.lower() in SKIP_APPS or not (summary or body):
                continue
            if is_one_time_code(f"{summary} {body}"):
                print(f"skipped a one-time code from {app or 'unknown app'}", file=sys.stderr, flush=True)
                continue
            if policy.drop_reason(app, summary, body):
                continue
            text = f"{summary}\n{body}".strip() if body else summary
            if recent.seen_recently(noise.content_key(app, text), time.time()):
                continue
            if not limiter.allow():
                print("rate limit hit, dropping a notification", file=sys.stderr, flush=True)
                continue
            urgency = hints.get("urgency", {}).get("data", 1)
            yield to_event(app, summary, body, urgency, _sender_comm(msg["sender"]), int(time.time() * 1000))
        except Exception as e:
            print(f"skipped a message: {e}", file=sys.stderr, flush=True)
    raise SystemExit(proc.wait() or 1)
