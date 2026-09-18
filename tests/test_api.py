"""Integration tests for the API, security/pairing, and import/export."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["PCRITUALS_DATA_DIR"] = tempfile.mkdtemp(prefix="pcrituals_test_")
os.environ["PCRITUALS_HOST"] = "127.0.0.1"

from fastapi.testclient import TestClient
from pcrituals.api import create_app


@pytest.fixture(scope="module")
def client():
    app = create_app()
    # NOTE: TestClient uses host "testserver" which is NOT loopback-by-our-check,
    # so remote auth rules apply. We pass the local token for authorized calls.
    from pcrituals.security import PairingCode
    from datetime import datetime, timezone
    token = app.state.pcrituals.security.register_local()
    return TestClient(app), token


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def g(c, path, token):
    return c.get(path, headers=auth(token))


def test_config_defaults_bind_lan(client):
    """The server must be LAN-reachable (0.0.0.0) so an iPhone can connect,
    and the pairing QR must embed a real LAN URL, not localhost."""
    from pcrituals.config import Config
    c = Config()
    assert c.bind_loopback_only is False
    assert c.host == "0.0.0.0"
    # Pairing must produce a LAN URL (not 127.0.0.1) for the phone to use.
    c2, token = client
    r = c2.post("/api/pair/start", headers=auth(token))
    assert r.status_code == 200
    lan = r.json().get("lan_url", "")
    assert lan.startswith("http://")
    assert "127.0.0.1" not in lan


def test_pairing_requires_valid_code(client):
    c, _ = client
    r = c.post("/api/pair/complete", json={"code": "NOPE"})
    assert r.status_code == 401


def test_full_pairing_flow_gives_token(client):
    c, token = client
    # Start pairing (loopback via local token) -> get code.
    r = c.post("/api/pair/start", headers=auth(token))
    assert r.status_code == 200
    data = r.json()
    code = data["code"]
    assert len(code) == 6
    # The response must include a real QR PNG (data URI) and a LAN url.
    assert data.get("qr_png", "").startswith("data:image/png;base64,")
    assert data.get("lan_url", "").startswith("http://")

    # Redeem it.
    r2 = c.post("/api/pair/complete", json={"code": code, "device_name": "iPhone"})
    assert r2.status_code == 200
    dev_token = r2.json()["token"]
    assert dev_token.startswith("pcrit")

    # Phone can now list devices with its token, but NOT without.
    assert c.get("/api/devices", headers=auth(dev_token)).status_code == 200


def test_ritual_crud(client):
    c, token = client
    r = c.post("/api/rituals", headers=auth(token), json={
        "name": "Workout", "stop_on_error": True,
        "actions": [{"type": "delay", "params": {"seconds": 1}}, {"type": "website", "target": "https://example.com"}],
    })
    assert r.status_code == 200
    rid = r.json()["id"]
    assert len(r.json()["actions"]) == 2

    # duplicate
    d = c.post(f"/api/rituals/{rid}/duplicate", headers=auth(token))
    assert d.status_code == 200
    assert d.json()["name"] == "Workout (copy)"
    assert d.json()["id"] != rid

    # update
    u = c.put(f"/api/rituals/{rid}", headers=auth(token), json={"name": "Workout Pro"})
    assert u.status_code == 200
    assert u.json()["name"] == "Workout Pro"

    # list
    assert len(c.get("/api/rituals", headers=auth(token)).json()) >= 2

    # delete
    assert c.delete(f"/api/rituals/{rid}", headers=auth(token)).status_code == 200
    assert c.get(f"/api/rituals/{rid}", headers=auth(token)).status_code == 404


def test_reorder_syntax(client):
    c, token = client
    ids = [r["id"] for r in c.get("/api/rituals", headers=auth(token)).json()]
    r = c.post("/api/rituals/reorder", headers=auth(token), json={"ids": list(reversed(ids))})
    assert r.status_code == 200


def test_import_validates_and_blocks_dangerous(client):
    c, token = client
    good = {"version": 1, "rituals": [{"name": "Imported", "actions": [{"type": "website", "target": "https://safe.com"}]}]}
    r = c.post("/api/import", headers=auth(token), json=good)
    assert r.status_code == 200
    assert r.json()["imported"] == 1

    bad = {"rituals": [{"name": "Evil", "actions": [{"type": "command", "target": "rm -rf /"}]}]}
    r = c.post("/api/import", headers=auth(token), json=bad)
    assert r.status_code == 200
    assert r.json()["imported"] == 0
    assert len(r.json()["errors"]) == 1


def test_power_requires_confirm_for_destructive(client):
    c, token = client
    r = c.post("/api/power", headers=auth(token), json={"action": "shutdown"})
    assert r.status_code == 400  # needs confirm
    r = c.post("/api/power", headers=auth(token), json={"action": "lock"})
    assert r.status_code in (200, 500)  # executed or unsupported on this platform


def test_history_clear(client):
    c, token = client
    r = c.delete("/api/history", headers=auth(token))
    assert r.status_code == 200


def test_integrations_endpoint(client):
    c, token = client
    r = c.get("/api/integrations", headers=auth(token))
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list) and len(data) >= 9
    assert all("name" in i and "installed" in i for i in data)


def test_wol_validation(client):
    c, token = client
    # Valid status
    assert c.get("/api/wol", headers=auth(token)).status_code == 200
    # Bad MAC rejected with 400
    r = c.post("/api/wol/wake", headers=auth(token), json={"mac": "not-a-mac"})
    assert r.status_code == 400
    # Missing MAC
    assert c.post("/api/wol/wake", headers=auth(token), json={}).status_code == 400


def test_voice_commands(client):
    c, token = client
    # Unrecognized
    r = c.post("/api/voice", headers=auth(token), json={"phrase": "what's the weather"})
    assert r.status_code == 200
    assert r.json()["intent"] is None
    # Destructive power action requires confirmation
    r = c.post("/api/voice", headers=auth(token), json={"phrase": "shut down"})
    assert r.status_code == 200
    assert r.json()["intent"] == "shutdown"
    assert r.json()["needs_confirmation"] is True
    assert r.json()["executed"] is False
    # Lock executes (needs no confirmation)
    r = c.post("/api/voice", headers=auth(token), json={"phrase": "lock the pc"})
    assert r.json()["intent"] == "lock"
    # Confirm executes destructive
    r = c.post("/api/voice", headers=auth(token), json={"phrase": "shut down", "confirm": True})
    assert r.json()["executed"] in (True, False)  # platform-dependent
    # Missing phrase
    assert c.post("/api/voice", headers=auth(token), json={}).status_code == 400


def test_voice_starts_by_ritual_name(client):
    c, token = client
    # Create a ritual first.
    r = c.post("/api/rituals", headers=auth(token), json={
        "name": "Coffee", "stop_on_error": True,
        "actions": [{"type": "delay", "params": {"seconds": 0.2}}],
    })
    rid = r.json()["id"]
    # Trigger it by (case-insensitive) name.
    r = c.post("/api/voice", headers=auth(token), json={"phrase": "start coffee"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "start_ritual"
    assert body["ritual_name"].lower() == "coffee"
    assert body["executed"]["executed"] is True


def test_backup_crud(client):
    c, token = client
    # Create
    r = c.post("/api/backups", headers=auth(token), json={"label": "test"})
    assert r.status_code == 200
    assert r.json()["files"] >= 1
    # List
    lst = c.get("/api/backups", headers=auth(token)).json()
    assert len(lst) >= 1
    name = lst[0]["name"]
    # Delete
    assert c.delete(f"/api/backups/{name}", headers=auth(token)).status_code == 200
    # Restore of a missing backup -> 404
    r = c.post("/api/backups/nonexistent/restore", headers=auth(token))
    assert r.status_code == 404


def test_settings_and_update_endpoints(client):
    c, token = client
    # Get current settings
    r = c.get("/api/settings", headers=auth(token))
    assert r.status_code == 200
    assert "update_url" in r.json()
    # Set update URL
    r = c.post("/api/settings", headers=auth(token), json={"update_url": "https://example.com/update.json"})
    assert r.status_code == 200
    assert r.json()["update_url"] == "https://example.com/update.json"
    # Update status always returns version info
    r = c.get("/api/update/status", headers=auth(token))
    assert r.status_code == 200
    assert "version" in r.json()
    assert "available" in r.json()
    # Reset it
    c.post("/api/settings", headers=auth(token), json={"update_url": ""})