"""Hermes gateway hook for a cloud-hosted Hermes: send each exchange to recall.

Posts one plain-JSON record per finished exchange to the events_inbox topic with
the Confluent Cloud REST API (the documented produce API; it can't do Avro, so
recall's sink reads this topic as JSON). Python standard library only.

Settings, in the cloud agent's .env:
  RECALL_EVENTS_URL     https://<cluster>.<region>.aws.confluent.cloud/kafka/v3/clusters/<cluster-id>/topics/events_inbox/records
  RECALL_EVENTS_KEY     API key of the service account that may only WRITE events_inbox
  RECALL_EVENTS_SECRET  its secret
Records that fail to send are kept in hooks/cloud-recall-events/retry.jsonl and
sent again before the next one. Hermes truncates message and response to 500 chars.
"""

import asyncio
import base64
import hashlib
import json
import re
import os
import threading
import time
import urllib.request
from pathlib import Path

RETRY = Path(__file__).with_name("retry.jsonl")
_lock = threading.Lock()


# Discord prefixes the message with a note for the model; it is not what was said.
_PLATFORM_NOTE = re.compile(r"^\[Triggering message id:[^\]]*\]\s*")


def _post(url: str, auth: str, event: dict) -> None:
    body = json.dumps({"value": {"type": "JSON", "data": event}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth}",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        reply = json.loads(resp.read() or b"{}")
    if reply.get("error_code", 200) != 200:
        raise RuntimeError(f"produce failed: {reply}")


def _deliver(event: dict) -> None:
    url = os.environ.get("RECALL_EVENTS_URL", "")
    key, secret = os.environ.get("RECALL_EVENTS_KEY", ""), os.environ.get("RECALL_EVENTS_SECRET", "")
    if not (url and key and secret):
        print("[cloud-recall-events] RECALL_EVENTS_URL/KEY/SECRET not set; skipping", flush=True)
        return
    auth = base64.b64encode(f"{key}:{secret}".encode()).decode()
    with _lock:
        pending = []
        if RETRY.exists():
            pending = [json.loads(l) for l in RETRY.read_text().splitlines() if l.strip()]
        pending.append(event)
        failed = []
        for e in pending:
            if failed:  # keep order once something fails
                failed.append(e)
                continue
            try:
                _post(url, auth, e)
            except Exception as err:
                print(f"[cloud-recall-events] send failed, will retry: {err}", flush=True)
                failed.append(e)
        failed = failed[-500:]
        RETRY.write_text("".join(json.dumps(e) + "\n" for e in failed))


async def handle(event_type: str, context: dict):
    message = _PLATFORM_NOTE.sub("", (context.get("message") or "").strip())
    response = (context.get("response") or "").strip()
    if not (message or response):
        return
    now_ms = int(time.time() * 1000)
    session_id = context.get("session_id") or ""
    event = {
        "event_id": hashlib.sha256(f"cloud\0{session_id}\0{message}\0{now_ms}".encode()).hexdigest()[:32],
        "source": "hermes",
        "kind": context.get("platform") or None,
        "actor": context.get("user_id") or None,
        "occurred_at": now_ms,
        "body": f"Me: {message}\nHermes: {response}",
        "meta": {
            "agent": "cloud",
            "session_id": session_id,
            "chat_id": context.get("chat_id"),
            "chat_type": context.get("chat_type"),
            "platform": context.get("platform"),
        },
    }
    await asyncio.to_thread(_deliver, event)
