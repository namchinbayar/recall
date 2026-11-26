"""Produce events that local programs append to a spool file.

Producers that should not hold Kafka credentials (the Hermes gateway hook in
contrib/hermes-hook/) append one JSON event per line to SPOOL. `recall stream
spool` tails that file, drops one-time codes, and produces each event. The byte
offset of the last delivered line is kept next to the file, so a restart resends
nothing that Kafka already acknowledged. When the whole file has been delivered
and it has grown past ROTATE_BYTES, it is truncated.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .notify import is_one_time_code

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "recall"
SPOOL = Path(os.environ.get("RECALL_SPOOL") or STATE_DIR / "spool.jsonl")
ROTATE_BYTES = 1 << 20

REQUIRED = ("event_id", "source", "occurred_at")
FIELDS = ("event_id", "source", "kind", "actor", "occurred_at", "body", "meta")


def _offset_file(spool: Path) -> Path:
    return spool.with_suffix(spool.suffix + ".offset")


def _read_offset(spool: Path) -> int:
    try:
        return int(_offset_file(spool).read_text().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def _write_offset(spool: Path, offset: int) -> None:
    tmp = _offset_file(spool).with_suffix(".tmp")
    tmp.write_text(str(offset))
    tmp.replace(_offset_file(spool))


def to_event(raw: dict) -> dict | None:
    """Validate one spooled line; None means skip it."""
    if any(not raw.get(k) for k in REQUIRED):
        return None
    event = {k: raw.get(k) for k in FIELDS}
    event["occurred_at"] = int(event["occurred_at"])
    if not isinstance(event["meta"], str):
        event["meta"] = json.dumps(event["meta"] or {})
    if event["body"] and is_one_time_code(event["body"]):
        return None
    return event


def run(send: Callable[[dict], None], flush: Callable[[], int], spool: Path = SPOOL,
        poll: float = 1.0, once: bool = False, commit: bool = True) -> None:
    """Deliver spooled events forever (or until caught up, with once=True)."""
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.touch(exist_ok=True)
    offset = _read_offset(spool)
    while True:
        with open(spool, "r+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            size = os.fstat(f.fileno()).st_size
            if offset > size:  # truncated by hand
                offset = 0
            if commit and offset == size and size > ROTATE_BYTES:
                f.truncate(0)
                offset = 0
                _write_offset(spool, 0)
            f.seek(offset)
            data = f.read()
            fcntl.flock(f, fcntl.LOCK_UN)

        # Only whole lines; a writer may be mid-line.
        end = data.rfind(b"\n") + 1
        sent = 0
        for line in data[:end].splitlines():
            if not line.strip():
                continue
            try:
                event = to_event(json.loads(line))
            except (ValueError, TypeError) as e:
                print(f"skipped a bad spool line: {e}", file=sys.stderr, flush=True)
                continue
            if event is None:
                print("skipped a spooled event (missing fields or a one-time code)", file=sys.stderr, flush=True)
                continue
            send(event)
            sent += 1
        if end:
            left = flush()
            if left:
                print(f"{left} event(s) not delivered, retrying", file=sys.stderr, flush=True)
                time.sleep(10)
                continue
            offset += end
            if commit:
                _write_offset(spool, offset)
            if sent:
                print(f"produced {sent} spooled event(s)", flush=True)
        if once:
            return
        time.sleep(poll)
