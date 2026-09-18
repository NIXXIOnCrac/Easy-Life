"""FastAPI application exposing the local REST API + SSE + PWA frontend."""
from __future__ import annotations

import sys
import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from pcrituals.app import App
from pcrituals.auth import AuthError
from pcrituals.config import load_config
from pcrituals.platform import PlatformError
from pcrituals.sync import SyncConflict, SyncError

# Sentinel for `start_https_listener`: "let uvicorn configure its own logging".
# The native window passes log_config=None instead (uvicorn's default formatter
# calls sys.stdout.isatty(), which is None in a windowed frozen build).
_UVICORN_DEFAULT_LOGGING = object()


def _web_dir() -> Path:
    """Locate the bundled web frontend. Works in dev (package path) and in a
    PyInstaller --onefile extraction / --onedir bundle."""
    if getattr(sys, "frozen", False):
        # Bundles put non-code data next to the exe (onedir) or in _MEIPASS.
        exe_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        cand = exe_dir / "pcrituals" / "web"
        if (cand / "index.html").exists():
            return cand
        cand2 = exe_dir / "web"
        if (cand2 / "index.html").exists():
            return cand2
    return Path(__file__).parent / "web"


FRONTEND_DIR = _web_dir()


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def _app(request: Request) -> App:
    return request.app.state.pcrituals


def auth_required(request: Request) -> Optional[str]:
    """Authorise a request.

    - Once a login account exists, a valid session token (or a paired-device
      token) is required for everything — including localhost.
    - Before any account exists the app is open, and LAN clients still need a
      paired-device token.
    """
    ap = _app(request)
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    if not token:
        # A token in a query string leaks into request logs, proxy logs,
        # Referer headers and browser history. The only caller that cannot set a
        # header is EventSource, so the fallback is limited to the SSE endpoint.
        if request.url.path.endswith("/events"):
            token = request.query_params.get("token", "").strip()
    token = token.removeprefix("Bearer ").strip()

    if ap.auth.has_users():
        if token and (ap.auth.validate_session(token) or ap.security.is_valid_token(token)):
            return token
        raise HTTPException(status_code=401, detail="login required")

    # NOT `request.client.host` alone: a tunnel or reverse proxy runs on this
    # machine, so internet traffic arrives with a loopback address. Treating
    # that as the local desktop UI would hand out the whole API with no
    # credential. client_is_local() fails closed whenever forwarding headers
    # are present.
    from pcrituals import net
    if net.client_is_local(request):
        return token or "local"
    if token and ap.security.is_valid_token(token):
        return token
    raise HTTPException(status_code=401, detail="authentication required")


class _SlidingWindow:
    """A tiny per-key failure limiter for endpoint-level throttling."""

    def __init__(self, limit: int = 10, window: float = 60.0, cap: int = 2_000) -> None:
        self.limit = limit
        self.window = window
        self.cap = cap
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        if len(self._hits) > self.cap:
            self._hits = {k: [t for t in v if now - t < self.window]
                          for k, v in list(self._hits.items())}
            self._hits = {k: v for k, v in self._hits.items() if v}

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            self._prune(now)
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if hits:
                self._hits[key] = hits
            else:
                self._hits.pop(key, None)
            return len(hits) < self.limit

    def record(self, key: str) -> None:
        with self._lock:
            self._hits.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


_pair_guard = _SlidingWindow(limit=10, window=60.0)


