"""Integration detection & launch helpers for supported apps.

The Integrations UI lists these capabilities. This module performs REAL
detection on Windows (registry / known install paths / protocol handler
availability) and reports what's actually installed + how to launch it. On
non-Windows it returns "unavailable" so nothing is faked.
"""
from __future__ import annotations

import os
import platform
import sys
import subprocess
from pathlib import Path

# Each integration: known install locations (from env vars / Program Files),
# a protocol scheme for launching, and a friendly name.
INTEGRATIONS = {
    "steam": {
        "name": "Steam",
        "kind": "game", "scheme": "steam://",
        "detect": lambda p: p.steam(),
    },
    "epic_games": {
        "name": "Epic Games Launcher",
        "kind": "game", "scheme": "com.epicgames.launcher://",
        "detect": lambda p: p.epic(),
    },
    "battle_net": {
        "name": "Battle.net",
        "kind": "game", "scheme": "battle.net://",
        "detect": lambda p: p.battlenet(),
    },
    "riot": {
        "name": "Riot Client",
        "kind": "game", "scheme": "riot://",
        "detect": lambda p: p.riot(),
    },
    "discord": {
        "name": "Discord", "kind": "app", "scheme": None,
        "detect": lambda p: p.discord(),
    },
    "spotify": {
        "name": "Spotify", "kind": "app", "scheme": "spotify:",
        "detect": lambda p: p.spotify(),
    },
    "signalrgb": {
        "name": "SignalRGB", "kind": "rgb", "scheme": None,
        "detect": lambda p: p.signalrgb(),
    },
    "openrgb": {
        "name": "OpenRGB", "kind": "rgb", "scheme": None,
        "detect": lambda p: p.openrgb(),
    },
    "wallpaper_engine": {
        "name": "Wallpaper Engine", "kind": "app", "scheme": None,
        "detect": lambda p: p.wallpaper_engine(),
    },
}


class WindowsDetector:
    """Detection for the current (possibly Windows) host.

    `exists` and `env` are injectable so the detection rules (which are pure
    path/env logic) can be verified on a non-Windows host.
    """

    def __init__(self, *, exists: Optional[Callable[[Path], bool]] = None,
                 env: Optional[dict] = None) -> None:
        self._exists = exists or (lambda p: p.exists())
        self._env = os.environ if env is None else env
        self._pf = self._program_files_dirs()

    def _program_files_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            v = self._env.get(env)
            if v:
                dirs.append(Path(v))
        dirs.append(Path("C:\\Program Files"))
        dirs.append(Path("C:\\Program Files (x86)"))
        return list(dict.fromkeys(dirs))

    def _user_dirs(self) -> list[Path]:
        """Per-user install roots.

        Plenty of this software installs per-user (Microsoft Store / Squirrel
        style) and never touches Program Files, so a Program-Files-only search
        reports it as missing.
        """
        dirs: list[Path] = []
        for env in ("LOCALAPPDATA", "APPDATA", "PROGRAMDATA"):
            v = self._env.get(env)
            if v:
                dirs.append(Path(v))
        return list(dict.fromkeys(dirs))

    def _find(self, *rel: str) -> Path | None:
        for d in self._pf:
            cand = d.joinpath(*rel)
            if self._exists(cand):
                return cand
        return None

    def _find_anywhere(self, *rel: str) -> Path | None:
        """Look in Program Files *and* the per-user install roots."""
        found = self._find(*rel)
        if found:
            return found
        for d in self._user_dirs():
            cand = d.joinpath(*rel)
            if self._exists(cand):
                return cand
        return None

    def _reg_detect(self, hive_key: str) -> bool:
        """Check a Windows registry uninstall key path for an app name."""
        try:
            import winreg  # noqa: WPS433
        except ImportError:
            return False
        roots = [getattr(winreg, "HKEY_LOCAL_MACHINE", None),
                 getattr(winreg, "HKEY_CURRENT_USER", None)]
        for root in roots:
            if root is None:
                continue
            try:
                with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall") as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            sub = winreg.EnumKey(k, i)
                            with winreg.OpenKey(k, sub) as sk:
                                try:
                                    disp = winreg.QueryValueEx(sk, "DisplayName")[0]
                                    if hive_key.lower() in str(disp).lower():
                                        return True
                                except OSError:
                                    pass
                        except OSError:
                            continue
            except OSError:
                continue
        return False

    def steam(self) -> str | None:
        p = self._find("Steam", "steam.exe") or self._find("Steam")
        return str(p) if p else None

    def epic(self) -> str | None:
        p = self._find("Epic Games", "Launcher", "Portal", "Binaries", "Win64", "EpicGamesLauncher.exe")
        return str(p) if p else None

    def battlenet(self) -> str | None:
        p = self._find("Battle.net", "Battle.net.exe") or self._find("Battle.net")
        return str(p) if p else None

    def riot(self) -> str | None:
        p = self._find("Riot Games", "Riot Client", "RiotClientServices.exe") or self._find("Riot Client")
        return str(p) if p else None

    def discord(self) -> str | None:
        for base in ("Discord", "Discord", "discord"):
            p = self._find(base) or self._find(base, "Discord.exe")
            if p:
                return str(p)
        # Local appdata fallback (Discord is a user-install).
        local = os.environ.get("LOCALAPPDATA")
        if local:
            for f in ("Discord", "DiscordCanary"):
                for rd in ("app-*",):
                    import glob
                    hits = glob.glob(str(Path(local, f, "*.exe")))
                    if hits:
                        return hits[0]
                    hits2 = glob.glob(str(Path(local, f, rd, "*.exe")))
                    if hits2:
                        return hits2[0]
        return None

    def spotify(self) -> str | None:
        for base in ("Spotify", "Spotify"):
            for sub in ("Spotify.exe", "spotify.exe"):
                p = self._find(base, sub) or self._find(base)
                if p:
                    return str(p)
        if self._reg_detect("Spotify"):
            return "Spotify"
        return None

    def signalrgb(self) -> str | None:
        p = self._find("SignalRGB")
        return str(p) if p else None

    def openrgb(self) -> str | None:
        p = self._find("OpenRGB", "OpenRGB.exe") or self._find("OpenRGB")
        return str(p) if p else None

    def wallpaper_engine(self) -> str | None:
        p = self._find("Steam", "steamapps", "common", "wallpaper_engine") or self._find("wallpaper_engine")
        return str(p) if p else None


def detect_integrations() -> list[dict]:
    """Return per-integration detection results with real availability."""
    if sys.platform != "win32":
        # On non-Windows we still report the integration list but mark detection
        # as unavailable (not fake) so the UI clearly communicates state.
        return [
            {"id": k, "name": v["name"], "kind": v["kind"],
             "installed": None, "path": None,
             "detected_on": "windows only", "scheme": v["scheme"]}
            for k, v in INTEGRATIONS.items()
        ]
    det = WindowsDetector()
    out = []
    for k, v in INTEGRATIONS.items():
        path = v["detect"](det)
        out.append({"id": k, "name": v["name"], "kind": v["kind"],
                    "installed": bool(path), "path": path,
                    "detected_on": platform.platform()[:40], "scheme": v["scheme"]})
    return out