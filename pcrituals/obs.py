"""Remote rescue for OBS — restart a dead stream from your phone, from anywhere.

The scenario this exists for: you're streaming IRL, out of the house, and OBS
crashes or the stream drops. Walking home isn't an option.

The interesting failure is not "the stream stopped" — OBS can restart that on
its own. It's **"OBS is gone"**, where there is no WebSocket to talk to and
nothing to ask. So the rescue works at two levels:

  * OBS is running but offline  -> tell it to go live.
  * OBS is running but hung     -> close it, relaunch, wait for it to answer.
  * OBS is not running at all   -> relaunch it, wait, then go live.

Every step is reported, because a button that says "fixed" while the stream is
still down is worse than one that admits it couldn't reach OBS.

OBS 28+ ships the WebSocket server built in; it just has to be switched on once
(Tools -> WebSocket Server Settings), and its password set here.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4455

# OBS's own process name and the usual install locations, so the user does not
# have to hunt for the executable.
OBS_PROCESS = "obs64"
OBS_EXE_CANDIDATES = (
    r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files\obs-studio\bin\64bit\obs32.exe",
)

CONNECT_TIMEOUT = 4.0
RELAUNCH_WAIT = 60.0


class OBSError(RuntimeError):
    """OBS could not be reached or refused the command, with a reason."""


def auth_string(password: str, salt: str, challenge: str) -> str:
    """The obs-websocket v5 authentication string.

    base64(sha256(base64(sha256(password + salt)) + challenge)) — per the
    protocol spec; getting the nesting wrong just yields "authentication
    failed" with no explanation, so it is pinned by its own test.
    """
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


class _Client:
    """One connection's worth of OBS RPC."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._n = 0

    @classmethod
    async def connect(cls, host: str, port: int, password: str,
                      timeout: float = CONNECT_TIMEOUT) -> "_Client":
        import websockets  # lazy: keeps this module importable without it

        try:
            ws = await asyncio.wait_for(websockets.connect(f"ws://{host}:{port}"),
                                        timeout)
        except Exception as e:
            raise OBSError(f"OBS is not answering on {host}:{port} ({type(e).__name__})") from e
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        except Exception as e:
            await ws.close()
            raise OBSError(f"OBS sent no hello message ({type(e).__name__})") from e

        ident: dict = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = (hello.get("d") or {}).get("authentication")
        if auth:
            if not password:
                await ws.close()
                raise OBSError("OBS is asking for a password. Add it in Easy Life settings.")
            ident["authentication"] = auth_string(password, auth.get("salt", ""),
                                                  auth.get("challenge", ""))
        try:
            await ws.send(json.dumps({"op": 1, "d": ident}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        except Exception as e:
            await ws.close()
            raise OBSError(f"OBS rejected the connection ({type(e).__name__})") from e
        if reply.get("op") != 2:      # 2 = Identified
            await ws.close()
            raise OBSError("OBS refused the connection — check the password.")
        return cls(ws)

    async def request(self, rtype: str, data: dict | None = None,
                      timeout: float = 10.0) -> dict:
        self._n += 1
        rid = str(self._n)
        await self.ws.send(json.dumps({
            "op": 6, "d": {"requestType": rtype, "requestId": rid,
                           "requestData": data or {}},
        }))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
            if msg.get("op") != 7:            # 7 = RequestResponse
                continue                      # events (op 5) are ignored
            d = msg.get("d") or {}
            if d.get("requestId") != rid:
                continue
            st = d.get("requestStatus") or {}
            if not st.get("result", False):
                raise OBSError(d.get("requestType", rtype) + " failed: "
                               + str(st.get("comment") or st.get("code")))
            return d.get("responseData") or {}

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:
            pass


def _run(coro, timeout: float = 30.0):
    """Run one asyncio job from sync code (FastAPI handlers run in a threadpool)."""
    return asyncio.run(asyncio.wait_for(coro, timeout))


class OBS:
    """Serially reachable OBS controls, configured from the app's config."""

    def __init__(self, config: Any) -> None:
        self.config = config

    # -- settings ----------------------------------------------------------
    @property
    def host(self) -> str:
        return getattr(self.config, "obs_host", DEFAULT_HOST) or DEFAULT_HOST

    @property
    def port(self) -> int:
        return int(getattr(self.config, "obs_port", DEFAULT_PORT) or DEFAULT_PORT)

    @property
    def password(self) -> str:
        return getattr(self.config, "obs_password", "") or ""

    @property
    def exe(self) -> str:
        return getattr(self.config, "obs_exe", "") or ""

    def find_exe(self) -> str:
        """Where OBS lives: the configured path, else the usual places."""
        if self.exe and os.path.exists(self.exe):
            return self.exe
        for cand in OBS_EXE_CANDIDATES:
            if os.path.exists(cand):
                return cand
        return ""

    # -- status ------------------------------------------------------------
    def status(self) -> dict:
        return _run(self.a_status())

    def start_stream(self) -> dict:
        return _run(self.a_start_stream())

    def stop_stream(self) -> dict:
        return _run(self.a_stop_stream())

    # Async forms. The engine dispatches actions from INSIDE a running event
    # loop, where asyncio.run() raises "cannot be called from a running event
    # loop" — so a Play containing an OBS step must use these, not the sync
    # wrappers above.
    async def a_start_stream(self) -> dict:
        return await self._call("StartStream")

    async def a_stop_stream(self) -> dict:
        return await self._call("StopStream")

    async def a_set_scene(self, scene: str) -> dict:
        return await self._set_scene_async((scene or "").strip())

    async def a_status(self) -> dict:
        out = {"reachable": False, "streaming": False, "recording": False,
               "reconnecting": False, "timecode": "", "congestion": 0.0,
               "exe_found": bool(self.find_exe()), "error": ""}
        try:
            data = await self._status_async()
        except OBSError as e:
            out["error"] = str(e)
            return out
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
            return out
        out.update(data)
        return out

    async def _status_async(self) -> dict:
        c = await _Client.connect(self.host, self.port, self.password)
        try:
            d = await c.request("GetStreamStatus")
            return {
                "reachable": True,
                "streaming": bool(d.get("outputActive")),
                "reconnecting": bool(d.get("outputReconnecting")),
                "timecode": str(d.get("outputTimecode") or ""),
                "congestion": float(d.get("outputCongestion") or 0.0),
                "skipped_frames": int(d.get("outputSkippedFrames") or 0),
                "total_frames": int(d.get("outputTotalFrames") or 0),
            }
        finally:
            await c.close()

    # -- commands ----------------------------------------------------------
    async def _call(self, rtype: str) -> dict:
        c = await _Client.connect(self.host, self.port, self.password)
        try:
            await c.request(rtype)
        finally:
            await c.close()
        return {"ok": True, "request": rtype}

    def set_scene(self, scene: str) -> dict:
        """Switch OBS to a named scene."""
        if not (scene or "").strip():
            raise OBSError("no scene name given")
        return _run(self.a_set_scene(scene))

    async def _set_scene_async(self, scene: str) -> dict:
        if not (scene or "").strip():
            raise OBSError("no scene name given")
        c = await _Client.connect(self.host, self.port, self.password)
        try:
            # OBS is case-sensitive about scene names but users are not, so an
            # exact miss falls back to a case-insensitive match rather than
            # failing a Play at 2am.
            names = await self._scene_names(c)
            if scene not in names:
                match = next((n for n in names if n.lower() == scene.lower()), "")
                if match:
                    scene = match
                else:
                    raise OBSError(
                        f"OBS has no scene called {scene!r}. It has: "
                        + (", ".join(names) if names else "none"))
            await c.request("SetCurrentProgramScene", {"sceneName": scene})
        finally:
            await c.close()
        return {"ok": True, "scene": scene}

    async def _scene_names(self, c: "_Client") -> list[str]:
        try:
            data = await c.request("GetSceneList")
        except OBSError:
            return []
        return [s.get("sceneName", "") for s in (data.get("scenes") or [])
                if s.get("sceneName")]

    def scenes(self) -> list[str]:
        """The scene names OBS currently has, for the UI to offer."""
        try:
            return _run(self._scenes_async())
        except Exception:
            return []

    async def _scenes_async(self) -> list[str]:
        c = await _Client.connect(self.host, self.port, self.password)
        try:
            return await self._scene_names(c)
        finally:
            await c.close()

    # -- the rescue --------------------------------------------------------
    def rescue(self, platform: Any = None, wait: float = RELAUNCH_WAIT) -> dict:
        """Fix a dead stream, whatever state OBS is in.

        Order matters: try to talk to it first, because relaunching a healthy
        OBS that merely stopped the stream would kill a working recording and
        lose whatever scene state it had.
        """
        from pcrituals.platform import get_platform

        plat = platform or get_platform()
        steps: list[str] = []

        before = self.status()
        if before["reachable"]:
            steps.append("OBS is running")
            if before["streaming"]:
                return {"ok": True, "did": "none", "steps": steps,
                        "message": "The stream is already live.", "status": before}
            step = self.start_stream()
            steps.append("told OBS to start streaming")
            after = self.status()
            return {"ok": bool(after["streaming"]), "did": "start",
                    "steps": steps, "message": "Stream live again." if after["streaming"]
                               else "OBS accepted the command but isn't streaming yet.",
                    "status": after}

        # Unreachable. Either it crashed or it is hung.
        steps.append("OBS did not answer on the WebSocket")
        if self._process_running(plat):
            try:
                plat.close_application(OBS_PROCESS)
                steps.append("closed an unresponsive OBS")
                time.sleep(3.0)
            except Exception as e:  # noqa: BLE001
                steps.append(f"could not close the hung OBS ({e})")

        exe = self.find_exe()
        if not exe:
            raise OBSError("Couldn't find OBS on this PC. Set its location in "
                           "Settings, then try again.")
        plat.launch(exe)
        steps.append("launched OBS")

        deadline = time.time() + wait
        while time.time() < deadline:
            if self.status()["reachable"]:
                steps.append("OBS is answering again")
                break
            time.sleep(2.0)
        else:
            raise OBSError("OBS started but its WebSocket never came up. Check "
                           "that Tools -> WebSocket Server Settings is enabled.")

        self.start_stream()
        steps.append("told OBS to start streaming")
        after = self.status()
        return {"ok": bool(after["streaming"]), "did": "relaunch",
                "steps": steps,
                "message": "OBS restarted and the stream is live." if after["streaming"]
                           else "OBS restarted but isn't streaming yet.",
                "status": after}

    def _process_running(self, plat: Any) -> bool:
        try:
            return bool(plat.list_running_processes(OBS_PROCESS))
        except Exception:
            return False
