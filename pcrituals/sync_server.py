"""Standalone, self-hostable sync server for Easy Life.

Why a separate app and a separate database
------------------------------------------
The desktop app's API is bound to one machine and trusts it (loopback is
effectively admin). A sync server is the opposite: it is reachable from the
internet, holds other people's data, and must assume every request is hostile.
Keeping it in its own FastAPI app, its own SQLite file and its own auth tables
means a bug here can never read the local rituals database, and an operator can
self-host it on a VPS without shipping the whole desktop app.

Run it with:  PCRITUALS_SYNC_DB=/var/lib/pcrituals-sync.db python -m pcrituals.sync_server
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pcrituals.auth import _hash_password, _hash_token, _verify_password
from pcrituals.config import default_data_dir
from pcrituals.db import open_db

PREFIX = "/sync"
TOKEN_TTL_DAYS = 30
DEFAULT_PORT = 8788
DEFAULT_HOST = "0.0.0.0"

# A vault is rituals + deck + settings; past this size the client is misbehaving
# (or trying to use us as free storage).
MAX_VAULT_BYTES = 512 * 1024

# Stricter than the local app (which allows 4): this account protects data on
# someone else's server, so it gets the stronger rule.
MIN_PASSWORD_LEN = 8

# The local app stays permissive; a public server does not.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")

RATE_WINDOW_SECONDS = 300.0
RATE_MAX_FAILURES = 5


class SyncServerError(RuntimeError):
    """Raised by SyncStore for expected, reportable conditions."""


class RevisionConflict(SyncServerError):
    """The caller's base_revision did not match the stored revision."""

    def __init__(self, current_revision: int) -> None:
        super().__init__("revision conflict")
        self.current_revision = current_revision


@dataclass
class SyncConfig:
    """Everything the sync server needs to know about its environment."""

    db_path: Path
    max_vault_bytes: int = MAX_VAULT_BYTES
    token_ttl_days: int = TOKEN_TTL_DAYS


def default_db_path() -> Path:
    """Sync database location.

    Kept beside the app's data dir by default (a self-hoster who only runs the
    sync server still gets one predictable place), overridable via
    PCRITUALS_SYNC_DB so it can live on a separate volume.
    """
    env = os.environ.get("PCRITUALS_SYNC_DB")
    if env:
        return Path(env)
    return default_data_dir() / "sync" / "sync.db"


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------
class RateLimiter:
    """Sliding-window failure counter, keyed by whatever string the caller picks.

    Same shape as AuthManager's throttle, but keyed per username+client IP and
    living in its own object because this server is multi-account: one abusive
    account must not lock everybody else out.
    """

    def __init__(self, limit: int = RATE_MAX_FAILURES,
                 window: float = RATE_WINDOW_SECONDS) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self._window]
            self._hits[key] = hits
            return len(hits) < self._limit

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._hits.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        """Drop all state (tests start from a clean slate)."""
        with self._lock:
            self._hits.clear()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
