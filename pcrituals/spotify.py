"""Spotify Web API integration (Authorization Code flow).

Gives *exact* now-playing data: real album art, precise progress/duration, and
true play/pause state — plus proper controls (play/pause/next/prev/shuffle/
repeat/seek/volume).

Setup (done by the user, locally — secrets never leave their machine):
  1. Create a free app at https://developer.spotify.com/dashboard
  2. Add redirect URI: http://127.0.0.1:<port>/callback
  3. Paste the Client ID + Client Secret into Settings → Spotify in the app.

Credentials + tokens are stored locally in <data>/spotify.json (chmod 600).
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

SCOPES = " ".join([
    "user-read-currently-playing",
    "user-read-playback-state",
    "user-modify-playback-state",
])


class SpotifyError(RuntimeError):
    pass


# The Web API reports repeat as a string; only the Web Playback SDK uses the
# integer `repeat_mode` field.
_REPEAT_BY_INDEX = {0: "off", 1: "context", 2: "track"}
_REPEAT_MODES = ("off", "context", "track")


def _as_bool(value) -> bool:
    """Coerce a JSON-ish value to bool.

    The API layer forwards the raw JSON `value`, so the string "false" (which
    is truthy in Python) must not be read as True.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _int_param(value, what: str) -> int:
    """Validate a numeric control parameter instead of silently using 0.

    `seek`/`volume` used `int(value or 0)`, so a missing value seeked to the
    start of the track and set the volume to mute.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        raise SpotifyError(f"spotify {what} needs a value")
    try:
        return int(float(value))
    except (TypeError, ValueError) as e:
        raise SpotifyError(f"spotify {what} needs a number, got {value!r}") from e


def _repeat_mode(state: dict) -> str:
    """Normalise the player's repeat state to off|context|track."""
    raw = state.get("repeat")
    if isinstance(raw, str) and raw.strip().lower() in _REPEAT_MODES:
        return raw.strip().lower()
    idx = state.get("repeat_mode")
    if idx is not None:
        try:
            return _REPEAT_BY_INDEX.get(int(idx), "off")
        except (TypeError, ValueError):
            return "off"
    return "off"



@dataclass
class SpotifyPlayback:
    title: str = ""
    artist: str = ""
    album: str = ""
    artwork: str = ""
    progress_ms: int = 0
    duration_ms: int = 0
    is_playing: bool = False
    shuffle: bool = False
    repeat: str = "off"      # off | context | track
    track_id: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "artwork": self.artwork or None,
            "position": round(self.progress_ms / 1000, 1),
            "duration": round(self.duration_ms / 1000, 1),
            "is_playing": self.is_playing,
            "shuffle": self.shuffle,
            "repeat": self.repeat,
            "track_id": self.track_id,
            "source": "spotify",
            "display": f"{self.artist} — {self.title}" if self.artist else self.title,
        }


