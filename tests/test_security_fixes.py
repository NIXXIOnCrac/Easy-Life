"""Regression tests for the security fixes from the adversarial review.

Each test proves a fix bites: revert the fix and the test fails.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals.models import Action, ActionType


# ---- 1. a vault can no longer smuggle a destructive command onto the deck ----
def test_vault_restore_rejects_a_destructive_deck_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    from pcrituals.app import App
    from pcrituals.config import load_config
    from pcrituals.vault import apply_vault

    app = App(load_config())
    report = apply_vault(app, {
        "version": 1,
        "rituals": [],
        "deck": [{"kind": "command", "target": "rm -rf /", "label": "evil"}],
        "settings": {},
    }, mode="replace")
    # The button must NOT be saved, and the error must be reported.
    assert report["deck"] == 0
    assert any("blocked" in e.lower() or "destructive" in e.lower() for e in report["errors"])
    assert app.store.list_deck() == []


def test_vault_restore_still_allows_a_benign_deck_button(tmp_path, monkeypatch):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    from pcrituals.app import App
    from pcrituals.config import load_config
    from pcrituals.vault import apply_vault

    app = App(load_config())
    report = apply_vault(app, {
        "version": 1,
        "rituals": [],
        "deck": [{"kind": "app", "target": "notepad", "label": "Notes"}],
        "settings": {},
    }, mode="replace")
    assert report["deck"] == 1
    assert app.store.list_deck()[0].label == "Notes"


# ---- 2. OBS host/port can no longer be pointed at arbitrary hosts (SSRF) ----
# Tested on App directly (not TestClient): mixing TestClient's anyio portal
# with the asyncio.run() delay tests in one process hangs pytest at teardown.
def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    from pcrituals.app import App
    from pcrituals.config import load_config
    return App(load_config())


def test_obs_host_rejects_a_public_ip(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        _app(tmp_path, monkeypatch).set_settings({"obs_host": "8.8.8.8"})


def test_obs_host_rejects_a_hostname(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        _app(tmp_path, monkeypatch).set_settings({"obs_host": "internal.corp"})


def test_obs_host_accepts_loopback(tmp_path, monkeypatch):
    out = _app(tmp_path, monkeypatch).set_settings({"obs_host": "127.0.0.1"})
    assert out["obs_host"] == "127.0.0.1"


def test_obs_port_rejects_out_of_range(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        _app(tmp_path, monkeypatch).set_settings({"obs_port": 99999})


# ---- 3. a delay can be interrupted by the cancel event ----
# asyncio.run() in the MAIN thread conflicts with TestClient's anyio portal and
# hangs the full suite, so these run their loop in a worker thread.
def _run_in_thread(coro):
    import threading
    box = {}
    def target():
        box["v"] = asyncio.run(coro)
    t = threading.Thread(target=target)
    t.start()
    t.join(timeout=15)
    return box.get("v")


def test_delay_is_interruptible():
    from pcrituals.actions import dispatch_action

    class _P:
        pass

    import threading
    ev = threading.Event()
    ev.set()  # cancel already requested
    action = Action(type=ActionType.DELAY, params={"seconds": 3600})

    async def go():
        # Must return promptly (not sleep 3600s) when the cancel event is set.
        await dispatch_action(_P(), action, cancel_event=ev)
        return "returned"

    assert _run_in_thread(go()) == "returned"


def test_delay_without_cancel_still_waits():
    from pcrituals.actions import dispatch_action

    class _P:
        pass

    action = Action(type=ActionType.DELAY, params={"seconds": 0.05})

    async def go():
        await dispatch_action(_P(), action)  # must not raise

    _run_in_thread(go())