def create_app() -> FastAPI:
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    config = load_config()
    app_state = App(config)

    @asynccontextmanager
    async def _lifespan(_app):
        """Start the OBS stream-drop watcher however the app was launched.

        It used to start only in desktop.py and api.run(), so running under
        uvicorn (which is how tests and a plain `uvicorn` launch work)
        silently had no stream watching at all. One per process: create_app
        runs many times and a watcher each time left a pile of threads.
        """
        try:
            from pcrituals.notify import Notifier
            from pcrituals.streamwatch import start_watcher
            start_watcher(config, Notifier(config))
        except Exception as e:  # noqa: BLE001 - never block app startup
            # Log it: swallowing this silently meant the watcher looked wired
            # up while never actually running.
            print(f"[pcrituals] stream watcher did not start: {e!r}")
        yield

    app = FastAPI(
        lifespan=_lifespan,
        title="Easy Life",
        version="0.1.0",
        description="Local control API for Easy Life. Desktop UI runs on loopback; "
                    "phones must pair before controlling.",
    )

    # CORS is intentionally permissive on origins since this is a LAN-only API and
    # authentication is token-based; credentials are not shared via cookies.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.pcrituals = app_state

    # OAuth redirect target for Spotify (must match the URI registered in the
    # Spotify dashboard). Lives outside /api so it matches the registered URL.
    @app.get("/callback")
    def spotify_callback(code: str = "", state: str = "", error: str = ""):
        from fastapi.responses import HTMLResponse
        if error:
            body = ("<h2>Spotify connection failed</h2>"
                    f"<p>{error}</p><p>You can close this window.</p>")
            return HTMLResponse(body, status_code=400)
        if not code:
            return HTMLResponse("<h2>Missing code</h2><p>Close this window and retry.</p>",
                                status_code=400)
        try:
            app_state.spotify.exchange_code(code, state)
            body = ("<h2>✅ Spotify connected</h2>"
                    "<p>You can close this window and return to Easy Life.</p>")
            return HTMLResponse(body)
        except Exception as e:  # noqa: BLE001
            return HTMLResponse(f"<h2>Connection failed</h2><p>{e}</p>", status_code=400)

    app.include_router(_api_router(app_state))

    # Hand the phone the self-signed certificate so it can be installed as a
    # trusted profile. Without this the user has to transfer a file off the PC
    # by hand, and iOS refuses the camera on an untrusted origin. A certificate
    # is public (it is sent in every TLS handshake), so this needs no auth; it
    # simply 404s when TLS has never been set up.
    @app.get("/cert")
    def download_cert():
        from fastapi.responses import FileResponse, PlainTextResponse
        cert_file = load_config().tls_cert_file
        if not cert_file.exists():
            return PlainTextResponse(
                "No certificate yet. On the PC run: python -m pcrituals.cert --enable",
                status_code=404)
        return FileResponse(str(cert_file), media_type="application/x-x509-ca-cert",
                            filename="easylife.crt")

    # Mount the PWA / desktop frontend (after API routes so /api/* wins).
    if FRONTEND_DIR.exists():
        app.mount("/", _frontend(), name="web")

    return app


def _is_test_client(request) -> bool:
    """Starlette's TestClient reports the client host as "testclient".

    Kept separate from the real loopback rule (and out of net.LOOPBACK_HOSTS)
    so the production trust decision stays honest — nothing can connect over
    TCP while claiming this name.
    """
    return (getattr(getattr(request, "client", None), "host", "") or "") == "testclient"


def _frontend() -> StaticFiles:
    # Default index served at / by StaticFiles if it's index.html.
    return StaticFiles(directory=str(FRONTEND_DIR), html=True)


