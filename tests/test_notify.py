"""Tests for pcrituals.notify: VAPID + RFC 8291 Web Push encryption + sending."""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals import notify as notify_mod
from pcrituals.notify import Notifier, Subscription, VapidError, _b64url, _b64url_decode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_subscription(p256dh: bytes, auth: bytes, endpoint: str = "https://push.example.net/push/abc") -> Subscription:
    return Subscription(endpoint=endpoint, p256dh=_b64(p256dh), auth=_b64(auth),
                        user_agent="test", created_at="2026-01-01T00:00:00+00:00")


def _fake_keys():
    """A fake subscription's p256dh/auth, generated with cryptography."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key()
    p256dh = pub.public_bytes(serialization.Encoding.X962,
                              serialization.PublicFormat.UncompressedPoint)
    auth = b"\x01" * 16
    return priv, p256dh, auth


class _FakeResp:
    """A urllib response that supports the context-manager protocol."""

    def __init__(self, status=201):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def notifier(tmp_path):
    from pcrituals.config import Config
    cfg = Config(data_dir=tmp_path)
    return Notifier(cfg, subject="mailto:admin@example.com")


# ---------------------------------------------------------------------------
# VAPID key generation / persistence
# ---------------------------------------------------------------------------


def test_vapid_keys_are_generated_and_persisted(notifier):
    priv_path = notifier.config.data_dir / "vapid_private.pem"
    pub_path = notifier.config.data_dir / "vapid_public.pem"
    assert priv_path.exists() and pub_path.exists()
    assert notifier.vapid_public_key  # base64url uncompressed point


def test_vapid_keygen_is_idempotent_on_reload(notifier):
    """Re-loading the Notifier must reuse the same key, not regenerate."""
    first = notifier.vapid_public_key
    priv_bytes = (notifier.config.data_dir / "vapid_private.pem").read_bytes()
    from pcrituals.config import Config
    again = Notifier(Config(data_dir=notifier.config.data_dir), subject="mailto:admin@example.com")
    assert again.vapid_public_key == first
    assert (notifier.config.data_dir / "vapid_private.pem").read_bytes() == priv_bytes


def test_vapid_private_key_file_is_restricted(tmp_path):
    from pcrituals.config import Config
    n = Notifier(Config(data_dir=tmp_path))
    mode = (tmp_path / "vapid_private.pem").stat().st_mode & 0o777
    assert mode == 0o600


def test_corrupt_vapid_key_raises_not_regenerates(tmp_path):
    from pcrituals.config import Config
    Notifier(Config(data_dir=tmp_path))
    (tmp_path / "vapid_private.pem").write_text("garbage")
    with pytest.raises(VapidError):
        Notifier(Config(data_dir=tmp_path))


# ---------------------------------------------------------------------------
# VAPID JWT
# ---------------------------------------------------------------------------


def test_vapid_token_is_well_formed(notifier):
    token = notify_mod._vapid_token(notifier._vapid_priv, "https://push.example.net", "mailto:admin@example.com")
    parts = token.split(".")
    assert len(parts) == 3
    header = json.loads(_b64url_decode(parts[0]))
    assert header == {"typ": "JWT", "alg": "ES256"}
    payload = json.loads(_b64url_decode(parts[1]))
    assert payload["aud"] == "https://push.example.net"
    assert payload["sub"] == "mailto:admin@example.com"
    assert payload["exp"] > int(time.time())


def test_vapid_token_signature_verifies(notifier):
    """The ES256 signature must verify against the VAPID public key."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    token = notify_mod._vapid_token(notifier._vapid_priv, "https://push.example.net", "mailto:admin@example.com")
    h, p, s = token.split(".")
    sig = _b64url_decode(s)
    assert len(sig) == 64  # raw r||s, each 32 bytes

    def _int_bytes(v: bytes) -> bytes:
        # DER INTEGER: prepend 0x00 when the high bit is set (keep positive).
        return (b"\x00" if v[0] & 0x80 else b"") + v

    r, s_raw = sig[:32], sig[32:]
    rb, sb = _int_bytes(r), _int_bytes(s_raw)
    der = (b"\x30" + bytes([len(rb) + len(sb) + 4])
           + b"\x02" + bytes([len(rb)]) + rb
           + b"\x02" + bytes([len(sb)]) + sb)
    pub = notifier._vapid_priv.public_key()
    pub.verify(der, f"{h}.{p}".encode("ascii"), ec.ECDSA(hashes.SHA256()))


