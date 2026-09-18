"""App-side glue between the local install and a self-hosted sync server.

The sync *token* is a credential, so it lives in its own file inside the data
directory with owner-only permissions. It is deliberately kept out of the app's
settings dict and out of the vault: a vault can be pushed to a server and pulled
onto another PC, and credentials must never travel inside it.

Nothing here contacts a network until the user explicitly connects, so a default
install never talks to anything external.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pcrituals.config import Config
from pcrituals.sync import SyncClient, SyncConflict, SyncError
from pcrituals.vault import apply_vault, build_vault

STATE_FILE = "sync.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SyncLink:
    """Owns the local sync state and performs push/pull against the server."""

    def __init__(self, config: Config) -> None:
        self.config = config
        config.ensure_dirs()
        self._path = Path(config.data_dir) / STATE_FILE
        self._state = self._load()

    # ---- persistence -------------------------------------------------------
    def _load(self) -> dict:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text("utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            pass
        return {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2), "utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — token is a credential
        except OSError:
            pass
        tmp.replace(self._path)

    # ---- views -------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self._state.get("url") and self._state.get("token"))

    def public_state(self) -> dict:
        """Everything the UI may see. Never includes the token."""
        return {
            "configured": self.configured,
            "url": self._state.get("url", ""),
            "username": self._state.get("username", ""),
            "auto_sync": bool(self._state.get("auto_sync")),
            "revision": int(self._state.get("revision") or 0),
            "last_sync": self._state.get("last_sync"),
            "last_error": self._state.get("last_error"),
        }

    def client(self) -> SyncClient:
        if not self.configured:
            raise SyncError("Not connected to a sync server")
        return SyncClient(self._state["url"], self._state.get("token", ""))

    # ---- connection --------------------------------------------------------
    def connect(self, url: str, username: str, password: str,
                register: bool = False) -> dict:
        base = SyncClient.normalize_url(url)
        if not base:
            raise SyncError("Enter the sync server address")
        anon = SyncClient(base)
        result = anon.register(username, password) if register else anon.login(username, password)
        token = result.get("token")
        if not token:
            raise SyncError("The sync server did not return a token")
        self._state.update({
            "url": base,
            "username": result.get("username") or username,
            "token": token,
            "revision": int(result.get("revision") or 0),
            "last_error": None,
        })
        self._save()
        return self.public_state()

    def disconnect(self) -> dict:
        """Forget the local link. The remote account is left untouched."""
        self._state = {}
        self._save()
        return self.public_state()

    def set_auto_sync(self, enabled: bool) -> dict:
        self._state["auto_sync"] = bool(enabled)
        self._save()
        return self.public_state()

    # ---- transfer ----------------------------------------------------------
    def push(self, app: Any) -> dict:
        vault = build_vault(app)
        client = self.client()
        try:
            result = client.push(vault, int(self._state.get("revision") or 0))
        except SyncConflict as conflict:
            # Someone else wrote first. Tell the caller so it can choose, rather
            # than silently clobbering their data.
            self._state["last_error"] = "The server has newer data"
            self._save()
            raise
        self._state["revision"] = int(result.get("revision") or 0)
        self._state["last_sync"] = _now()
        self._state["last_error"] = None
        self._save()
        return {
            "pushed": {
                "rituals": len(vault.get("rituals") or []),
                "deck": len(vault.get("deck") or []),
            },
            "revision": self._state["revision"],
            "state": self.public_state(),
        }

    def force_push(self, app: Any) -> dict:
        vault = build_vault(app)
        result = self.client().force_push(vault)
        self._state["revision"] = int(result.get("revision") or 0)
        self._state["last_sync"] = _now()
        self._state["last_error"] = None
        self._save()
        return {
            "pushed": {
                "rituals": len(vault.get("rituals") or []),
                "deck": len(vault.get("deck") or []),
            },
            "revision": self._state["revision"],
            "state": self.public_state(),
        }

    def pull(self, app: Any, mode: str = "merge") -> dict:
        remote = self.client().pull()
        vault = remote.get("vault")
        if not isinstance(vault, dict) or not vault:
            raise SyncError("The sync server has no saved data yet — push first")
        if mode not in ("merge", "replace"):
            raise SyncError("mode must be 'merge' or 'replace'")
        report = apply_vault(app, vault, mode=mode)
        self._state["revision"] = int(remote.get("revision") or 0)
        self._state["last_sync"] = _now()
        self._state["last_error"] = None
        self._save()
        return {"applied": report, "revision": self._state["revision"],
                "state": self.public_state()}

    def peek(self) -> dict:
        """Describe the remote vault without applying it."""
        remote = self.client().pull()
        vault = remote.get("vault") if isinstance(remote.get("vault"), dict) else {}
        return {
            "revision": int(remote.get("revision") or 0),
            "updated_at": remote.get("updated_at"),
            "rituals": len(vault.get("rituals") or []),
            "deck": len(vault.get("deck") or []),
            "exported_at": vault.get("exported_at"),
        }

    def delete_remote(self, password: str) -> dict:
        self.client().delete_account(password)
        self._state = {}
        self._save()
        return self.public_state()
