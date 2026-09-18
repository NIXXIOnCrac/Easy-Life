"""Client for the Easy Life sync server.

Why stdlib only
---------------
The desktop app ships inside a PyInstaller bundle where every dependency costs
size and a new CVE. Sync is a handful of JSON calls, so urllib is enough; the
client is the one place that must not add a dependency to the packaged app.

Error mapping is the other reason this module is thin and explicit: the UI needs
to tell "wrong password" from "your other PC pushed changes" from "the server is
down", and nothing here may ever put a token into an exception message that gets
logged or shown to the user.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 15.0
PREFIX = "/sync"


class SyncError(RuntimeError):
    """Any failure talking to the sync server."""


class SyncConflict(SyncError):
    """The vault changed on the server since we last pulled it."""

    def __init__(self, message: str = "vault changed on the server",
                 current_revision: int | None = None) -> None:
        super().__init__(message)
        # The revision the server actually holds, when it told us.
        self.current_revision = current_revision


class SyncClient:
    """Talks to a Easy Life sync server over HTTP(S)."""

    def __init__(self, base_url: str, token: str = "", timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = self.normalize_url(base_url)
        self.token = token
        self.timeout = timeout

    @staticmethod
    def normalize_url(url: str) -> str:
        """Accept "host", "host:8788" or a full URL.

        Defaults to https:// because a sync server normally lives on the
        internet; self-hosters on a LAN can write http:// explicitly.
        """
        value = (url or "").strip()
        if not value:
            raise SyncError("no sync server URL configured")
        if "://" not in value:
            value = "https://" + value
        return value.rstrip("/")

    # ---- account -----------------------------------------------------------
    def register(self, username: str, password: str) -> dict:
        result = self._request("POST", f"{PREFIX}/register",
                               {"username": username, "password": password}, auth=False)
        self.token = str(result.get("token") or "")
        if not self.token:
            raise SyncError("sync server did not return a token")
        return result

    def login(self, username: str, password: str) -> dict:
        result = self._request("POST", f"{PREFIX}/login",
                               {"username": username, "password": password}, auth=False)
        self.token = str(result.get("token") or "")
        if not self.token:
            raise SyncError("sync server did not return a token")
        return result

    def me(self) -> dict:
        return self._request("GET", f"{PREFIX}/me")

    def delete_account(self, password: str) -> dict:
        result = self._request("DELETE", f"{PREFIX}/account", {"password": password})
        # The token is dead server-side now; forget it locally too.
        self.token = ""
        return result

    # ---- vault -------------------------------------------------------------
    def pull(self) -> dict:
        """Return {"vault", "revision", "updated_at"}."""
        return self._request("GET", f"{PREFIX}/vault")

    def push(self, vault: dict, base_revision: int) -> dict:
        """Store a vault; raises SyncConflict if base_revision is stale."""
        return self._request("PUT", f"{PREFIX}/vault",
                             {"vault": vault, "base_revision": int(base_revision)})

    def force_push(self, vault: dict) -> dict:
        """Overwrite the server's vault regardless of revision."""
        return self._request("PUT", f"{PREFIX}/vault/force", {"vault": vault})

    # ---- transport ---------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None,
                 auth: bool = True) -> dict:
        url = self.base_url + path
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if auth and self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return _decode(response.read())
        except urllib.error.HTTPError as error:
            raise self._translate(error) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            # Never echo the URL's query string or any header into the message.
            raise SyncError("could not reach the sync server") from None

    @staticmethod
    def _translate(error: urllib.error.HTTPError) -> SyncError:
        detail = _error_detail(error)
        if error.code == 401:
            return SyncError("sync server rejected the credentials")
        if error.code == 409:
            return SyncConflict(detail.get("detail") or "vault changed on the server",
                                current_revision=detail.get("current_revision"))
        if error.code == 429:
            return SyncError("too many attempts, wait a few minutes")
        if error.code == 413:
            return SyncError("vault is too large for the sync server")
        message = detail.get("detail") or "request failed"
        return SyncError(f"sync server error ({error.code}): {message}")


def _decode(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SyncError("sync server returned an unexpected response") from None
    if not isinstance(parsed, dict):
        raise SyncError("sync server returned an unexpected response")
    return parsed


def _error_detail(error: urllib.error.HTTPError) -> dict:
    """Best-effort parse of the server's {"detail": ...} error body."""
    try:
        body = json.loads(error.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - a body we cannot read is not fatal
        return {}
    return body if isinstance(body, dict) else {}


__all__ = ["SyncClient", "SyncError", "SyncConflict"]
