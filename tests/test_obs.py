"""OBS remote rescue.

The failure that matters is not "the stream stopped" — OBS can restart that.
It is "OBS is GONE", where there is no WebSocket to ask. So the rescue has to
work at three levels, and each is pinned here:

  * running and live          -> do nothing (relaunching would kill a recording)
  * running but offline       -> ask it to go live
  * crashed or hung           -> relaunch, wait for it to answer, then go live

A real OBS WebSocket server is run in-process here — the actual v5 protocol,
including the authentication challenge — so the handshake and the commands are
exercised for real rather than mocked away.
"""
import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals import obs as obs_mod
from pcrituals.actions import dispatch_action
from pcrituals.models import Action, ActionType
from pcrituals.obs import OBS, OBSError, auth_string

websockets = pytest.importorskip("websockets")


# --------------------------------------------------------------------------
# A real obs-websocket v5 server.
# --------------------------------------------------------------------------
class FakeOBS:
    def __init__(self, password="", streaming=False, scenes=None):
        self.password = password
        self.streaming = streaming
        self.scenes = scenes if scenes is not None else ["MAIN 1", "BRB", "Starting Soon"]
        self.scene = self.scenes[0] if self.scenes else ""
        self.seen = []
        self.port = 0
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._stop_evt = None
        self._server = None

    async def _handler(self, ws):
        hello = {"op": 0, "d": {"obsWebSocketVersion": "5.5.2", "rpcVersion": 1}}
        salt, challenge = "salt123", "challenge456"
        if self.password:
            hello["d"]["authentication"] = {"challenge": challenge, "salt": salt}
        await ws.send(json.dumps(hello))

        first = json.loads(await ws.recv())
        if self.password:
            if (first.get("d") or {}).get("authentication") != auth_string(
                    self.password, salt, challenge):
                await ws.close(code=4009, reason="authentication failed")
                return
        await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))

        async for raw in ws:
            m = json.loads(raw)
            if m.get("op") != 6:
                continue
            d = m["d"]
            self.seen.append(d["requestType"])
            if d["requestType"] == "GetStreamStatus":
                data = {"outputActive": self.streaming, "outputReconnecting": False,
                        "outputTimecode": "00:01:00.000" if self.streaming else "",
                        "outputCongestion": 0.0, "outputSkippedFrames": 0,
                        "outputTotalFrames": 100}
            elif d["requestType"] == "StartStream":
                self.streaming = True
                data = {}
            elif d["requestType"] == "StopStream":
                self.streaming = False
                data = {}
            elif d["requestType"] == "GetSceneList":
                data = {"scenes": [{"sceneName": n} for n in self.scenes],
                        "currentProgramSceneName": self.scene}
            elif d["requestType"] == "SetCurrentProgramScene":
                self.scene = (d.get("requestData") or {}).get("sceneName", "")
                data = {}
            else:
                data = {}
            await ws.send(json.dumps({
                "op": 7,
                "d": {"requestType": d["requestType"], "requestId": d["requestId"],
                      "requestStatus": {"result": True, "code": 100},
                      "responseData": data},
            }))

    def start(self):
        async def main():
            self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._stop_evt = asyncio.Event()
            self._ready.set()
            await self._stop_evt.wait()
            # Close properly. Tearing the loop down instead produced a wall of
            # "Event loop stopped before Future completed" noise on every test,
            # which is exactly the kind of thing that hides a real failure.
            self._server.close()
            await self._server.wait_closed()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(main())

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "fake OBS did not start"
        return self

    def stop(self):
        if self._loop and self._stop_evt:
            self._loop.call_soon_threadsafe(self._stop_evt.set)
        if self._thread:
            self._thread.join(timeout=5)
        if self._loop and not self._loop.is_closed():
            self._loop.close()


@pytest.fixture
def fake_obs():
    servers = []

    def make(**kw):
        s = FakeOBS(**kw).start()
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.stop()


class FakeConfig:
    def __init__(self, port, password="", exe=""):
        self.obs_host = "127.0.0.1"
        self.obs_port = port
        self.obs_password = password
        self.obs_exe = exe


