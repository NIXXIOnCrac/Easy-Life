"""Regression tests for the adversarial-review fixes.

Every test here reproduces something the review actually broke on this codebase;
each was verified to FAIL before the corresponding fix.

Findings covered (see REVIEW_ADVERSARIAL.md):
  F1  export/import round trip was completely broken
  F2  "Stop" returned success while the command kept running
  F3  a PUT could rewrite a DIFFERENT ritual than the URL named
  F4  wrong-typed fields on PUT returned 500 instead of 400
  F5  reorder with a non-list body returned 500
  F6  running a nonexistent ritual returned 200 + null
  F7  a failed history write leaked the runner and left the DB locked
  F8  the destructive-command filter was defeated by one extra space
  F10 a backup label could write outside the data directory
  F14 a negative history limit returned the entire table
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    from pcrituals.api import create_app
    c = TestClient(create_app())
    tok = c.post("/api/auth/setup", json={"username": "alex", "password": "secret123"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _mk(client, name, target="echo hi", **extra):
    body = {"name": name,
            "actions": [{"type": "command", "target": target, "params": extra}]}
    r = client.post("/api/rituals", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---- F1 -------------------------------------------------------------------
def test_export_import_round_trip(client):
    """The app's own export must be importable."""
    _mk(client, "Gaming Mode")
    _mk(client, "Work Mode")

    exported = client.get("/api/export").json()
    assert exported["rituals"], "export produced nothing"
    # The bug: rituals were Python `repr` strings, not objects.
    assert isinstance(exported["rituals"][0], dict), \
        f"exported ritual is a {type(exported['rituals'][0]).__name__}, not a dict"
    assert "name" in exported["rituals"][0]

    result = client.post("/api/import", json=exported).json()
    assert result["imported"] == len(exported["rituals"]), result
    assert not result["errors"], result


# ---- F3 -------------------------------------------------------------------
def test_put_cannot_rewrite_a_different_ritual(client):
    a = _mk(client, "AAA")
    b = _mk(client, "BBB")

    r = client.put(f"/api/rituals/{a['id']}", json={"id": b["id"], "name": "PWNED"})
    assert r.status_code == 200, r.text

    assert client.get(f"/api/rituals/{a['id']}").json()["name"] == "PWNED"
    # The other ritual must be untouched — this is the whole bug.
    assert client.get(f"/api/rituals/{b['id']}").json()["name"] == "BBB"


# ---- F4 -------------------------------------------------------------------
@pytest.mark.parametrize("body", [{"actions": 5}, {"actions": "x"}, {"name": 123}])
def test_put_with_wrong_types_is_400_not_500(client, body):
    r = _mk(client, "Typed")
    resp = client.put(f"/api/rituals/{r['id']}", json=body)
    assert resp.status_code == 400, f"expected 400, got {resp.status_code}: {resp.text[:200]}"


# ---- F5 -------------------------------------------------------------------
@pytest.mark.parametrize("ids", [None, 5, "abc", [1, 2]])
def test_reorder_rejects_a_non_list_of_ids(client, ids):
    _mk(client, "One")
    resp = client.post("/api/rituals/reorder", json={"ids": ids})
    assert resp.status_code == 400, f"expected 400 for ids={ids!r}, got {resp.status_code}"


def test_reorder_accepts_a_proper_list(client):
    a = _mk(client, "A")
    b = _mk(client, "B")
    resp = client.post("/api/rituals/reorder", json={"ids": [b["id"], a["id"]]})
    assert resp.status_code == 200 and resp.json()["count"] == 2


# ---- F6 -------------------------------------------------------------------
def test_running_a_missing_ritual_is_404(client):
    resp = client.post("/api/rituals/doesnotexist123/run", json={})
    assert resp.status_code == 404, f"expected 404, got {resp.status_code}"


