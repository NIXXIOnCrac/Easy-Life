"""Start-at-login, background running, and the Quit endpoint.

The "normal app" behaviour depends on three things that are easy to get wrong
and invisible until a real Windows user hits them:

  * the login entry must point at something that shows NO window (the user's
    whole complaint is a console/script flashing up), and it must be the same
    value the installer writes, so the two cannot disagree;
  * starting the app twice must open a window rather than crash on the port;
  * closing the window must not kill the server the phone is paired to, which
    means there has to be an explicit Quit — and it must be loopback-only, so a
    paired phone cannot shut the PC's app down.

Windows is faked here (a dict-backed winreg), so these run anywhere.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from pcrituals import startup
from pcrituals.api import create_app


# --------------------------------------------------------------------------
# A minimal stand-in for winreg: just enough of the module surface that
# startup.py exercises, with the real error semantics (missing value -> the
# same exception winreg raises).
# --------------------------------------------------------------------------
class _Key:
    def __init__(self, store, path):
        self._store, self._path = store, path

    def __enter__(self):
        self._store.setdefault(self._path, {})
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 1

    def __init__(self):
        self.store = {}

    def OpenKey(self, root, path, reserved=0, access=0):
        if path not in self.store and access == self.KEY_READ:
            raise FileNotFoundError(path)
        return _Key(self.store, path)

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        return _Key(self.store, path)

    def QueryValueEx(self, key, name):
        try:
            return (self.store[key._path][name], self.REG_SZ)
        except KeyError:
            raise FileNotFoundError(name)

    def SetValueEx(self, key, name, reserved, kind, value):
        self.store.setdefault(key._path, {})[name] = value

    def DeleteValue(self, key, name):
        try:
            del self.store[key._path][name]
        except KeyError:
            raise FileNotFoundError(name)


@pytest.fixture
def fake_windows(monkeypatch):
    reg = FakeWinreg()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", reg)
    return reg


# ---- the login entry -----------------------------------------------------

def test_not_supported_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    st = startup.status()
    assert st["supported"] is False
    assert st["enabled"] is False
    # And the mutators report why instead of raising.
    assert startup.enable()["ok"] is False
    assert startup.disable()["ok"] is False


def test_disabled_by_default_then_enables_and_disables(fake_windows):
    assert startup.status()["enabled"] is False

    result = startup.enable()
    assert result["ok"] is True
    assert result["enabled"] is True
    # The installer writes this same value name, so it must match.
    assert fake_windows.store[startup.RUN_KEY][startup.VALUE_NAME] == startup.launch_command()

    assert startup.disable()["ok"] is True
    assert startup.status()["enabled"] is False


def test_disable_is_safe_when_never_enabled(fake_windows):
    assert startup.disable()["ok"] is True
    assert startup.status()["enabled"] is False


def test_enable_is_idempotent(fake_windows):
    startup.enable()
    startup.enable()
    assert len(fake_windows.store[startup.RUN_KEY]) == 1


def test_status_reads_what_the_installer_would_have_written(fake_windows):
    """A user who ticked 'run at login' during install must see the toggle ON."""
    fake_windows.store[startup.RUN_KEY] = {startup.VALUE_NAME: r'"C:\Apps\Easy Life\Easy Life.exe"'}
    st = startup.status()
    assert st["enabled"] is True
    assert "Easy Life.exe" in st["command"]


def test_login_entry_shows_no_window(fake_windows):
    """The user's complaint is a console/script appearing at startup, so the
    command must be the background entry point, never a .bat."""
    cmd = startup.launch_command()
    assert startup.BACKGROUND_FLAG in cmd
    assert ".bat" not in cmd.lower()


def test_login_entry_quotes_paths_with_spaces(monkeypatch):
    r"""An install path like "C:\Program Files\Easy Life\..." must survive."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable",
                        r"C:\Program Files\Easy Life\Easy Life.exe")
    cmd = startup.launch_command()
    assert cmd == ('"C:\\Program Files\\Easy Life\\Easy Life.exe" '
                   f'{startup.BACKGROUND_FLAG}'), cmd


# ---- the Quit endpoint ---------------------------------------------------

@pytest.fixture
def client():
    app = create_app()
    token = app.state.pcrituals.security.register_local()
    return TestClient(app), token


def test_quit_reports_501_when_there_is_no_desktop_shell(client):
    c, token = client
    r = c.post("/api/quit", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 501


def test_quit_calls_the_hook(client):
    c, token = client
    called = []
    c.app.state.pcrituals_request_quit = lambda: called.append(True) or True
    r = c.post("/api/quit", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert called == [True]


def test_quit_requires_auth(client):
    c, _ = client
    assert c.post("/api/quit").status_code == 401


def test_quit_is_refused_from_another_machine(client):
    """A paired phone must not be able to shut the PC's app down."""
    c, token = client
    c.app.state.pcrituals_request_quit = lambda: True
    # Same app, but the request appears to arrive from a LAN address instead of
    # loopback — exactly what a paired phone looks like.
    remote = TestClient(c.app, client=("192.168.1.77", 51234))
    r = remote.post("/api/quit", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


# ---- settings surface ----------------------------------------------------

def test_settings_expose_the_login_toggle(client, monkeypatch):
    c, token = client
    monkeypatch.setattr(sys, "platform", "linux")
    body = c.get("/api/settings", headers={"Authorization": f"Bearer {token}"}).json()
    assert "run_at_login" in body
    assert body["run_at_login"]["supported"] is False


def test_settings_toggle_drives_the_registry(client, fake_windows):
    c, token = client
    h = {"Authorization": f"Bearer {token}"}
    r = c.post("/api/settings", json={"run_at_login": True}, headers=h)
    assert r.status_code == 200
    assert startup.status()["enabled"] is True

    r = c.post("/api/settings", json={"run_at_login": False}, headers=h)
    assert r.status_code == 200
    assert startup.status()["enabled"] is False


def test_login_toggle_is_not_copied_into_settings_json(client, fake_windows):
    """Windows owns this setting. Persisting a copy would create a second answer
    that silently drifts from the registry."""
    c, token = client
    c.post("/api/settings", json={"run_at_login": True},
           headers={"Authorization": f"Bearer {token}"})
    settings_file = c.app.state.pcrituals.config.settings_file
    if settings_file.exists():
        assert "run_at_login" not in settings_file.read_text()
