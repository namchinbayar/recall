"""`recall doctor`: check that every part of recall works, and say so when one stops.

Each check answers one question a person would ask ("is the sink running?",
"can I still reach Atlas?") and returns a line. A check is `ok`, a `warn`
(shown, never pushed) or a failure. With `--alert`, a failure seen on two
checks in a row goes to the phone through Bark (BARK_KEY in the environment,
encrypted when BARK_ENC_KEY is set too); without the key, or if the send fails,
a desktop notification instead. One more message follows when everything is
fine again, and a reminder once a day while something stays broken.

What it cannot see: the machine being off, or Proton Pass being unreachable,
since the doctor then cannot start either.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import db, embed
from .config import Settings
from .stream import spool

STATE = spool.STATE_DIR / "doctor.json"
UNITS = ("recall-sink", "recall-notify", "recall-spool", "recall-metrics", "recall-sync", "recall-sessions", "recall-serve", "recall-learn")
VAULT_KEY = Path.home() / ".config" / "recall" / "vault.key"
METRICS_MAX_AGE = timedelta(minutes=10)
EVENTS_MAX_AGE = timedelta(hours=36)
SPOOL_MAX_AGE = timedelta(minutes=10)
FLAPPING = 3                    # restarts between two checks that count as a failure
REMIND = timedelta(hours=24)
CALM_TITLES = ("recall: still", "recall: all fine", "recall: fixed", "recall: test")  # sent without the alarm


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False          # ok=False and warn=True: shown, but never alerted

    @property
    def failed(self) -> bool:
        return not self.ok and not self.warn


def _age(then: datetime, now: datetime) -> str:
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 90:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


# ----- the checks ----------------------------------------------------------

def _systemctl(*args: str) -> str:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def unit_state(unit: str) -> dict | None:
    """None when the unit is not installed or not enabled (nothing to check)."""
    if _systemctl("is-enabled", f"{unit}.service") != "enabled":
        return None
    shown = dict(line.split("=", 1) for line in
                 _systemctl("show", f"{unit}.service", "-p", "ActiveState,NRestarts").splitlines() if "=" in line)
    return {"active": shown.get("ActiveState", "unknown"), "restarts": int(shown.get("NRestarts") or 0)}


def check_services(previous: dict, states: dict[str, dict | None] | None = None) -> tuple[list[Check], dict]:
    """previous / returned dict: unit -> restart count at the last check."""
    out: list[Check] = []
    counts: dict[str, int] = {}
    for unit in UNITS:
        state = states.get(unit) if states is not None else unit_state(unit)
        if state is None:
            continue
        counts[unit] = state["restarts"]
        since = state["restarts"] - previous.get(unit, state["restarts"])
        if state["active"] != "active":
            out.append(Check(unit, False, f"service is {state['active']}"))
        elif since >= FLAPPING:
            out.append(Check(unit, False, f"service restarted {since} times since the last check"))
        else:
            out.append(Check(unit, True, "running"))
    return out, counts


def check_atlas(settings: Settings) -> Check:
    try:
        db.client(settings).admin.command("ping")
        n = db.chunks(settings).estimated_document_count()
    except Exception as exc:  # any driver error means the same thing here
        return Check("atlas", False, f"cannot reach the database ({type(exc).__name__})")
    return Check("atlas", True, f"reachable, {n:,} chunks")


def check_indexes(settings: Settings) -> Check:
    try:
        found = {i["name"]: i for i in db.chunks(settings).list_search_indexes()}
    except Exception as exc:
        return Check("search indexes", False, f"cannot list them ({type(exc).__name__})")
    bad = [name for name in (settings.index_name, settings.text_index_name)
           if not found.get(name, {}).get("queryable")]
    if bad:
        return Check("search indexes", False, f"not ready: {', '.join(bad)} (search will fail or miss results)")
    return Check("search indexes", True, "both ready")


def check_plain_indexes(settings: Settings) -> Check:
    """A missing one breaks nothing, it makes lists and counts slow: shown, never alerted."""
    try:
        missing = db.missing_plain_indexes(db.chunks(settings))
    except Exception as exc:
        return Check("plain indexes", False, f"cannot list them ({type(exc).__name__})", warn=True)
    if missing:
        return Check("plain indexes", False, f"missing: {', '.join(missing)} (`recall init` creates them)", warn=True)
    return Check("plain indexes", True, f"all {len(db.PLAIN_INDEXES)} present")


def check_embeddings(settings: Settings) -> Check:
    try:
        vector = embed.embed_query(settings, "recall doctor")
    except Exception as exc:
        return Check("embeddings", False, f"Voyage call failed ({type(exc).__name__}); nothing new can be saved or searched")
    if len(vector) != settings.dimensions:
        return Check("embeddings", False, f"Voyage returned {len(vector)} numbers, the index expects {settings.dimensions}")
    return Check("embeddings", True, f"{settings.voyage_model} answers")


def check_metrics(settings: Settings, now: datetime) -> Check:
    host = socket.gethostname()
    try:
        last = db.client(settings)[settings.db_name]["metrics"].find_one(
            {"meta.host": host, "meta.kind": "system"}, {"ts": 1}, sort=[("ts", -1)])
    except Exception as exc:
        return Check("metrics", False, f"cannot read them ({type(exc).__name__})")
    if last is None:
        return Check("metrics", False, f"no samples from {host}", warn=True)
    if now - last["ts"] > METRICS_MAX_AGE:
        return Check("metrics", False, f"last sample from {host} was {_age(last['ts'], now)}")
    return Check("metrics", True, f"last sample {_age(last['ts'], now)}")


def check_events(settings: Settings, now: datetime) -> Check:
    try:
        last = db.chunks(settings).find_one({"tags": "event"}, {"created_at": 1}, sort=[("created_at", -1)])
    except Exception as exc:
        return Check("events", False, f"cannot read them ({type(exc).__name__})")
    if last is None:
        return Check("events", False, "no notifications or conversations stored", warn=True)
    if now - last["created_at"] > EVENTS_MAX_AGE:
        return Check("events", False, f"nothing stored since {_age(last['created_at'], now)} (quiet day, or the stream is stuck)", warn=True)
    return Check("events", True, f"last one stored {_age(last['created_at'], now)}")


def check_spool(now: datetime, path: Path | None = None) -> Check:
    path = path or spool.SPOOL
    try:
        info = path.stat()
    except FileNotFoundError:
        return Check("spool", True, "nothing waiting")
    waiting = info.st_size - spool._read_offset(path)
    age = now - datetime.fromtimestamp(info.st_mtime, timezone.utc)
    if waiting > 0 and age > SPOOL_MAX_AGE:
        return Check("spool", False, f"{waiting:,} bytes of Hermes events waiting since {_age(now - age, now)}")
    return Check("spool", True, "nothing waiting" if waiting <= 0 else "delivering")


def check_vault_key(path: Path | None = None) -> Check:
    path = path or VAULT_KEY
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return Check("vault key", False, f"{path} is missing; vault entries cannot be opened without it")
    if mode & 0o077:
        return Check("vault key", False, f"{path} is readable by other users (mode {mode:o}, should be 600)")
    return Check("vault key", True, "present, private")


def check_assistant() -> Check | None:
    """The Claude API key behind `recall chat` and the session summaries. A free call."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    import anthropic

    model = os.environ.get("RECALL_MODEL", "claude-opus-5-5")
    try:
        anthropic.Anthropic(max_retries=1, timeout=20.0).models.retrieve(model)
    except anthropic.AuthenticationError:
        return Check("assistant", False, "the Claude API key was rejected; chat and session summaries will fail")
    except anthropic.NotFoundError:
        return Check("assistant", False, f"model {model} is not available to this key")
    except anthropic.APIError as exc:
        return Check("assistant", False, f"Claude API not reachable ({type(exc).__name__})", warn=True)
    return Check("assistant", True, f"Claude API key works ({model})")