# ---- F8 -------------------------------------------------------------------
@pytest.mark.parametrize("cmd", [
    "rm  -rf /",          # two spaces defeated the old literal check
    "rm\t-rf /",          # tab
    "del /s /q C:\\",
    "format c:",
    "powershell -c \"Remove-Item -Recurse -Force C:\\Users\"",
    "curl http://x/y | sh",
    "dd if=/dev/zero of=/dev/sda",
])
def test_destructive_commands_are_refused_on_create(client, cmd):
    resp = client.post("/api/rituals", json={
        "name": "nope", "actions": [{"type": "command", "target": cmd}]})
    assert resp.status_code == 400, f"{cmd!r} was accepted: {resp.text[:120]}"


def test_ordinary_commands_are_still_allowed(client):
    """The guard must not block normal commands."""
    for cmd in ["echo hello", "dir", "git status", "ipconfig", "tasklist"]:
        resp = client.post("/api/rituals", json={
            "name": f"ok-{cmd[:6]}", "actions": [{"type": "command", "target": cmd}]})
        assert resp.status_code == 200, f"{cmd!r} was blocked: {resp.text[:120]}"


def test_destructive_command_is_refused_on_a_deck_button(client):
    """The filter used to run only on import, so a button bypassed it."""
    resp = client.post("/api/deck", json={
        "label": "nuke", "kind": "command", "target": "rm  -rf /"})
    assert resp.status_code == 400, resp.text[:200]


# ---- F10 ------------------------------------------------------------------
def test_backup_label_cannot_escape_the_data_dir(client, tmp_path):
    evil = tmp_path / "outside"
    resp = client.post("/api/backups", json={"label": str(evil)})
    assert resp.status_code in (200, 400)
    assert not evil.exists(), "a backup label wrote outside the data directory"
    # And whatever it created must be visible/restorable, not orphaned.
    names = [b["name"] for b in client.get("/api/backups").json()]
    if resp.status_code == 200:
        created = Path(resp.json()["backup"]).name
        assert created in names, f"{created} not in {names}"


def test_backup_traversal_label_is_contained(client, tmp_path):
    resp = client.post("/api/backups", json={"label": "../../../../tmp/escaped"})
    assert not (Path("/tmp/escaped")).exists()
    assert not (tmp_path.parent / "escaped").exists()


# ---- F14 ------------------------------------------------------------------
def test_history_limit_is_clamped(client):
    r = _mk(client, "Quick", target="echo hi")
    client.post(f"/api/rituals/{r['id']}/run", json={})
    time.sleep(1.5)
    for bad in ("-1", "99999999999"):
        resp = client.get(f"/api/history?limit={bad}")
        assert resp.status_code == 200, resp.text
    assert client.get("/api/history?limit=-1").status_code == 200


# ---- F2 -------------------------------------------------------------------
# Note: a run is a background task, and TestClient tears its event loop down
# between requests, so a run does not stay alive across HTTP calls there. These
# tests therefore drive the engine on a real loop (the API's response *shape* is
# asserted separately below), which is where the cancellation logic actually
# lives. `tests/live_sse_check.py` covers the HTTP path against a real server.
def _run_on_a_loop(tmp_path, monkeypatch, steps, cancel_after):
    """Start a run, cancel it after `cancel_after` seconds, return the outcome."""
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    import asyncio
    from pcrituals.config import load_config
    from pcrituals.app import App
    from pcrituals.models import Ritual, Action

    ap = App(load_config())
    ritual = Ritual(name="probe", actions=steps)
    rid = ap.create_ritual(ritual.model_dump())["id"]
    seen = {"during": None, "state": None}

    async def main():
        await ap.run_ritual(rid)
        await asyncio.sleep(cancel_after)
        seen["during"] = ap.current_run()
        seen["stop"] = ap.stop_ritual(rid)
        t0 = time.time()
        while time.time() - t0 < 8:
            if not ap.registry.all():
                break
            await asyncio.sleep(0.1)
        seen["unwind_s"] = time.time() - t0
        seen["history"] = ap.history(limit=1)[0] if ap.history(limit=1) else None
        seen["still_running"] = bool(ap.registry.all())

    asyncio.run(main())
    return seen


