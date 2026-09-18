"""Web Push notifications (VAPID + RFC 8291 encryption), no third-party libs.

Implements the raw Web Push protocol on top of the `cryptography` package
(already a dependency) so an installed PWA on the user's iPhone (iOS 16.4+)
gets a push when OBS's stream drops.

Two independent pieces of crypto are involved:

  * VAPID (RFC 8292) -- an ES256 (ECDSA P-256 / SHA-256) JWT signed with a
    per-install key pair, sent in the ``Authorization`` header so the push
    service can attribute the message to us. The key pair is generated once
    and persisted in the data dir (``vapid_private.pem`` / ``vapid_public.pem``).

  * Message encryption (RFC 8291) -- the payload is encrypted with AES-128-GCM
    using keys derived (via HKDF) from an ECDH shared secret between our
    ephemeral key and the subscription's ``p256dh`` key, mixed with the
    subscription's ``auth`` secret. The result is a single ``aes128gcm``
    record (86-byte header + ciphertext + 16-byte tag).

Storage follows the app's conventions (pcrituals/db.py ``open_db`` +
``LockedConnection``, ``CREATE TABLE IF NOT EXISTS`` migrations, ISO-8601 UTC
timestamps) so subscriptions live in the same SQLite file as everything else.

Design rules:
  * Never crash the caller on a single failed subscription -- ``send`` and
    ``send_to_all`` return structured results instead of raising for network
    errors, and a 404/410 (subscription gone) deletes the row.
  * Clear exceptions for *bad input* (missing/invalid keys, bad subscription).
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from pcrituals.config import Config
from pcrituals.db import open_db

# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    """Base64url without padding (JWT / Web Push wire format)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    """Decode unpadded base64url, tolerating padding if present."""
    s = data.strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# VAPID keys
# ---------------------------------------------------------------------------


class VapidError(Exception):
    """Raised when VAPID keys are missing or unusable."""