def check_online(url: str | None = None, get: Callable[..., httpx.Response] = httpx.get) -> Check | None:
    """The hosted recall (RECALL_ONLINE_HEALTHZ, e.g. https://name.example/healthz): up and able
    to reach its database. Skipped when the setting is empty."""
    url = url if url is not None else os.environ.get("RECALL_ONLINE_HEALTHZ", "").strip()
    if not url:
        return None
    try:
        resp = get(url, timeout=20, headers={"User-Agent": "recall-doctor"})
        ok = resp.status_code == 200 and resp.json().get("ok") is True
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return Check("online", False, f"{url} not reachable ({type(exc).__name__})")
    if not ok:
        return Check("online", False, f"{url} answered {resp.status_code}: the server is down or cannot reach the database")
    return Check("online", True, "hosted recall up, database reachable")


def run(settings: Settings, restarts: dict | None = None, now: datetime | None = None) -> tuple[list[Check], dict]:
    """All checks, plus the restart counts to hand to the next run."""
    now = now or datetime.now(timezone.utc)
    checks, counts = check_services(restarts or {})
    atlas = check_atlas(settings)
    checks.append(atlas)
    if atlas.ok:  # the rest of the database checks would only repeat the same failure
        checks += [check_indexes(settings), check_plain_indexes(settings),
                   check_metrics(settings, now), check_events(settings, now)]
    checks += [check_embeddings(settings), check_spool(now), check_vault_key()]
    checks += [c for c in (check_assistant(), check_online()) if c]
    return checks, counts