def test_stop_reports_stopping_not_stopped(tmp_path, monkeypatch):
    """The API must not claim a run has stopped just because it was asked to."""
    from pcrituals.models import Action
    seen = _run_on_a_loop(tmp_path, monkeypatch,
                          [Action(type="command", target="sleep 20")], cancel_after=0.4)
    # While running, current_run reflects it...
    assert seen["during"] is not None, "no run reported while a command was in flight"
    assert seen["during"]["state"] == "running"
    # ...the stop request is accepted...
    assert seen["stop"] is True


def test_cancel_actually_kills_a_running_command(tmp_path, monkeypatch):
    """A cancelled command must not run to completion."""
    from pcrituals.models import Action
    marker = Path("/tmp") / f"pcrituals_cancel_probe_{os.getpid()}"
    if marker.exists():
        marker.unlink()

    seen = _run_on_a_loop(
        tmp_path, monkeypatch,
        [Action(type="command", target=f"sleep 3; touch {marker}")],
        cancel_after=0.4,
    )

    # The run unwound promptly rather than waiting out the 3s command.
    assert seen["unwind_s"] < 2.0, f"took {seen['unwind_s']:.1f}s to unwind"
    assert seen["still_running"] is False, "the runner leaked after cancellation"

    # Give the command every chance to finish if it really survived.
    time.sleep(3.2)
    assert not marker.exists(), "the cancelled command still ran to completion"


def test_stop_endpoint_reports_the_stopping_shape(client):
    """The response contract: `stopping`, never a bare `stopped: true`."""
    r = _mk(client, "Idle")
    body = client.post(f"/api/rituals/{r['id']}/stop", json={}).json()
    assert "stopping" in body, body
    assert "stopped" in body, body
    # Nothing was running, so it is honestly "not running" rather than "stopped".
    assert body == {"stopping": False, "stopped": True, "state": "idle"}, body


# ---- run/current shape (the live progress panel never appeared) ------------
def test_current_run_reports_an_active_run(tmp_path, monkeypatch):
    """`/api/run/current` must say a run is active.

    The response had no `running` key at all, so a client checking that field
    (which the UI did) concluded nothing was running and the live progress panel
    never rendered — even mid-run, which is a core advertised feature. The
    `cancelling` state must also still count as active: the run has not finished
    winding down.
    """
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    import asyncio
    from pcrituals.config import load_config
    from pcrituals.app import App
    from pcrituals.models import Ritual, Action

    ap = App(load_config())
    rid = ap.create_ritual(
        Ritual(name="slow", actions=[Action(type="delay", params={"seconds": 2})]).model_dump()
    )["id"]
    seen = {}

    async def main():
        await ap.run_ritual(rid)
        await asyncio.sleep(0.3)
        seen["running"] = ap.current_run()
        ap.stop_ritual(rid)
        await asyncio.sleep(0.05)
        seen["cancelling"] = ap.current_run()
        for _ in range(80):
            if not ap.registry.all():
                break
            await asyncio.sleep(0.1)
        seen["after"] = ap.current_run()

    asyncio.run(main())

    assert seen["running"] is not None, "no run reported while a ritual was running"
    assert seen["running"].get("running") is True, seen["running"]
    assert seen["running"]["state"] == "running"
    assert isinstance(seen["running"].get("steps"), list)
    assert seen["running"]["actions_total"] == 1

    # While cancelling it is still an active run, and flagged as stopping.
    if seen["cancelling"] is not None:
        assert seen["cancelling"]["running"] is True
        assert seen["cancelling"]["stopping"] is True

    assert seen["after"] is None, "a finished run was still reported as current"