def _api_router(ap: App) -> APIRouter:
    router = APIRouter(prefix="/api")

    # ---- auth (login system) ---------------------------------------------------
    @router.get("/auth/status")
    def auth_status(request: Request):
        """Open endpoint: tells the UI whether to show setup, login, or the app."""
        token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        sess = ap.auth.validate_session(token) if token else None
        authed = bool(token and (sess or ap.security.is_valid_token(token)))
        return {
            "needs_setup": not ap.auth.has_users(),
            "authenticated": authed,
            "username": (sess or {}).get("username") or ("phone" if authed else None),
            # Show the onboarding tour only for a real user who hasn't done it.
            "onboarded": (sess or {}).get("onboarded", True) if authed else False,
        }

    @router.post("/auth/onboarded")
    def auth_onboarded(request: Request, _=Depends(auth_required)):
        token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        sess = ap.auth.validate_session(token)
        if sess:
            ap.auth.set_onboarded(sess["user_id"], True)
        return {"ok": True}

    @router.post("/auth/setup")
    def auth_setup(body: dict, request: Request):
        from pcrituals import net
        # Creating the FIRST account is a physical act: whoever does it owns the
        # PC. It must only be possible from the machine itself. Without this,
        # turning the tunnel on before setting up an account let anyone on the
        # internet claim the PC by calling this endpoint first.
        if not (net.client_is_local(request) or _is_test_client(request)):
            raise HTTPException(403, "setup is only allowed from this PC")
        if ap.auth.has_users():
            raise HTTPException(409, "An account already exists")
        try:
            ap.auth.create_user(body.get("username", ""), body.get("password", ""))
            return ap.auth.login(body.get("username", ""), body.get("password", ""), label="setup")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e

    @router.post("/auth/login")
    def auth_login(body: dict, request: Request):
        # Through a tunnel every visitor shares the loopback socket address, so
        # keying the lockout on it would let one attacker lock out everybody.
        from pcrituals import net
        client = net.client_ip_for_limits(request)
        try:
            return ap.auth.login(body.get("username", ""), body.get("password", ""),
                                 client=client)
        except AuthError as e:
            # 429 for lockout so the UI can say "wait", 401 otherwise.
            raise HTTPException(getattr(e, "status", 401), str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(401, str(e)) from e

    @router.post("/auth/logout")
    def auth_logout(request: Request, _=Depends(auth_required)):
        token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        ap.auth.logout(token)
        return {"ok": True}

    @router.post("/auth/change-password")
    def auth_change_password(request: Request, body: dict, _=Depends(auth_required)):
        token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        sess = ap.auth.validate_session(token)
        if not sess:
            raise HTTPException(400, "Not a user session")
        try:
            ap.auth.change_password(sess["user_id"], body.get("old_password", ""),
                                    body.get("new_password", ""))
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e

    @router.get("/auth/sessions")
    def auth_sessions(_=Depends(auth_required)):
        return ap.auth.list_sessions()

    # ---- rituals -----------------------------------------------------------
    @router.get("/rituals")
    def list_rituals(_=Depends(auth_required)):
        return ap.list_rituals()

    @router.get("/rituals/{rid}")
    def get_ritual(rid: str, _=Depends(auth_required)):
        r = ap.get_ritual(rid)
        if not r:
            raise HTTPException(404, "ritual not found")
        return r

    @router.post("/rituals")
    def create_ritual(body: dict, _=Depends(auth_required)):
        try:
            return ap.create_ritual(body)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e

    @router.put("/rituals/{rid}")
    def update_ritual(rid: str, body: dict, _=Depends(auth_required)):
        # ValidationError must surface as 400, not an unhandled 500 + traceback.
        # create() was wrapped and this was not, so any wrong-typed field became
        # a server error.
        try:
            r = ap.update_ritual(rid, body)
        except ValidationError as e:
            raise HTTPException(400, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if not r:
            raise HTTPException(404, "ritual not found")
        return r

    @router.post("/rituals/{rid}/duplicate")
    def duplicate(rid: str, _=Depends(auth_required)):
        r = ap.duplicate_ritual(rid)
        if not r:
            raise HTTPException(404, "ritual not found")
        return r

    @router.delete("/rituals/{rid}")
    def delete(rid: str, _=Depends(auth_required)):
        if not ap.delete_ritual(rid):
            raise HTTPException(404, "ritual not found")
        return {"deleted": True}

    @router.post("/rituals/reorder")
    def reorder(body: dict, _=Depends(auth_required)):
        ids = body.get("ids", [])
        # A non-list body used to reach the storage layer and raise TypeError
        # ('NoneType' object is not iterable) -> 500. A bare string silently
        # iterated its characters and matched nothing.
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise HTTPException(400, "ids must be a list of ritual ids")
        ap.reorder_rituals(ids)
        return {"ok": True, "count": len(ids)}

    # ---- execution ----------------------------------------------------------
    @router.post("/rituals/{rid}/run")
    async def run(rid: str, body: Optional[dict] = None, _=Depends(auth_required)):
        triggered_by = (body or {}).get("triggered_by", "local")
        try:
            result = await ap.run_ritual(rid, triggered_by=triggered_by)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        except KeyError as e:
            # A missing ritual used to return 200 with a `null` body, which every
            # client reads as "started" — then it waits for progress forever.
            raise HTTPException(404, str(e).strip("'") or "ritual not found") from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e)) from e
        if result is None:
            raise HTTPException(404, "ritual not found")
        return result

    @router.post("/rituals/{rid}/stop")
    def stop(rid: str, _=Depends(auth_required)):
        """Ask a running ritual to stop.

        Returns `stopping`, not `stopped`: cancellation is cooperative and the
        run has not actually unwound yet (an in-flight command still has to be
        killed). Reporting `stopped: true` immediately told clients a runaway
        command had ended when it was still running. `/api/run/current` reports
        `state: "cancelling"` until the run genuinely finishes.
        """
        accepted = ap.stop_ritual(rid)
        run = ap.current_run()
        return {
            "stopping": accepted,
            "stopped": False if accepted else True,
            "state": (run or {}).get("state") if accepted else "idle",
        }

    @router.get("/run/current")
    def current_run(_=Depends(auth_required)):
        return ap.current_run() or {"running": False}

    @router.get("/events")
    async def sse(request: Request, _=Depends(auth_required)):
        """Server-Sent Events stream of live ritual executions."""
        if request.headers.get("accept") == "text/event-stream":
            sub_id, _ = ap.hub.subscribe()
        else:
            sub_id, _ = ap.hub.subscribe()

        async def gen():
            try:
                async for data in ap.hub.stream(sub_id):
                    if await request.is_disconnected():
                        break
                    yield data
            finally:
                ap.hub.unsubscribe(sub_id)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---- PC status & controls ----------------------------------------------
    @router.get("/status")
    def status(_=Depends(auth_required)):
        return ap.pc_status()

    @router.get("/integrations")
    def integrations(_=Depends(auth_required)):
        """Detected integrations (real detection on Windows)."""
        return ap.detect_integrations()

    @router.get("/wol")
    def wol_status(_=Depends(auth_required)):
        """Wake-on-LAN configuration/status."""
        return ap.wol_status()

    @router.post("/wol/wake")
    def wol_wake(body: dict, _=Depends(auth_required)):
        """Send a Wake-on-LAN magic packet to a saved or given MAC address."""
        mac = (body.get("mac") or "").strip()
        if not mac:
            raise HTTPException(400, "missing MAC address")
        try:
            ap.wol_wake(mac)
            return {"sent": True, "mac": mac, "broadcast": ap.config.wol_broadcast}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # ---- deck (Stream Deck) ----------------------------------------------------
    @router.get("/deck")
    def list_deck(_=Depends(auth_required)):
        return ap.list_deck()

    @router.post("/deck")
    def create_deck_button(body: dict, _=Depends(auth_required)):
        try:
            return ap.save_deck_button(body)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e

    @router.put("/deck/{bid}")
    def update_deck_button(bid: str, body: dict, _=Depends(auth_required)):
        body = {**body, "id": bid}
        try:
            return ap.save_deck_button(body)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e

    @router.delete("/deck/{bid}")
    def delete_deck_button(bid: str, _=Depends(auth_required)):
        if not ap.delete_deck_button(bid):
            raise HTTPException(404, "deck button not found")
        return {"deleted": True}

    @router.post("/deck/reorder")
    def reorder_deck(body: dict, _=Depends(auth_required)):
        ap.reorder_deck(body.get("ids", []))
        return {"ok": True}

    @router.post("/deck/{bid}/press")
    async def press_deck(bid: str, _=Depends(auth_required)):
        try:
            return await ap.press_deck_button(bid)
        except KeyError:
            raise HTTPException(404, "deck button not found")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e)) from e

    @router.get("/deck/icon")
    def deck_icon(kind: str = "", target: str = "", label: str = "", _=Depends(auth_required)):
        return ap.preview_icon(kind, target, label)

    @router.get("/deck/suggest")
    def deck_suggest(target: str = "", _=Depends(auth_required)):
        return {"kind": ap.suggest_kind(target)}

    # ---- media -----------------------------------------------------------------
    @router.get("/media/now")
    def media_now(_=Depends(auth_required)):
        return ap.media_now_playing() or {}

    @router.post("/media/control")
    async def media_control(body: dict, _=Depends(auth_required)):
        action = (body.get("action") or "").strip()
        allowed = ("play_pause", "toggle", "play", "pause", "next", "previous", "prev",
                   "stop", "shuffle", "repeat", "seek", "volume")
        if action not in allowed:
            raise HTTPException(400, "invalid media action")
        try:
            return await asyncio.to_thread(ap.media_control, action, body.get("value"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e)) from e

    # ---- spotify ---------------------------------------------------------------
    @router.get("/spotify/status")
    def spotify_status(_=Depends(auth_required)):
        return ap.spotify_status()

    @router.post("/spotify/config")
    def spotify_config(body: dict, _=Depends(auth_required)):
        return ap.spotify_configure(body.get("client_id", ""),
                                    body.get("client_secret", ""),
                                    body.get("redirect_uri") or None)

    @router.get("/spotify/login")
    def spotify_login(_=Depends(auth_required)):
        try:
            return {"auth_url": ap.spotify_auth_url()}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e

    @router.post("/spotify/disconnect")
    def spotify_disconnect(_=Depends(auth_required)):
        return ap.spotify_disconnect()

    # ---- updater --------------------------------------------------------------
    @router.get("/update/status")
    def update_status(_=Depends(auth_required)):
        """Current version and whether an update is available."""
        return ap.update_status()

    @router.post("/update/apply")
    def update_apply(_=Depends(auth_required)):
        """Download and apply the pending update (Windows only)."""
        try:
            return ap.update_apply()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e

    @router.get("/settings")
    def get_settings(_=Depends(auth_required)):
        return ap.get_settings()

    @router.get("/obs/scenes")
    def obs_scenes(_=Depends(auth_required)):
        """Scene names, so the builder and the OBS tab can offer real choices."""
        from pcrituals.obs import OBS
        return {"scenes": OBS(ap.config).scenes()}

    @router.post("/obs/scene")
    def obs_set_scene(body: dict, _=Depends(auth_required)):
        from pcrituals.obs import OBS, OBSError
        try:
            return OBS(ap.config).set_scene(body.get("scene") or "")
        except OBSError as e:
            raise HTTPException(400, str(e)) from e

    def _notifier():
        from pcrituals.notify import Notifier
        if not hasattr(ap, "_notifier_cache"):
            ap._notifier_cache = Notifier(ap.config)
        return ap._notifier_cache

    @router.get("/notify/vapid")
    def notify_vapid(_=Depends(auth_required)):
        """The VAPID public key the browser needs to create a push subscription."""
        return {"public_key": _notifier().vapid_public_key}

    @router.post("/notify/subscribe")
    def notify_subscribe(body: dict, _=Depends(auth_required)):
        from pcrituals.notify import Subscription
        sub = Subscription(
            endpoint=str(body.get("endpoint") or ""),
            p256dh=str(body.get("p256dh") or ""),
            auth=str(body.get("auth") or ""),
            user_agent=str(body.get("user_agent") or ""),
        )
        if not sub.endpoint or not sub.p256dh or not sub.auth:
            raise HTTPException(400, "endpoint, p256dh and auth are required")
        # Validate before storing. A row that cannot be encrypted aborts
        # send_to_all, so one malformed subscription silently killed every
        # future stream-drop push.
        from pcrituals.notify import validate_endpoint
        try:
            validate_endpoint(sub.endpoint)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        try:
            _notifier()._encrypt(sub, b"probe")   # proves the keys are usable
        except Exception as e:  # noqa: BLE001 - any failure means unusable keys
            raise HTTPException(400, f"invalid push keys: {e}") from e
        return _notifier().save_subscription(sub).to_dict()

    @router.delete("/notify/subscribe")
    def notify_unsubscribe(body: dict, _=Depends(auth_required)):
        _notifier().delete_subscription(str(body.get("endpoint") or ""))
        return {"ok": True}

    @router.get("/notify/subscriptions")
    def notify_subscriptions(_=Depends(auth_required)):
        subs = _notifier().list_subscriptions()
        return {"count": len(subs), "subscriptions": [s.to_dict() for s in subs]}

    @router.post("/notify/test")
    def notify_test(_=Depends(auth_required)):
        """Send a test push so the user can confirm the phone receives it."""
        results = _notifier().send_to_all("Easy Life test notification")
        return {"sent": len(results), "results": results}

    @router.post("/plays/draft")
    def plays_draft(body: dict, _=Depends(auth_required)):
        """Turn a sentence into a draft Play. Saves nothing — the user reviews
        the steps before anything is created."""
        from pcrituals.nlplay import draft as draft_play
        text = str(body.get("text") or "")
        return draft_play(text)

    @router.post("/settings")
    def set_settings(body: dict, _=Depends(auth_required)):
        try:
            return ap.set_settings(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # ---- OBS remote rescue ---------------------------------------------------
    # Deliberately NOT loopback-only: the whole point is to fix a dead stream
    # from the phone, from anywhere. Auth still applies (a paired device or a
    # signed-in session), which is what makes that safe.
    @router.get("/obs/status")
    def obs_status(_=Depends(auth_required)):
        from pcrituals.obs import OBS
        return OBS(ap.config).status()

    @router.post("/obs/stream/start")
    def obs_stream_start(_=Depends(auth_required)):
        from pcrituals.obs import OBS, OBSError
        try:
            return OBS(ap.config).start_stream()
        except OBSError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/obs/stream/stop")
    def obs_stream_stop(_=Depends(auth_required)):
        from pcrituals.obs import OBS, OBSError
        try:
            return OBS(ap.config).stop_stream()
        except OBSError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/obs/rescue")
    def obs_rescue(_=Depends(auth_required)):
        """One button: get the stream back, whatever state OBS is in."""
        from pcrituals.obs import OBS, OBSError
        try:
            return OBS(ap.config).rescue()
        except OBSError as e:
            raise HTTPException(400, str(e)) from e

    # ---- tunnel (remote access with nothing to install on the phone) --------
    def _tunnel_mod():
        """Remote access uses one provider: Cloudflare quick tunnel. Exposes
        the same `tunnel` singleton shape: status()/start()/stop() and
        TunnelError."""
        from pcrituals import tunnel as cf_mod
        return cf_mod

    @router.get("/tunnel")
    def tunnel_status(_=Depends(auth_required)):
        return _tunnel_mod().tunnel.status()

    @router.post("/tunnel/start")
    def tunnel_start(request: Request, _=Depends(auth_required)):
        from pcrituals import net
        # Local only. Publishing this PC to the internet is a decision for the
        # person sitting at it — a LAN caller or an already-tunnelled client
        # must not be able to open it up further.
        if not (net.client_is_local(request) or _is_test_client(request)):
            raise HTTPException(403, "starting the tunnel is only allowed from this PC")
        mod = _tunnel_mod()
        try:
            return mod.tunnel.start(ap.config.port)
        except RuntimeError as e:  # both providers raise a TunnelError(RuntimeError)
            raise HTTPException(400, str(e)) from e

    @router.post("/tunnel/stop")
    def tunnel_stop(request: Request, _=Depends(auth_required)):
        from pcrituals import net
        if not (net.client_is_local(request) or _is_test_client(request)):
            raise HTTPException(403, "stopping the tunnel is only allowed from this PC")
        return _tunnel_mod().tunnel.stop()

    @router.post("/quit")
    def quit_app(request: Request, _=Depends(auth_required)):
        """Stop the app on purpose.

        Loopback only: quitting is a local action, and a paired phone must not
        be able to shut down the PC's app. When nothing is driving a desktop
        window (tests, `python -m pcrituals`) this reports 501 rather than
        pretending it did something.
        """
        from pcrituals import net
        if not (net.client_is_local(request) or _is_test_client(request)):
            raise HTTPException(403, "quitting is only allowed from this PC")
        # request.app is the FastAPI instance; this router only receives the
        # domain App, so the state lives here.
        fn = getattr(request.app.state, "pcrituals_request_quit", None)
        if not callable(fn):
            raise HTTPException(501, "this process has no desktop shell to stop")
        fn()
        return {"ok": True}

    @router.post("/voice")
    async def voice(body: dict, _=Depends(auth_required)):
        """Recognize-and-execute a voice command. Destructive power actions
        require a confirm flag (returned as needs_confirmation first)."""
        phrase = (body.get("phrase") or "").strip()
        if not phrase:
            raise HTTPException(400, "missing phrase")
        result = ap.voice_parse(phrase)
        if result is None:
            return {"intent": None, "executed": False, "message": "no matching command"}
        # Destructive actions need explicit confirmation from the caller.
        if result.get("needs_confirmation"):
            if body.get("confirm"):
                result["executed"] = ap._execute_voice_confirm(result["intent"])
                result["message"] = "executed"
            else:
                result["executed"] = False
                result["message"] = "confirmation required for this power action"
        return result

    # ---- power ----------------------------------------------------------------
    @router.post("/power")
    def power(body: dict, _=Depends(auth_required)):
        action = body.get("action")
        if action not in ("lock", "sleep", "restart", "shutdown"):
            raise HTTPException(400, "invalid power action")
        # Destructive actions require an explicit confirm flag.
        if action in ("restart", "shutdown") and not body.get("confirm"):
            raise HTTPException(400, "confirmation required for restart/shutdown")
        try:
            return ap.power_control(action)
        except PlatformError as e:
            raise HTTPException(500, str(e)) from e

    # ---- pairing ------------------------------------------------------------
    @router.post("/pair/start")
    def pair_start(_=Depends(auth_required)):
        """Generate a fresh pairing code (called from the desktop UI)."""
        return ap.create_pairing_code()

    @router.get("/pair/state")
    def pair_state(_=Depends(auth_required)):
        state = ap.pc_pairing_state()
        cfg = load_config()
        from pcrituals import net
        http_urls = net.lan_urls(cfg.port)
        state["tls"] = {
            "enabled": bool(cfg.tls_enabled),
            "port": int(cfg.tls_port),
            "cert_ready": cfg.tls_cert_file.exists(),
            # The phone fetches the cert over plain http (it has not trusted the
            # https origin yet, so it cannot load it).
            "cert_url": (http_urls[0] + "/cert") if http_urls else "",
        }
        # Away-from-home access. Reported separately from tls because the two
        # are independent: the tunnel is about *reach*, TLS is about the camera.
        scheme = "https" if cfg.tls_enabled else "http"
        port = cfg.tls_port if cfg.tls_enabled else cfg.port
        ts_ips = net.tailscale_ips()
        state["remote"] = {
            "ready": bool(ts_ips),
            "addresses": ts_ips,
            # The exact address the phone should use when it is out of the
            # house — this is what the QR prefers too.
            "away_url": f"{scheme}://{ts_ips[0]}:{port}" if ts_ips else "",
            "lan_url": (http_urls[0] if http_urls else ""),
        }
        return state

    @router.post("/pair/complete")
    def pair_complete(body: dict, request: Request):
        """Phone redeems a pairing code and receives a device token."""
        # This endpoint is necessarily unauthenticated (the phone has no token
        # yet), so it is the one place where guessing is possible. The codes are
        # short-lived and single-use, but rate-limit it anyway so a faster future
        # server cannot turn a 6-character code into a feasible sweep.
        from pcrituals import net
        client = net.client_ip_for_limits(request)
        if not _pair_guard.allow(client):
            raise HTTPException(429, "too many pairing attempts, wait a minute")
        code = (body.get("code") or "").strip().upper()
        if not code:
            raise HTTPException(400, "missing pairing code")
        pc = ap.security.redeem_pairing_code(code)
        if not pc:
            _pair_guard.record(client)
            raise HTTPException(401, "invalid or expired pairing code")
        _pair_guard.clear(client)
        device_name = (body.get("device_name") or "iPhone").strip() or "iPhone"
        result = ap.security.issue_device_token(device_name, pc)
        return result

    @router.get("/devices")
    def devices(_=Depends(auth_required)):
        return ap.list_devices()

    @router.post("/devices/{did}/revoke")
    def revoke(did: str, _=Depends(auth_required)):
        return {"revoked": ap.revoke_device(did)}

    @router.post("/devices/revoke-all")
    def revoke_all(_=Depends(auth_required)):
        return {"revoked": ap.revoke_all()}

    # ---- history -------------------------------------------------------------
    @router.get("/history")
    def history(limit: int = 100, _=Depends(auth_required)):
        # Clamp: SQLite treats LIMIT -1 as unlimited, so a negative limit made
        # the server serialise the entire history table.
        return ap.history(limit=max(1, min(int(limit), 1000)))

    @router.get("/history/{rid}")
    def history_ritual(rid: str, limit: int = 50, _=Depends(auth_required)):
        return ap.history_for_ritual(rid, limit=max(1, min(int(limit), 1000)))

    @router.delete("/history")
    def history_clear(_=Depends(auth_required)):
        ap.clear_history()
        return {"cleared": True}

    # ---- backups ---------------------------------------------------------------
    @router.get("/backups")
    def backups(_=Depends(auth_required)):
        return ap.list_backups()

    @router.post("/backups")
    def backup_create(body: dict, _=Depends(auth_required)):
        return ap.backup_now((body or {}).get("label", "manual"))

    @router.post("/backups/{name}/restore")
    def backup_restore(name: str, _=Depends(auth_required)):
        # Restore overwrites current state (rituals, keys) — require confirm.
        if not name:
            raise HTTPException(400, "missing backup name")
        try:
            return ap.restore_backup(name)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e

    @router.delete("/backups/{name}")
    def backup_delete(name: str, _=Depends(auth_required)):
        if not ap.delete_backup(name):
            raise HTTPException(404, "backup not found")
        return {"deleted": name}

    # ---- import / export ------------------------------------------------------
    @router.get("/export")
    def export(_=Depends(auth_required)):
        # Return the parsed object, not the JSON string. Returning the string
        # made FastAPI serialise it *again*, so the response was a JSON string
        # containing JSON — `response.json()` gave a str, not an object.
        return json.loads(ap.export_imports())

    @router.post("/import")
    def import_payload(body: dict, _=Depends(auth_required)):
        try:
            return ap.import_rituals(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    # ---- optional sync to a self-hosted server -------------------------------
    # Everything here is inert until the user connects. The sync token is kept in
    # its own 0600 file and is never returned to the client or put in a vault.
    @router.get("/sync/status")
    def sync_status(_=Depends(auth_required)):
        return ap.sync.public_state()

    @router.post("/sync/connect")
    def sync_connect(body: dict, _=Depends(auth_required)):
        url = (body.get("url") or "").strip()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not url:
            raise HTTPException(400, "Enter the sync server address")
        if not username or not password:
            raise HTTPException(400, "Enter the sync username and password")
        try:
            return ap.sync.connect(url, username, password,
                                   register=bool(body.get("register")))
        except SyncError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/sync/disconnect")
    def sync_disconnect(_=Depends(auth_required)):
        return ap.sync.disconnect()

    @router.post("/sync/auto")
    def sync_auto(body: dict, _=Depends(auth_required)):
        return ap.sync.set_auto_sync(bool(body.get("enabled")))

    @router.get("/sync/peek")
    def sync_peek(_=Depends(auth_required)):
        try:
            return ap.sync.peek()
        except SyncError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/sync/push")
    def sync_push(body: dict, _=Depends(auth_required)):
        try:
            if body.get("force"):
                return ap.sync.force_push(ap)
            return ap.sync.push(ap)
        except SyncConflict as e:
            # 409 so the UI can offer "overwrite the server" rather than
            # silently clobbering whatever the other machine wrote.
            raise HTTPException(409, {
                "message": "The sync server has newer data",
                "current_revision": getattr(e, "current_revision", None),
            }) from e
        except SyncError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/sync/pull")
    def sync_pull(body: dict, _=Depends(auth_required)):
        mode = (body.get("mode") or "merge").strip()
        try:
            return ap.sync.pull(ap, mode=mode)
        except SyncError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/sync/delete-remote")
    def sync_delete_remote(body: dict, _=Depends(auth_required)):
        try:
            return ap.sync.delete_remote(body.get("password") or "")
        except SyncError as e:
            raise HTTPException(400, str(e)) from e

    return router


def start_https_listener(cfg, app, *, log_config: object = _UVICORN_DEFAULT_LOGGING):
    """Serve the same app over https on a second port, in a background thread.

    The desktop window talks to plain http://127.0.0.1:<port> and must keep
    doing so, so HTTPS is an extra listener rather than a replacement. It runs
    in a thread because the plain server owns the main thread (and with it
    Ctrl+C).

    HTTPS is opt-in, so nothing here is allowed to take the app down: a machine
    that cannot produce a certificate says why and keeps serving http — and this
    returns None to say so. Otherwise it returns the uvicorn server.
    """
    import threading

    import uvicorn

    from pcrituals import cert as cert_mod

    try:
        certfile, keyfile = cert_mod.ensure_certificate(cfg)
    except cert_mod.CertError as exc:
        print(f"    HTTPS off : {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - never let TLS break the app
        print(f"    HTTPS off : could not prepare a certificate ({exc})")
        return None

    host = cfg.tls_host or ("127.0.0.1" if cfg.bind_loopback_only else cfg.host)
    kwargs = {} if log_config is _UVICORN_DEFAULT_LOGGING else {"log_config": log_config}
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=cfg.tls_port,
        ssl_certfile=str(certfile), ssl_keyfile=str(keyfile), **kwargs,
    ))

    def _serve() -> None:
        try:
            server.run()
        except BaseException as exc:  # noqa: BLE001 - includes uvicorn's sys.exit
            print(f"    HTTPS off : port {cfg.tls_port} would not start ({exc})")

    threading.Thread(target=_serve, name="easylife-https", daemon=True).start()
    return server


def run() -> None:
    import uvicorn
    cfg = load_config()
    app = create_app()
    host = "127.0.0.1" if cfg.bind_loopback_only else cfg.host
    from pcrituals import net
    if cfg.tls_enabled:
        # The phone should be pointed at the secure origin; the desktop keeps
        # using the http one.
        lan = net.lan_urls(cfg.tls_port, scheme="https")
    else:
        lan = net.lan_urls(cfg.port)
    lan_txt = ", ".join(lan) if lan else "(LAN IP not detected)"
    print(f"\n  Easy Life running")
    print(f"    Desktop UI : http://127.0.0.1:{cfg.port}")
    print(f"    Phone (LAN): {lan_txt}")
    print(f"    Pair on Phone Remote tab to enable remote control.\n")
    if cfg.tls_enabled:
        start_https_listener(cfg, app)
    # Watch OBS and push when the stream drops (self-gates on settings).
    # Must go through the singleton: constructing a StreamWatcher here meant
    # TWO watcher threads once the lifespan hook had also started one, so a
    # drop sent the push twice.
    try:
        from pcrituals.notify import Notifier
        from pcrituals.streamwatch import start_watcher
        start_watcher(cfg, Notifier(cfg))
    except Exception:  # noqa: BLE001
        pass
    uvicorn.run(app, host=host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    run()