class SpotifyClient:
    """Talks to Spotify on behalf of the user, if they've connected."""

    def __init__(self, config) -> None:
        self.config = config
        self._state = ""          # OAuth CSRF state (in-memory, per process)
        self._data = self._load()

    # ---- persistence -------------------------------------------------------
    @property
    def store_file(self) -> Path:
        return self.config.data_dir / "spotify.json"

    def _load(self) -> dict:
        try:
            return json.loads(self.store_file.read_text())
        except Exception:
            return {}

    def _save(self) -> None:
        self.store_file.write_text(json.dumps(self._data, indent=2))
        try:
            os.chmod(self.store_file, 0o600)
        except OSError:
            pass

    # ---- configuration / status -------------------------------------------
    @property
    def client_id(self) -> str:
        return self._data.get("client_id", "")

    @property
    def client_secret(self) -> str:
        return self._data.get("client_secret", "")

    @property
    def redirect_uri(self) -> str:
        return self._data.get("redirect_uri") or f"http://127.0.0.1:{self.config.port}/callback"

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def is_connected(self) -> bool:
        return bool(self._data.get("refresh_token"))

    def configure(self, client_id: str, client_secret: str,
                  redirect_uri: str | None = None) -> dict:
        self._data["client_id"] = (client_id or "").strip()
        if client_secret:
            self._data["client_secret"] = client_secret.strip()
        if redirect_uri:
            self._data["redirect_uri"] = redirect_uri.strip()
        self._save()
        return self.status()

    def disconnect(self) -> None:
        for k in ("access_token", "refresh_token", "expires_at", "user_name"):
            self._data.pop(k, None)
        self._save()

    def status(self) -> dict:
        return {
            "configured": self.is_configured(),
            "connected": self.is_connected(),
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "user": self._data.get("user_name", ""),
        }

    # ---- OAuth -------------------------------------------------------------
    def auth_url(self) -> str:
        if not self.is_configured():
            raise SpotifyError("Spotify Client ID/Secret not set")
        self._state = secrets.token_urlsafe(16)
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "state": self._state,
            "show_dialog": "false",
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def _basic_auth(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _post_token(self, form: dict) -> dict:
        data = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(
            TOKEN_URL, data=data, method="POST",
            headers={"Authorization": self._basic_auth(),
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise SpotifyError(f"token request failed ({e.code}): {detail}") from e

    def exchange_code(self, code: str, state: str = "") -> dict:
        if state and self._state and state != self._state:
            raise SpotifyError("OAuth state mismatch")
        tok = self._post_token({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        })
        self._store_tokens(tok)
        # Fetch the profile name for display.
        try:
            me = self._get("/me")
            self._data["user_name"] = me.get("display_name") or me.get("id", "")
            self._save()
        except Exception:
            pass
        return self.status()

    def _store_tokens(self, tok: dict) -> None:
        self._data["access_token"] = tok.get("access_token", "")
        if tok.get("refresh_token"):
            self._data["refresh_token"] = tok["refresh_token"]
        self._data["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 30
        self._save()

    def _access_token(self) -> str:
        if not self.is_connected():
            raise SpotifyError("Spotify not connected")
        if time.time() >= float(self._data.get("expires_at", 0)):
            tok = self._post_token({
                "grant_type": "refresh_token",
                "refresh_token": self._data["refresh_token"],
            })
            self._store_tokens(tok)
        return self._data["access_token"]

    # ---- API calls ---------------------------------------------------------
    def _get(self, path: str, timeout: float = 10.0):
        req = urllib.request.Request(API_BASE + path,
                                     headers={"Authorization": f"Bearer {self._access_token()}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            if e.code == 204:      # nothing playing
                return None
            raise SpotifyError(f"GET {path} failed ({e.code})") from e

    def _put(self, path: str, timeout: float = 10.0):
        req = urllib.request.Request(API_BASE + path, method="PUT",
                                     headers={"Authorization": f"Bearer {self._access_token()}",
                                              "Content-Length": "0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if e.code in (204, 202):
                return e.code
            raise SpotifyError(f"PUT {path} failed ({e.code})") from e

    def now_playing(self) -> Optional[SpotifyPlayback]:
        """Exact now-playing from Spotify (real art + progress). None if idle."""
        data = self._get("/me/player/currently-playing")
        if not data or not data.get("item"):
            return None
        item = data["item"]
        artists = ", ".join(a.get("name", "") for a in (item.get("artists") or []))
        images = (item.get("album") or {}).get("images") or []
        art = images[0].get("url") if images else ""
        return SpotifyPlayback(
            title=item.get("name", ""),
            artist=artists,
            album=(item.get("album") or {}).get("name", ""),
            artwork=art,
            progress_ms=int(data.get("progress_ms") or 0),
            duration_ms=int(item.get("duration_ms") or 0),
            is_playing=bool(data.get("is_playing")),
            track_id=item.get("id", ""),
        )

    def playback_state(self) -> dict:
        st = self._get("/me/player")
        if not st:
            return {}
        return {"shuffle": bool(st.get("shuffle_state")),
                "repeat": _repeat_mode(st),
                "device": ((st.get("device") or {}).get("name") or ""),
                "volume": (st.get("device") or {}).get("volume_percent")}

    def control(self, action: str, value=None) -> None:
        """Send a playback command to Spotify."""
        if action in ("play_pause", "toggle"):
            st = self._get("/me/player")
            if st and not st.get("is_playing"):
                self._put("/me/player/play")
            else:
                self._put("/me/player/pause")
        elif action == "play":
            self._put("/me/player/play")
        elif action == "pause":
            self._put("/me/player/pause")
        elif action == "next":
            self._put("/me/player/next")
        elif action in ("previous", "prev"):
            self._put("/me/player/previous")
        elif action == "shuffle":
            # Parse the value: "false" arrives as a truthy string from JSON.
            state = "true" if _as_bool(value) else "false"
            self._put(f"/me/player/shuffle?state={state}")
        elif action == "repeat":
            mode = str(value or "off").strip().lower()
            if mode not in _REPEAT_MODES:
                raise SpotifyError(f"repeat mode must be one of {'/'.join(_REPEAT_MODES)}")
            self._put(f"/me/player/repeat?state={urllib.parse.quote(mode)}")
        elif action == "seek":
            position = _int_param(value, "seek")
            if position < 0:
                raise SpotifyError("seek position cannot be negative")
            self._put(f"/me/player/seek?position_ms={position}")
        elif action == "volume":
            percent = _int_param(value, "volume")
            if not 0 <= percent <= 100:
                raise SpotifyError("volume must be between 0 and 100")
            self._put(f"/me/player/volume?volume_percent={percent}")
        else:
            raise SpotifyError(f"unsupported spotify action: {action}")

    def search_art(self, artist: str, title: str) -> Optional[str]:
        """Look up cover art via Spotify search (exact-ish, catalog based)."""
        q = urllib.parse.quote(f"track:{title} artist:{artist}")
        data = self._get(f"/search?q={q}&type=track&limit=1")
        try:
            items = data["tracks"]["items"]
            if items:
                imgs = items[0]["album"].get("images") or []
                return imgs[0]["url"] if imgs else None
        except Exception:
            return None
        return None