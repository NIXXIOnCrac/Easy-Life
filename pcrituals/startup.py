"""Start Easy Life automatically when the user logs in.

The installer offers a "run at login" checkbox, but that is a one-time decision
made during installation — a user who changes their mind later (or who installed
without it and now wants the phone to always reach the PC) has no way back
except editing the registry by hand.

This module gives the *app* control of the same registry value the installer
writes, so Settings and the installer are two doors onto one setting rather
than two competing mechanisms.

Windows only. Everything returns a clear, non-throwing answer elsewhere so the
UI can explain itself instead of silently doing nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Easy Life"
# Passed when Windows launches us at login: start the server, show no window.
# The whole point is that the PC is reachable from the phone without the user
# having to touch anything.
BACKGROUND_FLAG = "--background"


def is_supported() -> bool:
    return sys.platform.startswith("win")


def _winreg():
    """Import winreg lazily so this module imports anywhere."""
    import winreg  # noqa: WPS433
    return winreg


def launch_command() -> str:
    """The exact command Windows should run at login.

    Frozen (packaged) builds relaunch themselves. Running from source we point
    at desktop.py through pythonw.exe when it exists, because python.exe would
    flash a console window — the thing the user is trying to get rid of.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {BACKGROUND_FLAG}'

    script = Path(__file__).resolve().parent.parent / "desktop.py"
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    if pyw.exists():
        exe = pyw
    return f'"{exe}" "{script}" {BACKGROUND_FLAG}'


def status() -> dict:
    """What the UI needs to render the toggle honestly."""
    info: dict = {
        "supported": is_supported(),
        "enabled": False,
        "command": "",
        "value_name": VALUE_NAME,
    }
    if not is_supported():
        return info
    try:
        with _winreg().OpenKey(_winreg().HKEY_CURRENT_USER, RUN_KEY, 0,
                               _winreg().KEY_READ) as key:
            try:
                info["command"] = _winreg().QueryValueEx(key, VALUE_NAME)[0]
                info["enabled"] = bool(str(info["command"]).strip())
            except FileNotFoundError:
                pass
    except OSError:
        # A locked-down policy hive can make the key unreadable. Report "off"
        # rather than blowing up the settings screen.
        pass
    return info


def enable() -> dict:
    """Add the login entry. Idempotent."""
    if not is_supported():
        return {"ok": False, "reason": "not_windows", **status()}
    command = launch_command()
    try:
        with _winreg().CreateKeyEx(_winreg().HKEY_CURRENT_USER, RUN_KEY, 0,
                                   _winreg().KEY_SET_VALUE) as key:
            _winreg().SetValueEx(key, VALUE_NAME, 0, _winreg().REG_SZ, command)
    except OSError as e:
        return {"ok": False, "reason": str(e), **status()}
    return {"ok": True, "reason": "", **status()}


def disable() -> dict:
    """Remove the login entry. Safe to call when it was never there."""
    if not is_supported():
        return {"ok": False, "reason": "not_windows", **status()}
    try:
        with _winreg().OpenKey(_winreg().HKEY_CURRENT_USER, RUN_KEY, 0,
                               _winreg().KEY_SET_VALUE) as key:
            try:
                _winreg().DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass
    except OSError as e:
        return {"ok": False, "reason": str(e), **status()}
    return {"ok": True, "reason": "", **status()}


def set_enabled(on: bool) -> dict:
    return enable() if on else disable()
