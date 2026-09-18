"""Tunnel management: start, report, stop, and who is allowed to start it.

The tunnel is how a phone reaches the PC from anywhere with nothing installed on
the phone. Two properties matter more than the mechanics:

  * **The app never installs cloudflared by itself.** Downloading a binary from
    the internet and running it is not something a desktop app should do
    silently, so a missing binary is reported with the command to run.
  * **Only the PC can turn it on.** Publishing a machine to the internet is a
    decision for whoever is sitting at it; a LAN caller — or a client that is
    already inside the tunnel — must not be able to widen access further.

cloudflared is faked with a short script, so no test touches Cloudflare.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from pcrituals.api import create_app
from pcrituals.tunnel import Tunnel, TunnelError

# A stand-in that prints the same banner cloudflared does, then idles.
FAKE = """\
import sys, time
print("+--------------------------------------------------------------------+", flush=True)
print("|  Your quick Tunnel has been created! Visit it at:                  |", flush=True)
print("|  https://fake-words-abc123.trycloudflare.com                       |", flush=True)
print("+--------------------------------------------------------------------+", flush=True)
try:
    time.sleep(60)
except KeyboardInterrupt:
    pass
"""


@pytest.fixture
def fake_cloudflared(tmp_path, monkeypatch):
    script = tmp_path / "fake_cloudflared.py"
    script.write_text(FAKE)
    # The Tunnel shells out to the binary; run the script with this interpreter
    # instead so no real cloudflared is required.
    real = Tunnel.find_binary

    def launch(self, port, *, timeout=25.0):
        monkeypatch.setattr("pcrituals.tunnel.subprocess.Popen",
                            _popen_with(sys.executable, str(script)))
        return real.__get__(self)(port, timeout=timeout)

    return script


# Captured before any monkeypatching: the replacement itself calls Popen, so
# looking it up dynamically would recurse forever.
_REAL_POPEN = subprocess.Popen


def _popen_with(exe, script):
    def _popen(cmd, **kwargs):
        return _REAL_POPEN([exe, script], **kwargs)
    return _popen


# ---- discovery ------------------------------------------------------------

def test_missing_binary_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr("pcrituals.tunnel.shutil.which", lambda _n: None)
    monkeypatch.setattr("pcrituals.tunnel.os.path.exists", lambda _p: False)
    t = Tunnel()
    assert t.installed is False
    assert t.find_binary() is None
    assert t.running is False


def test_starting_without_cloudflared_explains_what_to_run(monkeypatch):
    """The app must not install it itself — it must say how."""
    monkeypatch.setattr("pcrituals.tunnel.shutil.which", lambda _n: None)
    monkeypatch.setattr("pcrituals.tunnel.os.path.exists", lambda _p: False)
    t = Tunnel()
    with pytest.raises(TunnelError) as err:
        t.start(8765)
    assert "cloudflared" in str(err.value)
    assert "winget" in str(err.value)


# ---- status ---------------------------------------------------------------

def test_status_always_carries_the_public_warning():
    """A public URL is reachable by anyone; the UI must never imply otherwise."""
    st = Tunnel().status()
    assert set(st) >= {"installed", "running", "url", "warning", "installed_hint"}
    assert "public" in st["warning"].lower()
    assert st["running"] is False


def test_status_is_off_when_idle():
    t = Tunnel()
    st = t.status()
    assert st["url"] == ""
    assert st["uptime"] == 0


# ---- lifecycle (fake binary) ---------------------------------------------

def test_start_reads_the_url_cloudflared_prints(monkeypatch):
    script = "/tmp/fake_cf.py"
    Path(script).write_text(FAKE)
    t = Tunnel()
    monkeypatch.setattr(t, "find_binary", lambda: sys.executable)
    monkeypatch.setattr("pcrituals.tunnel.subprocess.Popen",
                        _popen_with(sys.executable, script))
    try:
        st = t.start(8765, timeout=15)
        assert st["running"] is True
        assert st["url"] == "https://fake-words-abc123.trycloudflare.com"
        assert st["uptime"] >= 0
    finally:
        t.stop()


def test_stop_clears_the_url_and_reports_stopped(monkeypatch):
    script = "/tmp/fake_cf2.py"
    Path(script).write_text(FAKE)
    t = Tunnel()
    monkeypatch.setattr(t, "find_binary", lambda: sys.executable)
    monkeypatch.setattr("pcrituals.tunnel.subprocess.Popen",
                        _popen_with(sys.executable, script))
    t.start(8765, timeout=15)
    st = t.stop()
    assert st["running"] is False
    assert st["url"] == ""
    assert t.status()["running"] is False


def test_starting_twice_does_not_launch_a_second_tunnel(monkeypatch):
    script = "/tmp/fake_cf3.py"
    Path(script).write_text(FAKE)
    t = Tunnel()
    monkeypatch.setattr(t, "find_binary", lambda: sys.executable)
    monkeypatch.setattr("pcrituals.tunnel.subprocess.Popen",
                        _popen_with(sys.executable, script))
    try:
        first = t.start(8765, timeout=15)
        second = t.start(8765, timeout=15)
        assert second["url"] == first["url"]
    finally:
        t.stop()


# ---- only the PC may change this -----------------------------------------

@pytest.fixture
def client():
    app = create_app()
    token = app.state.pcrituals.security.register_local()
    return TestClient(app, client=("127.0.0.1", 51234)), token


def test_status_endpoint_needs_auth(client):
    """Loopback is the trusted desktop UI, so the 401 must be checked from
    somewhere else — a phone on the LAN."""
    c, _ = client
    lan = TestClient(c.app, client=("192.168.1.77", 51234))
    assert lan.get("/api/tunnel").status_code == 401


def test_status_endpoint_reports_shape(client):
    c, token = client
    body = c.get("/api/tunnel", headers={"Authorization": f"Bearer {token}"}).json()
    assert "installed" in body and "warning" in body


def test_lan_caller_cannot_publish_the_pc(client, monkeypatch):
    """A paired phone on the LAN must not be able to open the PC to the internet."""
    c, token = client
    remote = TestClient(c.app, client=("192.168.1.77", 51234))
    r = remote.post("/api/tunnel/start", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_tunnelled_caller_cannot_widen_access_further(client):
    """Someone already inside the tunnel must not be able to start another."""
    c, token = client
    tunnelled = TestClient(c.app, client=("127.0.0.1", 51234))
    tunnelled.headers.update({"CF-Connecting-IP": "203.0.113.9"})
    r = tunnelled.post("/api/tunnel/start", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_local_caller_gets_a_real_answer_not_a_crash(client, monkeypatch):
    """From the PC itself the call is allowed; with no cloudflared it explains."""
    c, token = client
    monkeypatch.setattr("pcrituals.tunnel.shutil.which", lambda _n: None)
    monkeypatch.setattr("pcrituals.tunnel.os.path.exists", lambda _p: False)
    r = c.post("/api/tunnel/start", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    assert "cloudflared" in r.json()["detail"]
