"""Regression tests for the notification/push defects found by the verifier.

Each one is a bug that either killed the feature silently or made the app a
confused deputy.
"""
import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---- 1. one unusable subscription must never kill the rest ----------------
def test_send_to_all_survives_a_row_with_bad_keys(tmp_path, monkeypatch):
    """The bug: send() called _encrypt outside any try, and send_to_all had no
    per-row guard, so ONE malformed stored row aborted the loop. streamwatch
    swallows exceptions, so every future stream-drop push was silently lost."""
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    from pcrituals.config import load_config
    from pcrituals.notify import Notifier, Subscription

    n = Notifier(load_config())
    # a row that cannot be encrypted (garbage key material)
    n.save_subscription(Subscription(endpoint="https://push.example.com/bad",
                                     p256dh="AA,AA,AA", auth="AA"))
    results = n.send_to_all("hello")
    assert len(results) == 1
    assert results[0]["ok"] is False          # reported, not raised
    assert results[0]["endpoint"].endswith("/bad")


def test_send_returns_a_result_instead_of_raising_on_bad_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    from pcrituals.config import load_config
    from pcrituals.notify import Notifier, Subscription

    n = Notifier(load_config())
    res = n.send(Subscription(endpoint="https://push.example.com/x",
                              p256dh="not-a-point", auth="AA"), "hi")
    assert res["ok"] is False and res["error"]


# ---- 2. the endpoint must be a public https URL ---------------------------
@pytest.mark.parametrize("bad", [
    "file:///etc/passwd",                 # local file-existence oracle
    "http://127.0.0.1:8765/api/health",   # SSRF against the app itself
    "ftp://127.0.0.1:21/",
    "https://169.254.169.254/latest",     # cloud metadata
    "https://10.0.0.5/x",                 # private
    "https://[::1]/x",                    # loopback ipv6
    "",
])
def test_validate_endpoint_rejects_dangerous_urls(bad):
    from pcrituals.notify import validate_endpoint
    with pytest.raises(ValueError):
        validate_endpoint(bad)


@pytest.mark.parametrize("good", [
    "https://fcm.googleapis.com/fcm/send/abc",
    "https://updates.push.services.mozilla.com/wpush/v2/xyz",
])
def test_validate_endpoint_accepts_real_push_services(good):
    from pcrituals.notify import validate_endpoint
    assert validate_endpoint(good) == good


def test_validate_endpoint_rejects_an_absurdly_long_url():
    from pcrituals.notify import validate_endpoint
    with pytest.raises(ValueError):
        validate_endpoint("https://push.example.com/" + "a" * 5000)


# ---- 3. ES256 signatures must be exactly 64 bytes -------------------------
def test_es256_signature_is_always_64_bytes():
    """The bug: only the DER sign byte was stripped, never left-padded, so an
    r or s with a leading zero byte produced a 63-byte signature (~0.8% of
    signatures) and the push service rejected that message."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from pcrituals.notify import _sign_es256

    lengths = set()
    for _ in range(600):
        key = ec.generate_private_key(ec.SECP256R1())
        lengths.add(len(_sign_es256(key, b"x" * 32)))
    assert lengths == {64}, f"signature lengths seen: {sorted(lengths)}"


# ---- 4. a bad key must be rejected at subscribe time, not stored ----------
def test_subscribe_rejects_malformed_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from pcrituals.api import create_app

    c = TestClient(create_app())
    c.post("/api/auth/setup", json={"username": "u", "password": "pass1234"})
    tok = c.post("/api/auth/login",
                 json={"username": "u", "password": "pass1234"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}

    r = c.post("/api/notify/subscribe", headers=h, json={
        "endpoint": "https://push.example.com/1",
        "p256dh": "AA,AA,AA", "auth": "AA"})
    assert r.status_code == 400


def test_subscribe_rejects_a_non_https_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from pcrituals.api import create_app

    c = TestClient(create_app())
    c.post("/api/auth/setup", json={"username": "u", "password": "pass1234"})
    tok = c.post("/api/auth/login",
                 json={"username": "u", "password": "pass1234"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}

    r = c.post("/api/notify/subscribe", headers=h, json={
        "endpoint": "file:///etc/passwd", "p256dh": "AA", "auth": "AA"})
    assert r.status_code == 400
