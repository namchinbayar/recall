import os
from datetime import datetime, timedelta, timezone

from recall import doctor

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)


def test_services_report_stopped_and_flapping_units_and_skip_missing_ones():
    states = {"recall-sink": {"active": "active", "restarts": 2}, "recall-notify": {"active": "failed", "restarts": 0},
              "recall-spool": {"active": "active", "restarts": 9}, "recall-metrics": None}
    checks, counts = doctor.check_services({"recall-sink": 1, "recall-spool": 5}, states)
    by = {c.name: c for c in checks}
    assert set(by) == {"recall-sink", "recall-notify", "recall-spool"}
    assert by["recall-sink"].ok and by["recall-notify"].detail == "service is failed"
    assert by["recall-spool"].failed and "restarted 4 times" in by["recall-spool"].detail
    assert counts == {"recall-sink": 2, "recall-notify": 0, "recall-spool": 9}
    first, _ = doctor.check_services({}, {"recall-sink": {"active": "active", "restarts": 50}})
    assert first[0].ok     # no earlier count: nothing to compare with


def test_spool_fails_only_when_events_wait_for_a_while(tmp_path):
    spool = tmp_path / "spool.jsonl"
    assert doctor.check_spool(NOW, spool).ok
    spool.write_text('{"event_id": "1"}\n')
    old = (NOW - timedelta(minutes=30)).timestamp()
    os.utime(spool, (old, old))
    assert doctor.check_spool(NOW, spool).failed
    assert doctor.check_spool(NOW - timedelta(minutes=28), spool).detail == "delivering"
    spool.with_suffix(".jsonl.offset").write_text(str(spool.stat().st_size))
    assert doctor.check_spool(NOW, spool).detail == "nothing waiting"


def test_vault_key_must_exist_and_be_private(tmp_path):
    key = tmp_path / "vault.key"
    assert "missing" in doctor.check_vault_key(key).detail
    key.write_text("k")
    key.chmod(0o644)
    assert doctor.check_vault_key(key).failed
    key.chmod(0o600)
    assert doctor.check_vault_key(key).ok


def test_alert_waits_for_a_second_sighting_then_reminds_and_reports_recovery():
    state, msg = doctor.decide({}, {"atlas": "cannot reach the database"}, NOW)
    assert msg is None and state["pending"] == {"atlas": "cannot reach the database"}

    gone, msg = doctor.decide(state, {}, NOW)                    # a blip: never announced
    assert msg is None and gone == {"pending": {}, "alerted": {}, "last_alert": None}

    state, msg = doctor.decide(state, {"atlas": "cannot reach the database"}, NOW + timedelta(minutes=10))
    assert msg == ("recall: 1 problem", "atlas: cannot reach the database") and state["alerted"]

    same, msg = doctor.decide(state, {"atlas": "cannot reach the database"}, NOW + timedelta(hours=5))
    assert msg is None and same["last_alert"] == state["last_alert"]

    state, msg = doctor.decide(state, {"atlas": "cannot reach the database", "recall-sink": "service is failed"},
                               NOW + timedelta(hours=6))
    assert msg is None and state["pending"] == {"recall-sink": "service is failed"}
    state, msg = doctor.decide(state, {"atlas": "cannot reach the database", "recall-sink": "service is failed"},
                               NOW + timedelta(hours=6, minutes=10))
    assert msg[0] == "recall: 2 problems" and "recall-sink: service is failed" in msg[1]

    state, msg = doctor.decide(state, {"atlas": "cannot reach the database"}, NOW + timedelta(hours=31))
    assert msg[0] == "recall: still 1 problem"

    state, msg = doctor.decide(state, {}, NOW + timedelta(hours=32))
    assert msg == ("recall: all fine again", "Fixed: atlas") and state["alerted"] == {}
    assert doctor.decide(state, {}, NOW + timedelta(hours=33)) == ({"pending": {}, "alerted": {}, "last_alert": None}, None)