class FakePlatform:
    """Records what the rescue did to the machine."""

    def __init__(self, obs_running=False, exe=""):
        self.obs_running = obs_running
        self.exe = exe
        self.launched = []
        self.killed = []

    def list_running_processes(self, hint):
        return ["obs64"] if self.obs_running else []

    def close_application(self, name):
        self.killed.append(name)
        self.obs_running = False
        return True

    def launch(self, target, args=None):
        self.launched.append(target)
        self.obs_running = True


# ---- the protocol --------------------------------------------------------

def test_auth_string_matches_the_documented_construction():
    """Pinned vector: base64(sha256(base64(sha256(pw + salt)) + challenge))."""
    assert auth_string("secret", "salt123", "challenge456") == \
        "xgzgHJ5CaCNvrzkqxH6D2xMsV17ODXfIyB12Cj4aV1o="


def test_auth_string_is_deterministic_and_sensitive_to_each_input():
    base = auth_string("secret", "salt123", "challenge456")
    assert auth_string("secret", "salt123", "challenge456") == base
    assert auth_string("other", "salt123", "challenge456") != base
    assert auth_string("secret", "other", "challenge456") != base
    assert auth_string("secret", "salt123", "other") != base


# ---- status and commands -------------------------------------------------

def test_status_reports_a_live_stream(fake_obs):
    s = fake_obs(streaming=True)
    st = OBS(FakeConfig(s.port)).status()
    assert st["reachable"] is True
    assert st["streaming"] is True
    assert st["timecode"] == "00:01:00.000"


def test_status_reports_an_offline_stream(fake_obs):
    s = fake_obs(streaming=False)
    st = OBS(FakeConfig(s.port)).status()
    assert st["reachable"] is True
    assert st["streaming"] is False


def test_status_is_not_an_exception_when_obs_is_absent():
    """OBS being closed is a normal state, not an error to throw at the phone."""
    st = OBS(FakeConfig(59999)).status()
    assert st["reachable"] is False
    assert st["streaming"] is False
    assert st["error"]


def test_password_authentication_succeeds(fake_obs):
    s = fake_obs(password="hunter2", streaming=False)
    st = OBS(FakeConfig(s.port, password="hunter2")).status()
    assert st["reachable"] is True


def test_wrong_password_is_reported_not_swallowed(fake_obs):
    s = fake_obs(password="hunter2")
    st = OBS(FakeConfig(s.port, password="wrong")).status()
    assert st["reachable"] is False
    assert st["error"]


def test_missing_password_when_obs_wants_one_is_explained(fake_obs):
    s = fake_obs(password="hunter2")
    st = OBS(FakeConfig(s.port, password="")).status()
    assert st["reachable"] is False
    assert "password" in st["error"].lower()


def test_start_and_stop_stream(fake_obs):
    s = fake_obs(streaming=False)
    o = OBS(FakeConfig(s.port))
    o.start_stream()
    assert s.streaming is True
    o.stop_stream()
    assert s.streaming is False
    assert "StartStream" in s.seen and "StopStream" in s.seen


# ---- the rescue ----------------------------------------------------------

def test_rescue_does_nothing_when_already_live(fake_obs):
    """Relaunching a healthy OBS would kill a recording and lose scene state."""
    s = fake_obs(streaming=True)
    plat = FakePlatform()
    r = OBS(FakeConfig(s.port)).rescue(platform=plat, wait=2)
    assert r["ok"] is True and r["did"] == "none"
    assert plat.launched == [] and plat.killed == []


def test_rescue_starts_a_stream_that_merely_stopped(fake_obs):
    s = fake_obs(streaming=False)
    plat = FakePlatform(obs_running=True)
    r = OBS(FakeConfig(s.port)).rescue(platform=plat, wait=2)
    assert r["did"] == "start"
    assert r["ok"] is True
    assert plat.launched == [], "a running OBS must not be relaunched"


