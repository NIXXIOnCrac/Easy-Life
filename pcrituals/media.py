"""Media control + now-playing.

Two capabilities:

1. Transport control (play/pause, next, previous, stop) — done with the
   Windows *media keys*, which work for whatever is playing: Spotify, a
   browser tab, VLC, etc. No API keys or OAuth needed.

2. Now-playing info — best effort. On Windows we read the Spotify process
   window title (Spotify publishes "Artist - Song" there), and optionally the
   OS media session. If nothing is available we return None and the UI hides
   the player gracefully.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

# Windows virtual-key codes for media keys.
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002

# Album art lookup — free, no API key.
# Deezer's search matches far better than iTunes for ambiguous titles, so it's
# tried first. Every result is *validated* against artist + title; if nothing
# matches confidently we return None (better no art than the wrong cover).
DEEZER_SEARCH = "https://api.deezer.com/search"
ITUNES_SEARCH = "https://itunes.apple.com/search"
_art_cache: dict[str, Optional[str]] = {}
# The app runs for days; cap the memo so it cannot grow without bound.
_ART_CACHE_MAX = 256


def _cache_art(key: str, value: Optional[str]) -> None:
    if len(_art_cache) >= _ART_CACHE_MAX:
        _art_cache.clear()
    _art_cache[key] = value


def _press_media_key(vk: int) -> None:
    """Send one media-key press through the Windows keybd_event API.

    Kept as a module-level function so the key mapping can be verified on any
    host (the ctypes calls themselves only work on Windows). Explicit argtypes
    are set because keybd_event's last parameter is ULONG_PTR: without a
    prototype ctypes passes a C int and a 64-bit value would be truncated.
    """
    import ctypes

    user32 = ctypes.windll.user32
    try:
        user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte,
                                       ctypes.c_ulong, ctypes.c_void_p]
        user32.keybd_event.restype = None
    except (AttributeError, TypeError):
        pass
    user32.keybd_event(vk, 0, 0, None)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, None)



def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _artist_matches(want: str, got: str) -> bool:
    w, g = _norm(want), _norm(got)
    if not w or not g:
        return False
    return w == g or w in g or g in w


def _title_matches(want: str, got: str) -> bool:
    w, g = _norm(want), _norm(got)
    if not w or not g:
        return False
    # Exact, or the candidate has extra text (e.g. "Song (Remastered)").
    return w == g or w in g


def _deezer_art(artist: str, title: str, timeout: float) -> Optional[str]:
    params = urllib.parse.urlencode({"q": f"{artist} {title}".strip(), "limit": 5})
    url = f"{DEEZER_SEARCH}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "pcrituals/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    for d in data.get("data") or []:
        a = (d.get("artist") or {}).get("name", "")
        t = d.get("title", "")
        if _artist_matches(artist, a) and _title_matches(title, t):
            alb = d.get("album") or {}
            return alb.get("cover_xl") or alb.get("cover_big") or alb.get("cover_medium")
    return None


def _itunes_art(artist: str, title: str, timeout: float) -> Optional[str]:
    params = urllib.parse.urlencode({"term": f"{artist} {title}".strip(),
                                     "media": "music", "entity": "song", "limit": 5})
    url = f"{ITUNES_SEARCH}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "pcrituals/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    for x in data.get("results") or []:
        a = x.get("artistName", "")
        t = x.get("trackName", "")
        if _artist_matches(artist, a) and _title_matches(title, t):
            raw = x.get("artworkUrl100") or x.get("artworkUrl60") or ""
            if raw:
                return re.sub(r"/\d+x\d+bb\.", "/600x600bb.", raw)
    return None


def lookup_album_art(artist: str, title: str, timeout: float = 4.0) -> Optional[str]:
    """Find album artwork for a track. Returns a high-res image URL or None.

    No API key required. Tries Deezer (best matching), then iTunes, validating
    artist + title so we never show the wrong cover. Results are cached and the
    function never raises.
    """
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not title:
        return None
    key = f"{artist}|{title}".lower()
    if key in _art_cache:
        return _art_cache[key]

    art: Optional[str] = None
    for source in (_deezer_art, _itunes_art):
        try:
            art = source(artist, title, timeout)
        except Exception:
            art = None
        if art:
            break
    _cache_art(key, art)
    return art


@dataclass
class NowPlaying:
    title: str = ""
    artist: str = ""
    source: str = ""       # e.g. "spotify"
    playing: bool = True
    artwork: str = ""      # album cover image URL (may be empty)

    def to_dict(self) -> dict:
        return {"title": self.title, "artist": self.artist,
                "source": self.source, "playing": self.playing,
                "artwork": self.artwork or None,
                "display": self._display()}

    def _display(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} — {self.title}"
        return self.title or self.artist or ""


class MediaController:
    """Cross-platform media control with a Windows implementation."""

    def __init__(self) -> None:
        self.is_windows = sys.platform == "win32"

    # ---- transport -------------------------------------------------------
    def _press(self, vk: int) -> None:
        if not self.is_windows:
            return
        _press_media_key(vk)

    def play_pause(self) -> None:
        self._press(VK_MEDIA_PLAY_PAUSE)

    def next(self) -> None:
        self._press(VK_MEDIA_NEXT_TRACK)

    def previous(self) -> None:
        self._press(VK_MEDIA_PREV_TRACK)

    def stop(self) -> None:
        self._press(VK_MEDIA_STOP)

    def control(self, action: str) -> None:
        map_ = {
            "play_pause": self.play_pause,
            "toggle": self.play_pause,
            "next": self.next,
            "previous": self.previous,
            "prev": self.previous,
            "stop": self.stop,
        }
        fn = map_.get(action)
        if fn is None:
            raise ValueError(f"unsupported media action: {action}")
        fn()

    # ---- now playing -----------------------------------------------------
    def now_playing(self) -> Optional[NowPlaying]:
        """Return what's playing, or None if we can't tell."""
        if not self.is_windows:
            return None
        # 1) Spotify window title (most reliable for Spotify on Windows).
        np = self._spotify_from_window_title()
        if np:
            return np
        return None

    def _spotify_from_window_title(self) -> Optional[NowPlaying]:
        """Read Spotify's main window title, e.g. 'Artist - Song'."""
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process -Name Spotify -ErrorAction SilentlyContinue | "
                 "Where-Object {$_.MainWindowTitle} | "
                 "Select-Object -First 1 -ExpandProperty MainWindowTitle"],
                capture_output=True, text=True, errors="replace", timeout=6).stdout.strip()
        except Exception:
            return None
        if not out:
            return None
        # Spotify shows "Artist - Song" (sometimes with extra suffixes).
        title = out
        title = re.sub(r"\s*[-–]\s*Spotify.*$", "", title, flags=re.IGNORECASE)
        if title.lower() in ("spotify", "spotify premium", "spotify free"):
            return None
        # Split on the central dash into artist / song when possible.
        artist, song = "", title
        m = re.split(r"\s+[-–]\s+", title, maxsplit=1)
        if len(m) == 2:
            artist, song = m[0].strip(), m[1].strip()
        np = NowPlaying(title=song, artist=artist, source="spotify", playing=True)
        # Attach album artwork (best-effort, cached, never raises).
        try:
            np.artwork = lookup_album_art(artist, song) or ""
        except Exception:
            np.artwork = ""
        return np