def report(checks: list[Check]) -> list[str]:
    width = max(len(c.name) for c in checks)
    return [f"  {'ok  ' if c.ok else 'warn' if c.warn else 'FAIL'}  {c.name:<{width}}  {c.detail}" for c in checks]


# ----- alerts --------------------------------------------------------------

def decide(state: dict, failed: dict[str, str], now: datetime) -> tuple[dict, tuple[str, str] | None]:
    """Next state and the message to send, if any.

    state: {"pending": {name: detail}, "alerted": {name: detail}, "last_alert": iso}.
    A failure is announced the second time in a row it is seen; a transient
    timeout therefore never reaches the phone.
    """
    pending, alerted = state.get("pending", {}), state.get("alerted", {})
    confirmed = {n: d for n, d in failed.items() if n in pending or n in alerted}
    new = {n: d for n, d in confirmed.items() if n not in alerted}
    last = datetime.fromisoformat(state["last_alert"]) if state.get("last_alert") else None
    nxt = {"pending": {n: d for n, d in failed.items() if n not in confirmed},
           "alerted": confirmed, "last_alert": state.get("last_alert")}

    message = None
    count = f"{len(confirmed)} problem{'s' if len(confirmed) > 1 else ''}"
    listing = "\n".join(f"{n}: {d}" for n, d in confirmed.items())
    if new:
        message = (f"recall: {count}", listing)
    elif confirmed and last and now - last >= REMIND:
        message = (f"recall: still {count}", listing)
    elif alerted and not confirmed:
        message = ("recall: all fine again" if not failed else "recall: fixed", "Fixed: " + ", ".join(alerted))
    if message:
        nxt["last_alert"] = now.isoformat()
    elif not confirmed:
        nxt["last_alert"] = None
    return nxt, message


def _seal(payload: dict, key: str) -> dict:
    """Encrypt a Bark payload with the key set in the Bark app (Push Encryption: AES256, CBC)."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv = secrets.token_hex(8)  # 16 characters, fresh per message; the app takes it from the request
    padder = padding.PKCS7(128).padder()
    data = padder.update(json.dumps(payload).encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    return {"ciphertext": base64.b64encode(enc.update(data) + enc.finalize()).decode(), "iv": iv}


def push(title: str, body: str) -> str:
    """Send to the phone through Bark. Returns how it went out: 'bark', 'desktop' or 'failed'.

    A new problem rings even when the phone is silenced (Bark's "critical" level, which
    needs Critical Alerts allowed for Bark on the phone); reminders, recoveries and the
    test message arrive as time-sensitive notifications.
    """
    bark_key = os.environ.get("BARK_KEY")
    if bark_key:
        payload = {
            "title": title[:250], "body": body[:1024], "group": "recall", "url": "https://recall.example.com/",
            "level": "timeSensitive" if title.startswith(CALM_TITLES) else "critical",
        }
        try:
            enc_key = os.environ.get("BARK_ENC_KEY")
            if enc_key:
                payload = _seal(payload, enc_key)
            httpx.post(os.environ.get("BARK_URL", "https://api.day.app").rstrip("/") + "/push", timeout=15,
                       json={**payload, "device_key": bark_key}).raise_for_status()
            return "bark"
        except (httpx.HTTPError, ValueError):  # ValueError: a key that is not 32 characters
            pass
    try:
        subprocess.run(["notify-send", "--app-name=recall", "--urgency=critical", title, body], timeout=10, check=True)
        return "desktop"
    except (OSError, subprocess.SubprocessError):
        return "failed"


def load_state(path: Path = STATE) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)


def once(settings: Settings, alert: bool, echo: Callable[[str], None], state_path: Path = STATE,
         send: Callable[[str, str], str] = push) -> list[Check]:
    state = load_state(state_path)
    checks, counts = run(settings, state.get("restarts"))
    if not alert:  # a look by hand leaves the alert state alone
        return checks
    nxt, message = decide(state, {c.name: c.detail for c in checks if c.failed}, datetime.now(timezone.utc))
    if message:
        echo(f"alert ({send(*message)}): {message[0]}")
    save_state({**nxt, "restarts": counts}, state_path)
    return checks


def run_loop(settings: Settings, interval: int, alert: bool, echo: Callable[[str], None]) -> None:
    """Check forever. Prints only the lines that changed, so the journal stays short."""
    shown: dict[str, str] = {}
    while True:
        try:
            checks = once(settings, alert, echo)
        except Exception as exc:  # the doctor must outlive whatever it is checking
            echo(f"doctor: check failed to run ({type(exc).__name__}: {exc})")
        else:
            for c, line in zip(checks, report(checks)):
                if shown.get(c.name) != line and (not c.ok or c.name in shown):
                    echo(line.strip())
                shown[c.name] = line
        time.sleep(interval)