def test_rescue_relaunches_a_crashed_obs(fake_obs, monkeypatch):
    """The headline case: OBS is gone and the user is out of the house."""
    s = fake_obs(streaming=False)
    cfg = FakeConfig(s.port, exe="/fake/obs64.exe")
    # First write the exe exists, then fail the first status so the rescue
    # believes OBS is down, then let it answer.
    monkeypatch.setattr(obs_mod.os.path, "exists", lambda p: p == "/fake/obs64.exe")
    o = OBS(cfg)
    real_status = o.status
    calls = {"n": 0}

    def status_once_down():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"reachable": False, "streaming": False}
        return real_status()

    monkeypatch.setattr(o, "status", status_once_down)
    plat = FakePlatform(obs_running=True)
    r = o.rescue(platform=plat, wait=10)
    assert plat.killed == ["obs64"], "a hung OBS must be closed first"
    assert plat.launched == ["/fake/obs64.exe"]
    assert r["did"] == "relaunch"
    assert r["ok"] is True
    assert s.streaming is True


def test_rescue_explains_when_obs_cannot_be_found(fake_obs, monkeypatch):
    s = fake_obs()
    monkeypatch.setattr(obs_mod.os.path, "exists", lambda p: False)
    monkeypatch.setattr(obs_mod, "OBS_EXE_CANDIDATES", ())
    o = OBS(FakeConfig(s.port, exe=""))
    monkeypatch.setattr(o, "status", lambda: {"reachable": False, "streaming": False})
    with pytest.raises(OBSError) as err:
        o.rescue(platform=FakePlatform(), wait=2)
    assert "couldn't find obs" in str(err.value).lower()


# ---- OBS as a Play step ---------------------------------------------------

class ActionPlatform:
    """What dispatch_action needs: a platform that carries the app config."""

    def __init__(self, cfg):
        self.config = cfg

    def launch(self, *a, **k):
        pass


def test_obs_actions_dispatch_from_inside_a_running_event_loop(fake_obs):
    """The engine runs actions from an async context, where asyncio.run()
    raises "cannot be called from a running event loop". A Play containing an
    OBS step would have failed exactly there."""
    s = fake_obs(streaming=False)
    plat = ActionPlatform(FakeConfig(s.port))

    async def go():
        await dispatch_action(plat, Action(type=ActionType.OBS_STREAM_START))
        await dispatch_action(plat, Action(type=ActionType.OBS_STREAM_STOP))

    asyncio.run(go())
    assert "StartStream" in s.seen and "StopStream" in s.seen


def test_obs_scene_step_switches_the_scene(fake_obs):
    s = fake_obs(scenes=["MAIN 1", "BRR", "Starting Soon"])
    plat = ActionPlatform(FakeConfig(s.port))

    async def go():
        await dispatch_action(plat, Action(type=ActionType.OBS_SCENE, target="MAIN 1"))

    asyncio.run(go())
    assert s.scene == "MAIN 1"


def test_obs_scene_step_is_case_insensitive_but_fails_loudly_when_unknown(fake_obs):
    """Users type "main 1". An exact match is found if possible; a genuinely
    missing scene must fail rather than silently doing nothing."""
    s = fake_obs(scenes=["MAIN 1"])
    o = OBS(FakeConfig(s.port))
    o.set_scene("main 1")
    assert s.scene == "MAIN 1"
    with pytest.raises(OBSError) as err:
        o.set_scene("nope")
    assert "MAIN 1" in str(err.value)


def test_obs_scene_step_requires_a_target(fake_obs):
    s = fake_obs()
    plat = ActionPlatform(FakeConfig(s.port))

    async def go():
        await dispatch_action(plat, Action(type=ActionType.OBS_SCENE, target=""))

    with pytest.raises(Exception):
        asyncio.run(go())


def test_every_action_type_is_registered():
    """An unregistered type fails at RUN time, inside a Play, in front of the
    user. Catch it here instead."""
    import inspect

    from pcrituals import actions as act
    from pcrituals.models import ActionType as AT

    src = inspect.getsource(act.dispatch_action)
    missing = [t.name for t in AT if f"ActionType.{t.name}" not in src]
    assert not missing, f"no handler registered for: {missing}"


def test_rescue_reports_every_step_it_took(fake_obs, monkeypatch):
    """A button that says "fixed" while the stream is down is worse than one
    that admits what it could not reach — so the steps are always returned."""
    s = fake_obs(streaming=False)
    plat = FakePlatform(obs_running=True)
    r = OBS(FakeConfig(s.port)).rescue(platform=plat, wait=2)
    assert isinstance(r["steps"], list) and r["steps"]
    assert any("running" in s_ for s_ in r["steps"])
