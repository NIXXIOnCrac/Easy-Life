"""Pairing and authentication.

Flow:
  1. Windows app generates a short-lived pairing code + displays QR (the code,
     NOT a secret token, is in the QR).
  2. Phone scans QR / types code and POSTs /api/pair with the code.
  3. Server returns a long-lived bearer/device token.
  4. Phone uses the token for all future requests.

Security properties:
  - Pairing codes are single-use and expire (config.pairing_code_ttl).
  - The QR contains only the ephemeral code, never a permanent secret.
  - Tokens are stored securely (SQLite); never logged.
  - Devices can be revoked; all pairings can be reset.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pcrituals.config import Config


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_code(code: str) -> str:
    """Canonical form of a pairing code.

    Codes are stored upper-case. Every lookup, redemption and deletion must use
    this same form: the DELETE previously used the raw user input, so a code
    typed in lower case validated (case-insensitive matching) but was never
    consumed - the "single-use" pairing code stayed redeemable.
    """
    return (code or "").strip().upper()



def _hash_token(token: str) -> str:
    """Store only a SHA-256 of tokens so a DB leak doesn't expose working keys."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class PairingCode:
    code: str
    device_hint: str
    created_at: datetime
    expires_at: datetime


class Security:
    """Manages pairing codes, device tokens, server secret."""

    def __init__(self, config: Config):
        self.config = config
        config.ensure_dirs()
        from pcrituals.db import open_db
        self._conn = open_db(config.db_file)
        # Guards multi-statement sequences (create/redeem) that must not
        # interleave when two FastAPI threads hit them at once.
        self._lock = threading.Lock()
        self._secret: str | None = None
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pairing_codes (
                code TEXT PRIMARY KEY,
                device_hint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                last_seen TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                is_local INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self._conn.commit()

    # ---- server secret ----------------------------------------------------
    @property
    def server_secret(self) -> str:
        """A persistent per-install secret used to derive local-only access.

        Creation is atomic (O_EXCL). A plain "does it exist? then write" is a
        race: two processes started together -- the app and a script, or two
        app instances -- can both see the file missing, both write a *different*
        secret, and the later write wins while the loser keeps using a secret
        that is no longer on disk, so tokens derived from it stop validating.
        """
        if self._secret:
            return self._secret
        with self._lock:
            if self._secret:
                return self._secret
            f = self.config.secret_file
            try:
                fd = os.open(str(f), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass          # another writer won; read their value below
            except OSError:
                # Unwritable location: fall back to a non-atomic create so the
                # app still works, rather than failing to start.
                if not f.exists():
                    f.write_text(secrets.token_hex(32))
                try:
                    f.chmod(0o600)
                except OSError:
                    pass
            else:
                try:
                    os.write(fd, secrets.token_hex(32).encode("utf-8"))
                finally:
                    os.close(fd)
            # Reading immediately can catch the window where the file exists but
            # the winning writer has not written its bytes yet (the create and
            # the write are two steps), which returned an empty secret. Retry
            # briefly until there is content.
            for _ in range(200):
                try:
                    value = f.read_text().strip()
                except OSError:
                    value = ""
                if value:
                    break
                time.sleep(0.005)
            self._secret = value
        return self._secret

    # ---- pairing codes ----------------------------------------------------
    def create_pairing_code(self, device_hint: str = "") -> PairingCode:
        code = secrets.token_hex(3).upper()  # 6-char code, e.g. "3F9A2B"
        now = _now()
        exp = now + timedelta(seconds=self.config.pairing_code_ttl)
        # Invalidate any existing active codes for good hygiene (single active
        # pair). Both statements hold the lock so two concurrent requests can't
        # interleave into two live codes.
        with self._lock:
            self._conn.execute("DELETE FROM pairing_codes")
            self._conn.execute(
                "INSERT INTO pairing_codes (code, device_hint, created_at, expires_at) "
                "VALUES (?,?,?,?)",
                (code, device_hint or "phone", now.isoformat(), exp.isoformat()),
            )
            self._conn.commit()
        return PairingCode(code=code, device_hint=device_hint, created_at=now, expires_at=exp)

    def validate_pairing_code(self, code: str) -> PairingCode | None:
        norm = _normalise_code(code)
        if not norm:
            return None
        row = self._conn.execute(
            "SELECT * FROM pairing_codes WHERE code=?", (norm,)).fetchone()
        if not row:
            return None
        expires = datetime.fromisoformat(row["expires_at"])
        if expires < _now():
            self._conn.execute("DELETE FROM pairing_codes WHERE code=?", (norm,))
            self._conn.commit()
            return None
        return PairingCode(
            code=row["code"], device_hint=row["device_hint"],
            created_at=datetime.fromisoformat(row["created_at"]), expires_at=expires)

    def redeem_pairing_code(self, code: str) -> PairingCode | None:
        # Consume the code atomically: validating then deleting in two steps let
        # two requests redeem the same code concurrently.
        with self._lock:
            pc = self.validate_pairing_code(code)
            if pc is None:
                return None
            self._conn.execute("DELETE FROM pairing_codes WHERE code=?", (pc.code,))
            self._conn.commit()
        return pc

    # ---- device tokens ----------------------------------------------------
    def issue_device_token(self, device_name: str, pc: PairingCode) -> dict:
        device_id = secrets.token_hex(8)
        token = f"pcrit{device_id}{secrets.token_urlsafe(32)}"
        now = _now()
        self._conn.execute(
            "INSERT INTO devices (id, name, token_hash, last_seen, created_at, revoked) "
            "VALUES (?,?,?,?,?,0)",
            (device_id, device_name, _hash_token(token), now.isoformat(), now.isoformat()),
        )
        self._conn.commit()
        return {"device_id": device_id, "token": token, "name": device_name}

    def register_local(self) -> str:
        """Return (creating if needed) the local-only access token."""
        # Stable per-install local token derived from the server secret.
        h = hmac.new(self.server_secret.encode(), b"local-access", hashlib.sha256)
        token = h.hexdigest()
        return token

    def is_valid_token(self, token: str) -> bool:
        if not token:
            return False
        # Local token check.
        if hmac.compare_digest(token, self.register_local()):
            return True
        row = self._conn.execute(
            "SELECT revoked, last_seen, id FROM devices WHERE token_hash=?",
            (_hash_token(token),)).fetchone()
        if not row:
            return False
        if row["revoked"]:
            return False
        # Update last_seen (throttled-ish; fine for a local app).
        self._conn.execute(
            "UPDATE devices SET last_seen=? WHERE id=?", (_now().isoformat(), row["id"]))
        self._conn.commit()
        return True

    def list_devices(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, name, last_seen, created_at, revoked, is_local "
            "FROM devices ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]

    def revoke_device(self, device_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE devices SET revoked=1 WHERE id=?", (device_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def revoke_all(self) -> int:
        cur = self._conn.execute("UPDATE devices SET revoked=1")
        self._conn.commit()
        return cur.rowcount

    def pairing_ui_state(self) -> dict:
        """Current pairing status for the desktop UI."""
        pc = self._conn.execute("SELECT * FROM pairing_codes").fetchone()
        code = None
        expires_in = None
        if pc:
            expires = datetime.fromisoformat(pc["expires_at"])
            expires_in = max(0, int((expires - _now()).total_seconds()))
        return {
            "active_code": pc["code"] if pc else None,
            "expires_in": expires_in,
        }