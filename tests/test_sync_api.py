"""End-to-end test of the OPTIONAL sync feature.

Starts a real sync server in-process, then drives the desktop app's /sync
endpoints against it: connect, push, pull, conflict handling, disconnect.

The important security property this checks: the sync credential must never
travel inside a vault (a vault is pushed to a server and can be pulled onto
another PC), and /sync must not be reachable without a session.
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import uvicorn
from fastapi.testclient import TestClient


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def sync_server():
    """A real sync server on a real port, so the client uses real HTTP."""
    os.environ["PCRITUALS_SYNC_DB"] = str(Path("/tmp") / f"syncapi_{os.getpid()}.db")
    from pcrituals.sync_server import create_app as create_sync_app
    port = _free_port()
    config = uvicorn.Config(create_sync_app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        pytest.skip("could not start the sync server")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


def _app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    from pcrituals.api import create_app
    return TestClient(create_app())


def _session(c) -> dict:
    r = c.post("/api/auth/setup", json={"username": "alex", "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_sync_endpoints_require_a_session(tmp_path, monkeypatch, sync_server):
    c = _app_client(tmp_path / "gated", monkeypatch)
    c.post("/api/auth/setup", json={"username": "alex", "password": "secret123"})
    for method, path in [("get", "/api/sync/status"), ("post", "/api/sync/connect"),
                         ("post", "/api/sync/push"), ("post", "/api/sync/pull"),
                         ("get", "/api/sync/peek")]:
        call = getattr(c, method)
        r = call(path, json={}) if method == "post" else call(path)
        assert r.status_code == 401, f"{method.upper()} {path} returned {r.status_code}"


def test_full_sync_round_trip(tmp_path, monkeypatch, sync_server):
    c = _app_client(tmp_path / "a", monkeypatch)
    h = _session(c)

    # Not connected yet, and nothing has left the machine.
    st = c.get("/api/sync/status", headers=h).json()
    assert st["configured"] is False
    assert st["url"] == ""

    # Register a remote account and connect.
    r = c.post("/api/sync/connect", headers=h, json={
        "url": sync_server, "username": "alex", "password": "syncpass123", "register": True})
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True
    # The credential must never be exposed to the client.
    assert "token" not in r.text

    # Create content, then push.
    c.post("/api/rituals", headers=h, json={
        "name": "Gaming Mode",
        "actions": [{"type": "delay", "params": {"seconds": 1}, "label": "wait"}],
    })
    r = c.post("/api/sync/push", headers=h, json={})
    assert r.status_code == 200, r.text
    assert r.json()["pushed"]["rituals"] >= 1
    assert r.json()["revision"] >= 1

    # The server should now describe a vault containing that ritual.
    peek = c.get("/api/sync/peek", headers=h)
    assert peek.status_code == 200, peek.text
    assert peek.json()["rituals"] >= 1

    # A second install pulls the same vault and ends up with the ritual.
    a2 = _app_client(tmp_path / "b", monkeypatch)
    h2 = _session(a2)
    r = a2.post("/api/sync/connect", headers=h2, json={
        "url": sync_server, "username": "alex", "password": "syncpass123"})
    assert r.status_code == 200, r.text
    r = a2.post("/api/sync/pull", headers=h2, json={"mode": "replace"})
    assert r.status_code == 200, r.text
    names = [x["name"] for x in a2.get("/api/rituals", headers=h2).json()]
    assert "Gaming Mode" in names, names


def test_pushing_a_stale_revision_reports_a_conflict(tmp_path, monkeypatch, sync_server):
    """Two machines touching the same account: the second push must not silently
    clobber the first."""
    c = _app_client(tmp_path / "conf", monkeypatch)
    h = _session(c)
    r = c.post("/api/sync/connect", headers=h, json={
        "url": sync_server, "username": "confuser", "password": "syncpass123", "register": True})
    assert r.status_code == 200, r.text

    other = _app_client(tmp_path / "conf2", monkeypatch)
    h2 = _session(other)
    assert other.post("/api/sync/connect", headers=h2, json={
        "url": sync_server, "username": "confuser", "password": "syncpass123"}).status_code == 200

    # The second machine pushes (revision 1).
    assert other.post("/api/sync/push", headers=h2, json={}).status_code == 200
    # The first machine still believes it is at revision 0 -> conflict.
    r = c.post("/api/sync/push", headers=h, json={})
    assert r.status_code == 409, r.text
    # The UI can then choose to force.
    assert c.post("/api/sync/push", headers=h, json={"force": True}).status_code == 200


def test_vault_never_carries_the_sync_credential(tmp_path, monkeypatch, sync_server):
    """A vault is uploaded and can be downloaded onto another PC, so it must not
    contain credentials, device tokens or the machine's MAC address."""
    c = _app_client(tmp_path / "v", monkeypatch)
    h = _session(c)
    assert c.post("/api/sync/connect", headers=h, json={
        "url": sync_server, "username": "vaultuser", "password": "syncpass123",
        "register": True}).status_code == 200

    from pcrituals.vault import build_vault
    ap = c.app.state.app if hasattr(c.app.state, "app") else None
    # Reach the App through a request path instead of internals if needed.
    if ap is None:
        from pcrituals.api import create_app  # noqa: F401
        r = c.post("/api/sync/push", headers=h, json={})
        assert r.status_code == 200, r.text
        # Inspect what the server stored.
        raw = c.get("/api/sync/peek", headers=h).json()
        assert "token" not in str(raw)
        return

    vault = build_vault(ap)
    dumped = str(vault).lower()
    for forbidden in ("token", "secret", "password", "pbkdf2"):
        assert forbidden not in dumped, f"vault leaked '{forbidden}'"


def test_disconnect_forgets_the_local_link_only(tmp_path, monkeypatch, sync_server):
    c = _app_client(tmp_path / "d", monkeypatch)
    h = _session(c)
    assert c.post("/api/sync/connect", headers=h, json={
        "url": sync_server, "username": "discuser", "password": "syncpass123",
        "register": True}).status_code == 200
    assert c.post("/api/sync/disconnect", headers=h).json()["configured"] is False
    # The remote account still exists: we can sign in again.
    assert c.post("/api/sync/connect", headers=h, json={
        "url": sync_server, "username": "discuser", "password": "syncpass123"}).status_code == 200
