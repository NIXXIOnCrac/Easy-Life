"""Local accounts + login sessions.

This is a *local* login: accounts live in the app's own database, passwords are
stored only as salted PBKDF2 hashes, and a successful login issues a session
token the client sends on every request.

Behaviour:
  - Until the first account is created the app is open (as before).
  - Once an account exists, every control endpoint requires a valid session
    (or a paired-device token for the phone), even from localhost.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from pcrituals.config import Config

PBKDF2_ITERATIONS = 240_000
SESSION_TTL_DAYS = 30
MIN_PASSWORD_LEN = 4

# Login throttling. Keyed on username+client, with a second cap per client so a
# single host cannot probe many usernames.
LOCKOUT_WINDOW = 300.0        # seconds a failure is remembered
LOCKOUT_THRESHOLD = 5         # failures for one username+client before lockout
LOCKOUT_IP_THRESHOLD = 20     # failures from one client across all usernames
MAX_TRACKED_FAILURES = 2_000  # hard bound on the failure dict (memory safety)


class AuthError(RuntimeError):
    """Invalid credentials or a rejected auth operation."""

    status = 401


class ThrottleError(AuthError):
    """Too many failed attempts — the caller must back off."""

    status = 429


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthManager:
    """Accounts and sessions, backed by the app's SQLite database."""

    def __init__(self, config: Config) -> None:
        self.config = config
        config.ensure_dirs()
        from pcrituals.db import open_db
        self._conn = open_db(config.db_file)
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                label TEXT DEFAULT ''
            );
            """
        )
        # Added later: onboarding flag (safe to re-run).
        try:
            self._conn.execute("ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    # ---- accounts ----------------------------------------------------------
    def has_users(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) c FROM users").fetchone()
        return row["c"] > 0

    def user_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

    def create_user(self, username: str, password: str, *, allow_existing: bool = False) -> dict:
        username = (username or "").strip()
        if not username:
            raise AuthError("Username is required")
        if len(password or "") < MIN_PASSWORD_LEN:
            raise AuthError(f"Password must be at least {MIN_PASSWORD_LEN} characters")
        with self._lock:
            if not allow_existing and self.has_users():
                raise AuthError("An account already exists")
            if self._conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise AuthError("That username is taken")
            uid = secrets.token_hex(8)
            self._conn.execute(
                "INSERT INTO users (id, username, password_hash, created_at) VALUES (?,?,?,?)",
                (uid, username, _hash_password(password), _now()))
            self._conn.commit()
        return {"id": uid, "username": username}

    # A hash of a fixed string, used to spend the same PBKDF2 work for an unknown
    # username as for a known one. Without it, a login attempt for a nonexistent
    # user returned ~41x faster, so the identical error message leaked which
    # usernames exist.
    _DUMMY_HASH = _hash_password("pcrituals-dummy-password", salt=b"\x00" * 16)

    def verify(self, username: str, password: str) -> Optional[str]:
        """Return the user id if credentials are valid, else None."""
        row = self._conn.execute("SELECT id, password_hash FROM users WHERE username=?",
                                 ((username or "").strip(),)).fetchone()
        if not row:
            # Spend the same work as a real check so timing does not reveal
            # whether the username exists.
            _verify_password(password or "", self._DUMMY_HASH)
            return None
        if _verify_password(password or "", row["password_hash"]):
            return row["id"]
        return None

    def change_password(self, user_id: str, old: str, new: str) -> None:
        row = self._conn.execute("SELECT username, password_hash FROM users WHERE id=?",
                                 (user_id,)).fetchone()
        if not row or not _verify_password(old, row["password_hash"]):
            raise AuthError("Current password is incorrect")
        if len(new or "") < MIN_PASSWORD_LEN:
            raise AuthError(f"New password must be at least {MIN_PASSWORD_LEN} characters")
        with self._lock:
            self._conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                               (_hash_password(new), user_id))
            # Invalidate other sessions after a password change.
            self._conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._conn.commit()

    # ---- login throttle ----------------------------------------------------
    def _throttle(self, username: str, client: str = "") -> None:
        """Refuse a login once an identity has failed too often.

        Keyed on username *and* client address so that someone else on the LAN
        cannot lock the real user out by deliberately failing their logins. A
        second, higher cap keyed on the client alone stops one host from
        probing many usernames.

        `_failures` is pruned on every call: without that, a caller sending a
        stream of random usernames would grow this dict without bound.
        """
        now = time.time()
        ukey = f"{username.strip().lower()}|{client}"
        ikey = f"ip:{client}" if client else None

        with self._lock:
            if len(self._failures) > MAX_TRACKED_FAILURES:
                self._failures = {
                    k: [t for t in v if now - t < LOCKOUT_WINDOW]
                    for k, v in list(self._failures.items())
                }
                self._failures = {k: v for k, v in self._failures.items() if v}

            hits = [t for t in self._failures.get(ukey, []) if now - t < LOCKOUT_WINDOW]
            if hits:
                self._failures[ukey] = hits
            else:
                self._failures.pop(ukey, None)

            ip_hits = []
            if ikey:
                ip_hits = [t for t in self._failures.get(ikey, []) if now - t < LOCKOUT_WINDOW]
                if ip_hits:
                    self._failures[ikey] = ip_hits
                else:
                    self._failures.pop(ikey, None)

            if len(hits) >= LOCKOUT_THRESHOLD or len(ip_hits) >= LOCKOUT_IP_THRESHOLD:
                # Tell the user how long to wait; "a few minutes" with no number
                # is the kind of thing that makes people assume the app is broken.
                worst = hits if len(hits) >= LOCKOUT_THRESHOLD else ip_hits
                remaining = max(1, int(LOCKOUT_WINDOW - (now - min(worst)) + 0.999))
                raise ThrottleError(
                    f"Too many attempts. Try again in {remaining} seconds."
                )

    def _record_failure(self, username: str, client: str = "") -> None:
        now = time.time()
        with self._lock:
            self._failures.setdefault(f"{username.strip().lower()}|{client}", []).append(now)
            if client:
                self._failures.setdefault(f"ip:{client}", []).append(now)

    # ---- sessions ----------------------------------------------------------
    def login(self, username: str, password: str, label: str = "local",
              client: str = "") -> dict:
        self._throttle(username, client)
        uid = self.verify(username, password)
        if not uid:
            self._record_failure(username, client)
            raise AuthError("Incorrect username or password")
        self._failures.pop(f"{username.strip().lower()}|{client}", None)
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = now + SESSION_TTL_DAYS * 86400
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen, label) "
                "VALUES (?,?,?,?,?,?)",
                (_hash_token(token), uid,
                 datetime.fromtimestamp(now, timezone.utc).isoformat(),
                 datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                 datetime.fromtimestamp(now, timezone.utc).isoformat(), label))
            self._conn.execute("UPDATE users SET last_login=? WHERE id=?",
                               (datetime.fromtimestamp(now, timezone.utc).isoformat(), uid))
            # Drop this user's expired sessions so the table cannot grow forever.
            self._conn.execute(
                "DELETE FROM sessions WHERE user_id=? AND expires_at<?",
                (uid, datetime.fromtimestamp(now, timezone.utc).isoformat()))
            self._conn.commit()
        name = self._conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()["username"]
        return {"token": token, "username": name, "expires_in": SESSION_TTL_DAYS * 86400}

    def validate_session(self, token: str) -> Optional[dict]:
        if not token:
            return None
        row = self._conn.execute(
            "SELECT s.user_id, s.expires_at, u.username, u.onboarded FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
            (_hash_token(token),)).fetchone()
        if not row:
            return None
        try:
            if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
                with self._lock:
                    self._conn.execute("DELETE FROM sessions WHERE token_hash=?",
                                       (_hash_token(token),))
                    self._conn.commit()
                return None
        except Exception:
            return None
        with self._lock:
            self._conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?",
                               (datetime.now(timezone.utc).isoformat(), _hash_token(token)))
            self._conn.commit()
        return {"user_id": row["user_id"], "username": row["username"],
                "onboarded": bool(row["onboarded"])}

    def set_onboarded(self, user_id: str, value: bool = True) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET onboarded=? WHERE id=?",
                               (1 if value else 0, user_id))
            self._conn.commit()

    def logout(self, token: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token_hash=?",
                               (_hash_token(token),))
            self._conn.commit()

    def logout_all(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions")
            self._conn.commit()
            return cur.rowcount

    def list_sessions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT s.label, s.created_at, s.last_seen, s.expires_at, u.username "
            "FROM sessions s JOIN users u ON u.id = s.user_id ORDER BY s.last_seen DESC").fetchall()
        return [dict(r) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()