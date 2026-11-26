"""Hermes gateway hook: spool each finished exchange for recall.

Holds no secrets and makes no network calls. It appends one JSON line to
~/.local/state/recall/spool.jsonl (or $RECALL_SPOOL); the recall-spool service
produces it to Kafka. Hermes truncates `message` and `response` to 500 chars.
Install: copy this directory to ~/.hermes/hooks/ and run `hermes gateway restart`.
"""

import fcntl
import hashlib
import json
import re
import os
import time
from pathlib import Path

STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
SPOOL = Path(os.environ.get("RECALL_SPOOL") or STATE / "recall" / "spool.jsonl")


# Discord prefixes the message with a note for the model; it is not what was said.
_PLATFORM_NOTE = re.compile(r"^\[Triggering message id:[^\]]*\]\s*")


def handle(event_type: str, context: dict):
    message = _PLATFORM_NOTE.sub("", (context.get("message") or "").strip())
    response = (context.get("response") or "").strip()
    if not (message or response):
        return
    now_ms = int(time.time() * 1000)
    session_id = context.get("session_id") or ""
    digest = hashlib.sha256(f"hermes\0{session_id}\0{message}\0{now_ms}".encode()).hexdigest()[:32]
    event = {
        "event_id": digest,
        "source": "hermes",
        "kind": context.get("platform") or None,
        "actor": context.get("user_id") or None,
        "occurred_at": now_ms,
        "body": f"Me: {message}\nHermes: {response}",
        "meta": {
            "session_id": session_id,
            "chat_id": context.get("chat_id"),
            "chat_type": context.get("chat_type"),
            "platform": context.get("platform"),
        },
    }
    SPOOL.parent.mkdir(parents=True, exist_ok=True)
    with open(SPOOL, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(event) + "\n")
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)