def _load_or_create_vapid(data_dir: Path) -> tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]:
    """Load the persisted VAPID key pair, generating it only if missing.

    The private key is written with 0o600 permissions. If a key file exists
    but is corrupt, we raise rather than silently regenerate (regenerating
    would invalidate every in-flight push the push service still attributes
    to the old key).
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    priv_path = data_dir / "vapid_private.pem"
    pub_path = data_dir / "vapid_public.pem"

    if priv_path.exists() and pub_path.exists():
        try:
            priv = load_pem_private_key(priv_path.read_bytes(), password=None)
            pub = serialization.load_pem_public_key(pub_path.read_bytes())
            if not isinstance(priv, ec.EllipticCurvePrivateKey) or not isinstance(pub, ec.EllipticCurvePublicKey):
                raise VapidError("VAPID key files are not EC keys")
            return priv, pub
        except (ValueError, TypeError) as exc:
            raise VapidError(f"VAPID key files are corrupt: {exc}") from exc

    # Generate a fresh pair. Use O_EXCL so two processes racing to create the
    # key don't each write a different one (same pattern as the server secret).
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    try:
        fd = os.open(str(priv_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(priv_pem)
    except FileExistsError:
        # Another writer won the race; read their key instead of overwriting.
        try:
            priv = load_pem_private_key(priv_path.read_bytes(), password=None)
            pub = priv.public_key()
        except (ValueError, TypeError) as exc:
            raise VapidError(f"VAPID key files are corrupt: {exc}") from exc
    # Public key is not secret; write it best-effort. Recompute it from the
    # FINAL priv: in the FileExistsError branch (and when only the private key
    # file exists) the key we ended up with is not the one we generated, so a
    # pub_pem computed earlier wrote a public key that did not match the
    # private key - and the browser would then subscribe with a key the server
    # cannot use.
    try:
        pub_pem = priv.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        pub_path.write_bytes(pub_pem)
    except OSError:
        pass
    return priv, priv.public_key()


def _vapid_public_key_b64(priv: ec.EllipticCurvePrivateKey) -> str:
    """The VAPID public key as the raw uncompressed P-256 point (65 bytes)."""
    pub = priv.public_key()
    return _b64url(pub.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    ))


# Push endpoints must be public HTTPS URLs. Without this a client could store
# file:///etc/passwd (a local file-existence oracle) or http://127.0.0.1:<port>
# and make the app SSRF itself.
_ENDPOINT_MAX = 2048


def validate_endpoint(endpoint: str) -> str:
    """Return the endpoint if it is a safe public https URL, else raise."""
    from urllib.parse import urlparse

    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("endpoint is required")
    if len(endpoint) > _ENDPOINT_MAX:
        raise ValueError("endpoint is too long")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https":
        raise ValueError("push endpoint must be an https:// URL")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("push endpoint has no host")
    try:
        import ipaddress
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("push endpoint must be a public address")
    except ValueError as exc:
        # Not an IP literal: a normal hostname is fine (a name that resolves to
        # a private address is the push service's problem, not ours).
        if "must be a public address" in str(exc):
            raise
    return endpoint


def _sign_es256(priv: ec.EllipticCurvePrivateKey, signing_input: bytes) -> bytes:
    """ES256 signature: raw r||s (64 bytes), NOT the DER form.

    cryptography returns DER. Each INTEGER is prefixed with a 0x00 byte when
    its high bit is set (to keep it positive); the JWT spec wants the raw
    32-byte values, so strip any leading zero.
    """
    der = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r_len = der[3]
    r = der[4:4 + r_len]
    s = der[4 + r_len + 2:]
    # JOSE wants fixed 32-byte r and s. Merely stripping the DER sign byte
    # produced a 63-byte signature whenever r or s had a leading zero byte
    # (~0.8% of signatures), and the push service rejected those messages.
    r = r.lstrip(b"\x00").rjust(32, b"\x00")
    s = s.lstrip(b"\x00").rjust(32, b"\x00")
    sig = r + s
    assert len(sig) == 64, f"ES256 signature must be 64 bytes, got {len(sig)}"
    return sig


def _vapid_token(priv: ec.EllipticCurvePrivateKey, audience: str, subject: str, ttl: int = 3600) -> str:
    """Build a VAPID JWT (ES256) for the given push service audience.

    ``audience`` is the push service origin (scheme://host[:port]) parsed from
    the subscription endpoint. ``subject`` is a mailto: or https: contact.
    """
    now = int(time.time())
    header = {"typ": "JWT", "alg": "ES256"}
    payload = {
        "aud": audience,
        "exp": now + ttl,
        "sub": subject,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    )
    sig = _sign_es256(priv, signing_input.encode("ascii"))
    return signing_input + "." + _b64url(sig)


# ---------------------------------------------------------------------------
# Subscription model + storage
# ---------------------------------------------------------------------------


@dataclass
class Subscription:
    """A Web Push subscription as reported by the browser's PushManager.

    ``p256dh`` and ``auth`` are the base64url (unpadded) keys the browser
    generated for this subscription; they are required to encrypt messages.
    """

    endpoint: str
    p256dh: str
    auth: str
    user_agent: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "p256dh": self.p256dh,
            "auth": self.auth,
            "user_agent": self.user_agent,
            "created_at": self.created_at,
        }


class Notifier:
    """VAPID keys + subscription storage + send logic for Web Push."""

    def __init__(self, config: Config, *, subject: str = "mailto:admin@localhost"):
        self.config = config
        self.subject = subject
        config.ensure_dirs()
        self._vapid_priv, self._vapid_pub = _load_or_create_vapid(config.data_dir)
        self._conn = open_db(config.db_file)
        self._lock = threading.Lock()
        self._migrate()

    # ---- storage ----------------------------------------------------------

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint TEXT PRIMARY KEY,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def save_subscription(self, subscription: Subscription) -> Subscription:
        """Insert or replace a subscription. Returns the stored row."""
        if not subscription.endpoint or not subscription.p256dh or not subscription.auth:
            raise ValueError("subscription requires endpoint, p256dh and auth")
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(endpoint) DO UPDATE SET
                    p256dh = excluded.p256dh,
                    auth = excluded.auth,
                    user_agent = excluded.user_agent
                """,
                (subscription.endpoint, subscription.p256dh, subscription.auth,
                 subscription.user_agent, subscription.created_at or now),
            )
            self._conn.commit()
        return self.get_subscription(subscription.endpoint)

    def get_subscription(self, endpoint: str) -> Optional[Subscription]:
        row = self._conn.execute(
            "SELECT endpoint, p256dh, auth, user_agent, created_at "
            "FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,),
        ).fetchone()
        if row is None:
            return None
        return Subscription(endpoint=row["endpoint"], p256dh=row["p256dh"],
                            auth=row["auth"], user_agent=row["user_agent"],
                            created_at=row["created_at"])

    def list_subscriptions(self) -> list[Subscription]:
        rows = self._conn.execute(
            "SELECT endpoint, p256dh, auth, user_agent, created_at "
            "FROM push_subscriptions ORDER BY created_at"
        ).fetchall()
        return [Subscription(endpoint=r["endpoint"], p256dh=r["p256dh"],
                             auth=r["auth"], user_agent=r["user_agent"],
                             created_at=r["created_at"]) for r in rows]

    def delete_subscription(self, endpoint: str) -> bool:
        """Delete a subscription. Returns True if a row was removed."""
        with self._lock:
            res = self._conn.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
            self._conn.commit()
        return res.rowcount > 0

    # ---- VAPID ------------------------------------------------------------

    @property
    def vapid_public_key(self) -> str:
        """Base64url uncompressed P-256 public key (for the frontend)."""
        return _vapid_public_key_b64(self._vapid_priv)

    # ---- encryption (RFC 8291) -------------------------------------------

    def _encrypt(self, subscription: Subscription, payload: bytes) -> bytes:
        """Encrypt ``payload`` into a single aes128gcm record.

        Returns the full body: 86-byte header + ciphertext + 16-byte tag.
        """
        ua_public = _b64url_decode(subscription.p256dh)
        auth_secret = _b64url_decode(subscription.auth)
        if len(ua_public) != 65 or ua_public[0] != 0x04:
            raise ValueError("p256dh must be a 65-byte uncompressed P-256 point")
        if len(auth_secret) != 16:
            raise ValueError("auth must be 16 bytes")

        # Ephemeral ECDH key pair for this message.
        eph = ec.generate_private_key(ec.SECP256R1())
        eph_pub = eph.public_key()
        eph_pub_bytes = eph_pub.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )

        # ECDH shared secret with the subscription's public key.
        ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
        ecdh_secret = eph.exchange(ec.ECDH(), ua_pub)

        # HKDF to combine ECDH secret with the auth secret (RFC 8291 3.3).
        key_info = b"WebPush: info\x00" + ua_public + eph_pub_bytes
        ikm = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=auth_secret,
            info=key_info,
        ).derive(ecdh_secret)

        # RFC 8188 content encryption key + nonce.
        salt = secrets.token_bytes(16)
        cek = HKDF(
            algorithm=hashes.SHA256(),
            length=16,
            salt=salt,
            info=b"Content-Encoding: aes128gcm\x00",
        ).derive(ikm)
        nonce = HKDF(
            algorithm=hashes.SHA256(),
            length=12,
            salt=salt,
            info=b"Content-Encoding: nonce\x00",
        ).derive(ikm)

        # Single record: plaintext + 0x02 padding delimiter.
        record_size = 4096
        plaintext = payload + b"\x02"
        if len(plaintext) + 16 > record_size:
            raise ValueError("payload too large for a single Web Push record")

        ct = AESGCM(cek).encrypt(nonce, plaintext, None)

        # 86-byte header: salt(16) || rs(4, big-endian) || keyid_len(1) || keyid(65).
        header = salt + record_size.to_bytes(4, "big") + bytes([65]) + eph_pub_bytes
        return header + ct

    # ---- sending ----------------------------------------------------------

    def _build_headers(self, subscription: Subscription, body: bytes, ttl: int) -> dict:
        from urllib.parse import urlparse
        origin = urlparse(subscription.endpoint)
        audience = f"{origin.scheme}://{origin.netloc}"
        token = _vapid_token(self._vapid_priv, audience, self.subject)
        return {
            "Content-Type": "application/octet-stream",
            "Content-Encoding": "aes128gcm",
            "TTL": str(ttl),
            "Authorization": f"vapid t={token}, k={_vapid_public_key_b64(self._vapid_priv)}",
        }

    def send(self, subscription: Subscription, payload: str | bytes, ttl: int = 60) -> dict:
        """Send one push message. Returns a result dict, never raises for
        network/push-service errors.

        Result keys: ``ok`` (bool), ``status`` (int or None), ``deleted``
        (bool, True when a 404/410 removed the subscription), ``error`` (str,
        only when not ok).

        Raises ``ValueError`` for bad input (missing keys, bad payload).
        """
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes):
            raise ValueError("payload must be str or bytes")
        if ttl < 0:
            raise ValueError("ttl must be >= 0")

        # A stored row that is unusable (bad scheme, bad key) must return a
        # result, not raise: send_to_all callers are fire-and-forget, and one
        # bad row used to abort the whole loop so every push stopped silently.
        try:
            validate_endpoint(subscription.endpoint)
            body = self._encrypt(subscription, payload)
        except ValueError as exc:
            return {"ok": False, "status": None, "deleted": False, "error": str(exc)}
        headers = self._build_headers(subscription, body, ttl)
        req = urllib.request.Request(
            subscription.endpoint, data=body, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return {"ok": True, "status": resp.status, "deleted": False, "error": None}
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):
                self.delete_subscription(subscription.endpoint)
                return {"ok": False, "status": exc.code, "deleted": True,
                        "error": f"subscription gone ({exc.code})"}
            # 429 = rate limited; leave the subscription in place.
            return {"ok": False, "status": exc.code, "deleted": False,
                    "error": f"push service error {exc.code}"}
        except urllib.error.URLError as exc:
            return {"ok": False, "status": None, "deleted": False,
                    "error": f"network error: {exc.reason}"}
        except OSError as exc:
            return {"ok": False, "status": None, "deleted": False,
                    "error": f"network error: {exc}"}

    def send_to_all(self, payload: str | bytes, ttl: int = 60) -> list[dict]:
        """Send ``payload`` to every stored subscription.

        Returns a list of per-subscription result dicts (see ``send``), each
        tagged with the ``endpoint`` it refers to. A failure on one
        subscription never stops the others.
        """
        results = []
        for sub in self.list_subscriptions():
            endpoint = sub.endpoint
            try:
                res = self.send(sub, payload, ttl)
            except Exception as exc:  # noqa: BLE001
                # One unusable row must never stop the others.
                res = {"ok": False, "status": None, "deleted": False, "error": str(exc)}
            res["endpoint"] = endpoint
            results.append(res)
        return results