def test_once_sends_alerts_only_with_alert_and_keeps_state(tmp_path, monkeypatch):
    failing = [doctor.Check("atlas", False, "cannot reach the database"), doctor.Check("events", False, "quiet", warn=True)]
    monkeypatch.setattr(doctor, "run", lambda settings, restarts=None, now=None: (failing, {"recall-sink": 3}))
    sent, lines, path = [], [], tmp_path / "doctor.json"
    send = lambda title, body: sent.append((title, body)) or "bark"

    doctor.once(None, False, lines.append, path, send)
    assert not path.exists() and not sent
    doctor.once(None, True, lines.append, path, send)
    assert not sent and doctor.load_state(path)["restarts"] == {"recall-sink": 3}
    doctor.once(None, True, lines.append, path, send)
    assert sent == [("recall: 1 problem", "atlas: cannot reach the database")]      # the warning is not alerted
    assert lines == ["alert (bark): recall: 1 problem"]
    assert [l.split()[0] for l in doctor.report(failing)] == ["FAIL", "warn"]


def test_push_falls_back_to_a_desktop_notification(monkeypatch):
    ran = []
    monkeypatch.delenv("BARK_KEY", raising=False)
    monkeypatch.setattr(doctor.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    assert doctor.push("recall: 1 problem", "atlas: down") == "desktop"
    assert ran[0][0] == "notify-send" and ran[0][-2:] == ["recall: 1 problem", "atlas: down"]


def test_push_rings_for_a_new_problem_and_encrypts_when_a_key_is_set(monkeypatch):
    import base64, json
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    posted = []

    class Ok:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(doctor.httpx, "post", lambda url, **kw: posted.append((url, kw["json"])) or Ok())
    monkeypatch.setenv("BARK_KEY", "devicekey")
    monkeypatch.delenv("BARK_ENC_KEY", raising=False)
    monkeypatch.delenv("BARK_URL", raising=False)
    assert doctor.push("recall: 1 problem", "atlas: down") == "bark"
    assert doctor.push("recall: all fine again", "Fixed: atlas") == "bark"
    assert posted[0][0] == "https://api.day.app/push" and posted[0][1]["device_key"] == "devicekey"
    assert [p[1]["level"] for p in posted] == ["critical", "timeSensitive"]

    key = "k" * 32
    monkeypatch.setenv("BARK_ENC_KEY", key)
    assert doctor.push("recall: 1 problem", "atlas: down") == "bark"
    sealed = posted[2][1]
    assert set(sealed) == {"ciphertext", "iv", "device_key"} and len(sealed["iv"]) == 16
    dec = Cipher(algorithms.AES(key.encode()), modes.CBC(sealed["iv"].encode())).decryptor()
    unpad = padding.PKCS7(128).unpadder()
    plain = unpad.update(dec.update(base64.b64decode(sealed["ciphertext"])) + dec.finalize()) + unpad.finalize()
    assert json.loads(plain)["body"] == "atlas: down" and json.loads(plain)["level"] == "critical"


def test_online_checks_the_hosted_health_route_and_is_skipped_without_a_url():
    import httpx

    def answer(status, body):
        return lambda url, **kw: httpx.Response(status, json=body, request=httpx.Request("GET", url))

    def down(url, **kw):
        raise httpx.ConnectError("no route", request=httpx.Request("GET", url))

    url = "https://recall.example/healthz"
    assert doctor.check_online("") is None
    assert doctor.check_online(url, answer(200, {"ok": True})).ok
    assert doctor.check_online(url, answer(503, {"ok": False})).failed
    assert doctor.check_online(url, answer(200, {"ok": False})).failed
    assert doctor.check_online(url, down).failed


def test_an_alert_opens_the_hosted_recall_when_its_address_is_known(monkeypatch):
    posted = []

    class Ok:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(doctor.httpx, "post", lambda url, **kw: posted.append(kw["json"]) or Ok())
    monkeypatch.setenv("BARK_KEY", "devicekey")
    for name in ("BARK_ENC_KEY", "BARK_URL", "RECALL_ONLINE_HEALTHZ", "RECALL_ONLINE_URL"):
        monkeypatch.delenv(name, raising=False)
    doctor.push("recall: 1 problem", "atlas: down")
    assert "url" not in posted[-1]                      # no address set: no link, and no made-up one
    monkeypatch.setenv("RECALL_ONLINE_URL", "https://name.example/mcp")
    doctor.push("recall: 1 problem", "atlas: down")
    assert posted[-1]["url"] == "https://name.example/"
    monkeypatch.setenv("RECALL_ONLINE_HEALTHZ", "https://other.example/healthz")
    assert doctor.app_url() == "https://other.example/"  # what the doctor itself checks comes first
    monkeypatch.setenv("RECALL_ONLINE_HEALTHZ", "http://127.0.0.1:8791/healthz")
    assert doctor.app_url() == "https://name.example/"   # a local address is not one a phone can open