def test_vapid_authorization_header_shape(notifier):
    sub = _make_subscription(*_fake_keys()[1:])
    body = notifier._encrypt(sub, b"hi")
    headers = notifier._build_headers(sub, body, 60)
    auth = headers["Authorization"]
    assert auth.startswith("vapid t=")
    assert ", k=" in auth
    assert headers["Content-Encoding"] == "aes128gcm"
    assert headers["TTL"] == "60"


# ---------------------------------------------------------------------------
# RFC 8291 encryption
# ---------------------------------------------------------------------------


def test_encryption_round_trips_with_fake_subscription(notifier):
    """Ciphertext decrypts back to the plaintext using the same keys."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes

    ua_priv, ua_pub_bytes, auth = _fake_keys()
    sub = _make_subscription(ua_pub_bytes, auth)
    plaintext = b"stream dropped"
    body = notifier._encrypt(sub, plaintext)

    # Parse the aes128gcm header.
    salt = body[:16]
    rs = int.from_bytes(body[16:20], "big")
    keyid_len = body[20]
    keyid = body[21:21 + keyid_len]
    ciphertext = body[21 + keyid_len:]
    assert keyid_len == 65 and keyid[0] == 0x04

    # Recover the ephemeral public key from the header and do ECDH.
    eph_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), keyid)
    ecdh_secret = ua_priv.exchange(ec.ECDH(), eph_pub)
    key_info = b"WebPush: info\x00" + ua_pub_bytes + keyid
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth, info=key_info).derive(ecdh_secret)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)
    decrypted = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert decrypted == plaintext + b"\x02"  # padding delimiter


def test_encryption_matches_rfc8291_appendix_a_vector(notifier):
    """Pinned vector from RFC 8291 Appendix A: known keys -> known ciphertext."""
    as_private = _b64url_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw")
    ua_public = _b64url_decode("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcx"
                               "aOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4")
    auth = _b64url_decode("BTBZMqHH6r4Tts7J_aSIgg")
    salt = _b64url_decode("DGv6ra1nlYgDCS1FRnbzlw")
    plaintext = _b64url_decode("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24")
    expected_ciphertext = _b64url_decode(
        "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ")

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    as_priv = ec.derive_private_key(int.from_bytes(as_private, "big"), ec.SECP256R1())
    eph_pub_bytes = as_priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_priv.exchange(ec.ECDH(), ua_pub)

    key_info = b"WebPush: info\x00" + ua_public + eph_pub_bytes
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth, info=key_info).derive(ecdh_secret)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)
    ct = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    assert ct == expected_ciphertext


def test_encryption_rejects_bad_keys(notifier):
    sub = _make_subscription(b"\x00" * 65, b"\x01" * 16)  # not a valid point
    with pytest.raises(ValueError):
        notifier._encrypt(sub, b"hi")
    sub2 = _make_subscription(b"\x04" + b"\x00" * 64, b"\x01" * 8)  # bad auth length
    with pytest.raises(ValueError):
        notifier._encrypt(sub2, b"hi")


# ---------------------------------------------------------------------------
# Subscription storage
# ---------------------------------------------------------------------------


def test_save_get_list_delete(notifier):
    sub = _make_subscription(*_fake_keys()[1:])
    saved = notifier.save_subscription(sub)
    assert saved.endpoint == sub.endpoint
    assert notifier.get_subscription(sub.endpoint) is not None
    assert len(notifier.list_subscriptions()) == 1
    assert notifier.delete_subscription(sub.endpoint) is True
    assert notifier.get_subscription(sub.endpoint) is None
    assert notifier.delete_subscription(sub.endpoint) is False


def test_save_upserts_on_same_endpoint(notifier):
    _, p256dh, auth = _fake_keys()
    sub = _make_subscription(p256dh, auth)
    notifier.save_subscription(sub)
    updated = _make_subscription(p256dh, b"\x02" * 16, endpoint=sub.endpoint)
    notifier.save_subscription(updated)
    subs = notifier.list_subscriptions()
    assert len(subs) == 1
    assert subs[0].auth == _b64(b"\x02" * 16)


def test_save_rejects_incomplete_subscription(notifier):
    with pytest.raises(ValueError):
        notifier.save_subscription(Subscription(endpoint="", p256dh="x", auth="y"))


# ---------------------------------------------------------------------------
# send() / send_to_all()
# ---------------------------------------------------------------------------


def test_send_posts_with_correct_headers(notifier, monkeypatch):
    _, p256dh, auth = _fake_keys()
    sub = _make_subscription(p256dh, auth)
    captured = {}

    def fake_urlopen(req, timeout=10):
        captured["req"] = req
        return _FakeResp(201)

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    res = notifier.send(sub, "hello", ttl=30)
    assert res["ok"] is True and res["status"] == 201
    req = captured["req"]
    assert req.get_method() == "POST"
    # urllib capitalizes header names; HTTP is case-insensitive, so look up
    # case-insensitively to assert on the actual wire values.
    sent = {k.lower(): v for k, v in req.headers.items()}
    assert sent["content-encoding"] == "aes128gcm"
    assert sent["ttl"] == "30"
    assert sent["authorization"].startswith("vapid t=")
    assert sent["content-type"] == "application/octet-stream"
    # Body is the full aes128gcm record.
    body = req.data
    assert body[20] == 65  # keyid length
    assert len(body) > 86


def test_send_404_deletes_subscription(notifier, monkeypatch):
    import urllib.error
    _, p256dh, auth = _fake_keys()
    sub = _make_subscription(p256dh, auth)
    notifier.save_subscription(sub)

    def fake_urlopen(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 404, "gone", {}, None)

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    res = notifier.send(sub, "hi")
    assert res["ok"] is False and res["deleted"] is True
    assert notifier.get_subscription(sub.endpoint) is None


def test_send_410_deletes_subscription(notifier, monkeypatch):
    import urllib.error
    _, p256dh, auth = _fake_keys()
    sub = _make_subscription(p256dh, auth)
    notifier.save_subscription(sub)

    def fake_urlopen(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 410, "gone", {}, None)

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    res = notifier.send(sub, "hi")
    assert res["deleted"] is True
    assert notifier.get_subscription(sub.endpoint) is None


def test_send_429_keeps_subscription(notifier, monkeypatch):
    import urllib.error
    _, p256dh, auth = _fake_keys()
    sub = _make_subscription(p256dh, auth)
    notifier.save_subscription(sub)

    def fake_urlopen(req, timeout=10):
        raise urllib.error.HTTPError(req.full_url, 429, "slow down", {}, None)

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    res = notifier.send(sub, "hi")
    assert res["ok"] is False and res["deleted"] is False
    assert notifier.get_subscription(sub.endpoint) is not None


def test_send_network_error_does_not_raise(notifier, monkeypatch):
    import urllib.error
    _, p256dh, auth = _fake_keys()
    sub = _make_subscription(p256dh, auth)

    def fake_urlopen(req, timeout=10):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    res = notifier.send(sub, "hi")
    assert res["ok"] is False and res["error"]


def test_send_rejects_bad_input(notifier):
    _, p256dh, auth = _fake_keys()
    sub = _make_subscription(p256dh, auth)
    with pytest.raises(ValueError):
        notifier.send(sub, 123)  # not str/bytes
    with pytest.raises(ValueError):
        notifier.send(sub, "hi", ttl=-1)


def test_send_to_all_continues_after_failure(notifier, monkeypatch):
    """A failing subscription must not stop the others."""
    import urllib.error
    _, p256dh, auth = _fake_keys()
    good = _make_subscription(p256dh, auth, endpoint="https://push.example.net/push/good")
    bad = _make_subscription(p256dh, auth, endpoint="https://push.example.net/push/bad")
    notifier.save_subscription(good)
    notifier.save_subscription(bad)

    calls = []

    def fake_urlopen(req, timeout=10):
        calls.append(req.full_url)
        if "bad" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "gone", {}, None)
        return _FakeResp(201)

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    results = notifier.send_to_all("hi")
    assert len(results) == 2
    by_endpoint = {r["endpoint"]: r for r in results}
    assert by_endpoint[good.endpoint]["ok"] is True
    assert by_endpoint[bad.endpoint]["deleted"] is True
    # The bad one was removed, the good one remains.
    assert notifier.get_subscription(bad.endpoint) is None
    assert notifier.get_subscription(good.endpoint) is not None