class SyncStore:
    """Accounts, sync tokens and vaults on the server's own SQLite file.

    All access goes through pcrituals.db.open_db: FastAPI runs sync endpoints in
    a thread pool and a bare sqlite3 connection is not thread-safe.
    """

    def __init__(self, db_path: Path | str, *, token_ttl_days: int = TOKEN_TTL_DAYS) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = open_db(self.db_path)
        self._lock = threading.Lock()
        self.token_ttl_days = token_ttl_days
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                pw_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token_hash TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tokens_account ON tokens(account_id);
            CREATE TABLE IF NOT EXISTS vaults (
                account_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    # ---- accounts ----------------------------------------------------------
    def create_account(self, username: str, password: str) -> dict:
        username = username.strip()
        with self._lock:
            # COLLATE NOCASE on the column makes this check case-insensitive too.
            if self._conn.execute("SELECT 1 FROM accounts WHERE username=?",
                                  (username,)).fetchone():
                raise SyncServerError("username already taken")
            account_id = secrets.token_hex(8)
            self._conn.execute(
                "INSERT INTO accounts (id, username, pw_hash, created_at) VALUES (?,?,?,?)",
                (account_id, username, _hash_password(password), _now()))
            self._conn.commit()
        return {"id": account_id, "username": username}

    def verify(self, username: str, password: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT id, pw_hash FROM accounts WHERE username=?",
            ((username or "").strip(),)).fetchone()
        if row and _verify_password(password or "", row["pw_hash"]):
            return row["id"]
        return None

    def username(self, account_id: str) -> str:
        row = self._conn.execute("SELECT username FROM accounts WHERE id=?",
                                 (account_id,)).fetchone()
        return row["username"] if row else ""

    def password_matches(self, account_id: str, password: str) -> bool:
        row = self._conn.execute("SELECT pw_hash FROM accounts WHERE id=?",
                                 (account_id,)).fetchone()
        return bool(row) and _verify_password(password or "", row["pw_hash"])

    def delete_account(self, account_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tokens WHERE account_id=?", (account_id,))
            self._conn.execute("DELETE FROM vaults WHERE account_id=?", (account_id,))
            self._conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self._conn.commit()

    def count_accounts(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]

    # ---- tokens ------------------------------------------------------------
    def issue_token(self, account_id: str, ttl_days: int | None = None) -> str:
        """Mint a token. Only its SHA-256 hash is persisted."""
        ttl = self.token_ttl_days if ttl_days is None else ttl_days
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        with self._lock:
            self._conn.execute(
                "INSERT INTO tokens (token_hash, account_id, created_at, expires_at) "
                "VALUES (?,?,?,?)",
                (_hash_token(token), account_id, now.isoformat(),
                 (now + timedelta(days=ttl)).isoformat()))
            self._conn.commit()
        return token

    def account_for_token(self, token: str) -> Optional[dict]:
        """Return {"id","username"} for a live token, else None (expired = dead)."""
        if not token:
            return None
        token_hash = _hash_token(token)
        row = self._conn.execute(
            "SELECT t.account_id, t.expires_at, a.username FROM tokens t "
            "JOIN accounts a ON a.id = t.account_id WHERE t.token_hash=?",
            (token_hash,)).fetchone()
        if not row:
            return None
        try:
            expired = datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc)
        except ValueError:
            expired = True
        if expired:
            with self._lock:
                self._conn.execute("DELETE FROM tokens WHERE token_hash=?", (token_hash,))
                self._conn.commit()
            return None
        return {"id": row["account_id"], "username": row["username"]}

    def count_tokens(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM tokens").fetchone()["c"]

    def token_hashes(self) -> list[str]:
        rows = self._conn.execute("SELECT token_hash FROM tokens").fetchall()
        return [r["token_hash"] for r in rows]

    def expire_token(self, token: str) -> None:
        """Force a token past its expiry (used by tests and admin tooling)."""
        with self._lock:
            self._conn.execute("UPDATE tokens SET expires_at=? WHERE token_hash=?",
                               ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                                _hash_token(token)))
            self._conn.commit()

    # ---- vaults ------------------------------------------------------------
    def get_vault(self, account_id: str) -> dict:
        row = self._conn.execute(
            "SELECT data, revision, updated_at FROM vaults WHERE account_id=?",
            (account_id,)).fetchone()
        if not row:
            return {"vault": None, "revision": 0, "updated_at": None}
        try:
            data = json.loads(row["data"])
        except (ValueError, TypeError):
            # A corrupt row must not take the endpoint down; treat it as empty
            # and let the next push overwrite it.
            data = None
        return {"vault": data, "revision": int(row["revision"]),
                "updated_at": row["updated_at"]}

    def put_vault(self, account_id: str, vault: dict, *,
                  base_revision: Optional[int] = None, force: bool = False) -> dict:
        """Store a vault, incrementing the revision.

        Without force, base_revision must equal the stored revision or a
        RevisionConflict is raised: two machines editing offline would otherwise
        silently overwrite each other.
        """
        payload = json.dumps(vault, default=str)
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT revision FROM vaults WHERE account_id=?", (account_id,)).fetchone()
            current = int(row["revision"]) if row else 0
            if not force and base_revision != current:
                raise RevisionConflict(current)
            revision = current + 1
            self._conn.execute(
                "INSERT INTO vaults (account_id, data, revision, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET data=excluded.data, "
                "revision=excluded.revision, updated_at=excluded.updated_at",
                (account_id, payload, revision, now))
            self._conn.commit()
        return {"revision": revision, "updated_at": now}

    def vault_bytes(self, account_id: str) -> int:
        return len(json.dumps(self.get_vault(account_id)["vault"] or {}, default=str).encode())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# HTTP app
# ---------------------------------------------------------------------------
def create_app(config: Optional[SyncConfig] = None):
    """Build the sync server app. `config` defaults to env-driven settings."""
    cfg = config or SyncConfig(db_path=default_db_path())
    store = SyncStore(cfg.db_path, token_ttl_days=cfg.token_ttl_days)
    limiter = RateLimiter()

    app = FastAPI(
        title="Easy Life Sync",
        version="1.0.0",
        description="Self-hostable vault sync for Easy Life. Tokens are hashed at "
                    "rest; vault writes are revision-checked.",
    )
    # The desktop client is not a browser, but a self-hoster may point a web UI
    # at this server; auth is Bearer-token based, never cookies.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])
    app.state.sync_store = store
    app.state.sync_config = cfg
    app.state.sync_limiter = limiter

    # ---- helpers -----------------------------------------------------------
    def _rate_key(kind: str, username: str, request: Request) -> str:
        client = request.client.host if request.client else "unknown"
        return f"{kind}|{username.lower()}|{client}"

    def _current_account(request: Request) -> dict:
        header = request.headers.get("Authorization") or ""
        token = header.removeprefix("Bearer ").strip()
        account = store.account_for_token(token)
        if not account:
            # One message for "no token", "wrong token" and "expired token":
            # the caller learns nothing about which.
            raise HTTPException(status_code=401, detail="invalid or expired token")
        return account

    def _body(body: Any) -> dict:
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected a JSON object")
        return body

    def _check_vault_size(vault: Any) -> None:
        size = len(json.dumps(vault, default=str).encode("utf-8"))
        if size > cfg.max_vault_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"vault is too large ({size} bytes, limit {cfg.max_vault_bytes})")

    # ---- accounts ----------------------------------------------------------
    @app.post(f"{PREFIX}/register")
    def register(request: Request, body: dict):
        data = _body(body)
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        key = _rate_key("register", username, request)
        if not limiter.allowed(key):
            raise HTTPException(status_code=429, detail="too many attempts, wait a few minutes")
        problem = _registration_problem(username, password)
        if problem:
            limiter.record_failure(key)
            raise HTTPException(status_code=400, detail=problem)
        try:
            account = store.create_account(username, password)
        except SyncServerError:
            limiter.record_failure(key)
            raise HTTPException(status_code=400, detail="username already taken") from None
        limiter.clear(key)
        token = store.issue_token(account["id"])
        return {"token": token, "username": account["username"],
                "revision": store.get_vault(account["id"])["revision"]}

    @app.post(f"{PREFIX}/login")
    def login(request: Request, body: dict):
        data = _body(body)
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        key = _rate_key("login", username, request)
        if not limiter.allowed(key):
            raise HTTPException(status_code=429, detail="too many attempts, wait a few minutes")
        account_id = store.verify(username, password)
        if not account_id:
            limiter.record_failure(key)
            # Never say which half was wrong.
            raise HTTPException(status_code=401, detail="invalid username or password")
        limiter.clear(key)
        token = store.issue_token(account_id)
        return {"token": token, "username": store.username(account_id),
                "revision": store.get_vault(account_id)["revision"]}

    @app.get(f"{PREFIX}/me")
    def me(request: Request):
        account = _current_account(request)
        state = store.get_vault(account["id"])
        return {"username": account["username"], "revision": state["revision"],
                "updated_at": state["updated_at"]}

    @app.delete(f"{PREFIX}/account")
    def delete_account(request: Request, body: dict):
        account = _current_account(request)
        data = _body(body)
        if not store.password_matches(account["id"], str(data.get("password") or "")):
            # Re-authenticate before destroying data: a stolen token alone must
            # not be enough to delete an account.
            raise HTTPException(status_code=403, detail="password is incorrect")
        store.delete_account(account["id"])
        return {"deleted": True}

    # ---- vaults ------------------------------------------------------------
    @app.get(f"{PREFIX}/vault")
    def get_vault(request: Request):
        account = _current_account(request)
        return store.get_vault(account["id"])

    @app.put(f"{PREFIX}/vault")
    def put_vault(request: Request, body: dict):
        account = _current_account(request)
        data = _body(body)
        vault = data.get("vault")
        if not isinstance(vault, dict):
            raise HTTPException(status_code=400, detail="vault must be an object")
        _check_vault_size(vault)
        try:
            base_revision = int(data.get("base_revision"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="base_revision must be an integer") from None
        try:
            result = store.put_vault(account["id"], vault, base_revision=base_revision)
        except RevisionConflict as conflict:
            # A bare {"detail": N} would be ambiguous, so the current revision
            # rides along and the client can rebase without a second round trip.
            return JSONResponse(
                {"detail": "revision conflict", "current_revision": conflict.current_revision},
                status_code=409)
        return result

    @app.put(f"{PREFIX}/vault/force")
    def force_vault(request: Request, body: dict):
        account = _current_account(request)
        data = _body(body)
        vault = data.get("vault")
        if not isinstance(vault, dict):
            raise HTTPException(status_code=400, detail="vault must be an object")
        _check_vault_size(vault)
        return store.put_vault(account["id"], vault, force=True)

    return app


def _registration_problem(username: str, password: str) -> str:
    """Return a human-readable reason, or "" when the input is acceptable."""
    if not USERNAME_RE.match(username):
        return ("username must be 3-32 characters, letters/digits/._- only")
    if len(password) < MIN_PASSWORD_LEN:
        return f"password must be at least {MIN_PASSWORD_LEN} characters"
    return ""


def main() -> None:
    import uvicorn

    host = os.environ.get("PCRITUALS_SYNC_HOST", DEFAULT_HOST)
    port = int(os.environ.get("PCRITUALS_SYNC_PORT", str(DEFAULT_PORT)))
    # log_level=info is enough: nothing here ever logs a token.
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
