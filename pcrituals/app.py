"""Application service: coordinates storage, security, engine, and live events."""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from pcrituals.config import Config
from pcrituals.engine import RitualRunner, RunState, RunnerRegistry
from pcrituals.models import HistoryEntry, Ritual, RitualStatus, Action
from pcrituals.platform import get_platform
from pcrituals.security import Security
from pcrituals.storage import Store


class LiveEventHub:
    """In-process pub/sub for live execution events (fanned out over SSE)."""

    def __init__(self) -> None:
        self._subs: dict[int, asyncio.Queue] = {}
        self._counter = 0

    def subscribe(self) -> tuple[int, asyncio.Queue]:
        self._counter += 1
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs[self._counter] = q
        return self._counter, q

    def unsubscribe(self, sub_id: int) -> None:
        self._subs.pop(sub_id, None)

    def _broadcast(self, event: dict) -> None:
        dead = []
        for sub_id, q in self._subs.items():
            if q.full():
                # Drop oldest to keep the stream live rather than blocking.
                try:
                    q.get_nowait()
                except Exception:
                    pass
            try:
                q.put_nowait(event)
            except Exception:
                dead.append(sub_id)
        for d in dead:
            self._subs.pop(d, None)

    def on_ritual_event(self, event: dict) -> None:
        self._broadcast({"channel": "ritual", **event})

    def on_generic(self, event: dict) -> None:
        self._broadcast({"channel": "system", **event})

    async def stream(self, sub_id: int):
        q = self._subs.get(sub_id)
        if q is None:
            return
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=15.0)
                # json.dumps with default=str guards against any non-serializable
                # field (e.g. a datetime) silently killing the whole subscriber.
                yield f"data: {json.dumps(item, default=str)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"


def _is_local_obs_host(host: str) -> bool:
    """Only loopback or private literal addresses are allowed for OBS.

    OBS runs on this PC, so there is no legitimate reason to point the app at
    an arbitrary host. Rejecting hostnames and public IPs closes an SSRF
    primitive (an authenticated client could otherwise probe internal hosts).
    """
    import ipaddress
    h = host.strip().lower()
    if h in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


class App:
    """Top-level application object wiring store + security + engine + hub."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = Store(config)
        self.security = Security(config)
        self.platform = get_platform()
        self.registry = RunnerRegistry()
        self.hub = LiveEventHub()
        from pcrituals.media import MediaController
        self.media = MediaController()
        from pcrituals.spotify import SpotifyClient
        self.spotify = SpotifyClient(config)
        from pcrituals.auth import AuthManager
        self.auth = AuthManager(config)
        # Optional link to a self-hosted sync server. Inert until the user
        # connects, so a default install never contacts anything external.
        from pcrituals.sync_link import SyncLink
        self.sync = SyncLink(config)
        self._history_lock = asyncio.Lock()

    # ---- ritual CRUD (business logic) ------------------------------------
    def list_rituals(self) -> list[dict]:
        return [r.model_dump() for r in self.store.list_rituals()]

    def get_ritual(self, ritual_id: str) -> Optional[dict]:
        r = self.store.get_ritual(ritual_id)
        return r.model_dump() if r else None

    def create_ritual(self, data: dict) -> dict:
        ritual = Ritual.model_validate(data)
        self.guard_ritual(ritual)
        self.store.save_ritual(ritual)
        return ritual.model_dump()

    def update_ritual(self, ritual_id: str, data: dict) -> Optional[dict]:
        existing = self.store.get_ritual(ritual_id)
        if not existing:
            return None
        # `id` and `created_at` come from the URL/record, never from the body.
        # Previously a body carrying a different id was merged in and then saved
        # under THAT id, so a request addressed to ritual A silently rewrote
        # ritual B (and a client that echoed a fetched object back could hit
        # this by accident).
        clean = {k: v for k, v in (data or {}).items() if k not in ("id", "created_at")}
        # Merge as plain data and validate once. Merging with model_copy() and
        # then dumping emitted PydanticSerializationUnexpectedValue warnings when
        # a field had the wrong type, before validation could reject it.
        merged_data = {**existing.model_dump(), **clean}
        merged_data["id"] = existing.id
        merged_data["created_at"] = existing.created_at
        merged = Ritual.model_validate(merged_data)
        merged.id = existing.id
        merged.created_at = existing.created_at
        self.guard_ritual(merged)
        self.store.save_ritual(merged)
        return merged.model_dump()

    def duplicate_ritual(self, ritual_id: str) -> Optional[dict]:
        existing = self.store.get_ritual(ritual_id)
        if not existing:
            return None
        clone = existing.model_copy(deep=True)
        clone.id = existing.id + "-copy-" + str(existing.id)[:4]
        import uuid
        clone.id = uuid.uuid4().hex[:12]
        clone.name = f"{existing.name} (copy)"
        clone.enabled = True
        clone.actions = [a.model_copy(update={"id": __import__("uuid").uuid4().hex[:12]}) for a in clone.actions]
        clone.set_updated()
        self.store.save_ritual(clone)
        return clone.model_dump()

    def delete_ritual(self, ritual_id: str) -> bool:
        # Stop it if running.
        self.registry.stop(ritual_id)
        return self.store.delete_ritual(ritual_id)

    def reorder_rituals(self, ordered_ids: list[str]) -> None:
        self.store.reorder_rituals(ordered_ids)

    def clear_history(self) -> None:
        self.store.clear_history()

    # ---- execution --------------------------------------------------------
    async def run_ritual(self, ritual_id: str, triggered_by: str = "local",
                         live: bool = True) -> Optional[dict]:
        ritual = self.store.get_ritual(ritual_id)
        if not ritual:
            return None
        # Don't allow concurrent runs of the same ritual.
        existing = self.registry.get(ritual_id)
        if existing and existing.state in (RunState.RUNNING, RunState.CANCELLING):
            raise RuntimeError("ritual already running")

        runner = RitualRunner(
            ritual, platform=self.platform, triggered_by=triggered_by,
            on_event=self.hub.on_ritual_event,
            action_timeout_default=self.config.action_timeout_default,
        )
        runner.run_id = f"{ritual.id}-{int(datetime.now(timezone.utc).timestamp()*1000)}"
        self.registry.start(runner)

        async def _exec():
            try:
                await runner.run()
            finally:
                # Persist history, then ALWAYS release the registry slot. These
                # used to be one block: if append_history raised, registry.remove
                # was skipped and the ritual stayed 409-locked forever, and — on
                # a disk-full error — the write failure also went unreported.
                try:
                    if runner.history:
                        self.store.append_history(runner.history)
                        self.hub.on_generic({"type": "history_updated"})
                except Exception as e:  # noqa: BLE001
                    logging.getLogger("pcrituals").error(
                        "could not save run history for %s: %s", ritual_id, e)
                    self.hub.on_generic({
                        "type": "history_failed",
                        "ritual_id": ritual_id,
                        "error": str(e),
                    })
                finally:
                    self.registry.remove(ritual_id)

        asyncio.create_task(_exec())
        return {
            "ritual_id": ritual_id,
            "run_id": runner.run_id,
            "running": True,
            "name": ritual.name,
        }

    def stop_ritual(self, ritual_id: str) -> bool:
        return self.registry.stop(ritual_id)

    def current_run(self) -> Optional[dict]:
        """Live status of any running ritual (for dashboard / phone)."""
        for rid, runner in self.registry.all().items():
            if runner.state in (RunState.RUNNING, RunState.CANCELLING):
                steps = [] if runner.history is None else [
                    {"name": s.action_name, "status": s.status,
                     "type": s.action_type, "detail": s.detail}
                    for s in runner.history.steps]
                return {
                    "ritual_id": rid,
                    "run_id": runner.run_id,
                    "state": runner.state.value,
                    # Include `running` explicitly. It was missing, so any client
                    # checking `response.running` (which the UI did) always saw
                    # nothing and the live progress panel never appeared at all,
                    # even while a ritual was mid-run. `cancelling` still counts
                    # as active — the run has not finished winding down.
                    "running": True,
                    "stopping": runner.state == RunState.CANCELLING,
                    "current_index": runner.current_step_index,
                    "actions_total": len(runner.ritual.enabled_actions),
                    "actions_completed": runner.actions_completed,
                    "actions_failed": runner.actions_failed,
                    "name": runner.ritual.name,
                    "steps": steps,
                    "triggered_by": runner.triggered_by,
                }
        return None

    # ---- PC status ---------------------------------------------------------
    def pc_status(self) -> dict:
        return {
            "online": True,
            "platform": self.platform.name,
            "cpu": round(self.platform.cpu_load(), 1),
            "memory": self.platform.memory_usage(),
            "uptime": None,
            "running_ritual": self.current_run(),
        }

    # ---- power controls (with caller confirmation requirement handled in API) ---
    def power_control(self, action: str) -> dict:
        self.platform.power(action)
        return {"action": action, "executed": True}

    # ---- pairing passthroughs ----------------------------------------------
    def create_pairing_code(self) -> dict:
        pc = self.security.create_pairing_code()
        # Encode the LAN URL (the phone should connect to the PC's real IP, not
        # localhost) into the QR so scanning it takes the phone straight to the
        # pairing page.
        base = self.pairing_base_url()
        # The pairing code is single-use and expires (config.pairing_code_ttl), so
        # embedding it in the URL is safe (the security model permits short-lived
        # secrets in the QR — never permanent tokens).
        url = f"{base}/#/remote?code={pc.code}" if base else ""
        result = {"code": pc.code, "device_hint": pc.device_hint,
                  "expires_in": int((pc.expires_at - datetime.now(timezone.utc)).total_seconds()),
                  "lan_url": base}
        try:
            import qrcode
            from io import BytesIO
            qr = qrcode.QRCode(border=1, box_size=8)
            qr.add_data(url or pc.code)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            import base64
            result["qr_png"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            result["qr_png"] = None  # frontend falls back to the manual code
        return result

    def pairing_base_url(self) -> str:
        """The best URL for a phone to reach this PC, or '' if undetectable.

        The order is the phone's interest, not the PC's: a Tailscale address
        keeps working when the phone leaves the house, so it beats the LAN
        address that only works on the same Wi-Fi. The LAN address remains in
        the list as a fallback for a phone that has not joined the tailnet yet.

        With HTTPS on these are https addresses: the phone PWA needs a secure
        origin for its camera, and the QR code is how the phone gets there, so
        advertising http would defeat the point of enabling TLS.
        """
        # A running tunnel beats everything else: it is the only address that
        # works from anywhere AND needs nothing installed on the phone. Without
        # this the QR kept advertising the home Wi-Fi address, so scanning it
        # away from home opened a page that could never load.
        # Cloudflare quick tunnel is the remote-access provider.
        from pcrituals import tunnel as mod
        public = (mod.tunnel.status().get("url") or "").strip()
        if public:
            return public.rstrip("/")

        from pcrituals import net
        scheme, port = "http", self.config.port
        if self.config.tls_enabled:
            scheme, port = "https", self.config.tls_port
        urls = net.pairing_urls(port, scheme=scheme)
        return urls[0] if urls else ""

    def pc_pairing_state(self) -> dict:
        return self.security.pairing_ui_state()

    def list_devices(self) -> list[dict]:
        return self.security.list_devices()

    def revoke_device(self, device_id: str) -> bool:
        return self.security.revoke_device(device_id)

    def revoke_all(self) -> int:
        return self.security.revoke_all()

    # ---- history -----------------------------------------------------------
    def history(self, limit: int = 100) -> list[dict]:
        return [h.model_dump() for h in self.store.list_history(limit=limit)]

    def history_for_ritual(self, ritual_id: str, limit: int = 50) -> list[dict]:
        return [h.model_dump() for h in self.store.list_history(ritual_id=ritual_id, limit=limit)]

    def history_summary(self) -> dict:
        return self.store.history_summary()

    # ---- import / export ---------------------------------------------------
    def export_imports(self) -> str:
        """Serialise every ritual as JSON.

        `default=str` used to be used here, which stringified the pydantic models
        into Python *repr* strings. The result could not be re-imported at all —
        the app's own export failed to round-trip — so the models are dumped
        properly instead.
        """
        rituals = [json.loads(r.model_dump_json()) for r in self.store.list_rituals()]
        return json.dumps({"version": 1, "rituals": rituals}, default=str)

    def import_rituals(self, payload: dict) -> dict:
        """Validate and import a ritual export. Does NOT execute imported commands."""
        if not isinstance(payload, dict):
            raise ValueError("invalid import format")
        items = payload.get("rituals", payload.get("data", []))
        if not isinstance(items, list):
            raise ValueError("'rituals' must be a list")
        imported = 0
        errors = []
        for item in items:
            if not isinstance(item, dict) or "actions" not in item or "name" not in item:
                errors.append("entry missing name/actions")
                continue
            try:
                ritual = Ritual.model_validate(item)
                if not self._safe_ritual(ritual):
                    errors.append(f"'{item.get('name')}': blocked dangerous command")
                    continue
                ritual.id = _new_id()
                self.store.save_ritual(ritual)
                imported += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"'{item.get('name')}': {e}")
        return {"imported": imported, "errors": errors}

    # Patterns that describe a command whose *purpose* is destroying data or the
    # machine. Matched against a whitespace-normalised, lower-cased command so
    # that "rm  -rf" (two spaces) or "rm\t-rf" cannot slip through, which is
    # exactly how the previous literal-substring check was defeated.
    _DESTRUCTIVE = [
        r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf]",          # rm -rf / rm -fr / rm -r -f
        r"\bdel\s+/[sq]",                            # del /s /q
        r"\brd\s+/s", r"\brmdir\s+/s",
        r"\bformat\s+[a-z]:",                        # format c:
        r"\bdiskpart\b",
        r"\bmkfs(\.\w+)?\b",
        r"\bdd\s+if=",
        r"remove-item\b.*-recurse",                  # PowerShell recursive delete
        r"\|\s*(sh|bash|cmd|powershell)\b",          # piping a download into a shell
        r"\b(shutdown|poweroff|halt)\b",
        r"\breboot\b",
        r"\bgit\s+push\s+.*--force",
        r">\s*/dev/(sd|nvme|hd)",
        r":\(\)\s*\{.*\};:",                         # fork bomb
        r"\bcipher\s+/w",
    ]

    def dangerous_command(self, ritual: Ritual) -> Optional[str]:
        """Return the offending command if a ritual contains a destructively
        intented command action, else None.

        Deliberately narrow: it targets commands whose whole purpose is
        destroying data or the machine, not merely commands that touch files.
        Anything that can shut a machine down belongs in the `power` action,
        which requires an explicit confirmation.
        """
        import re
        for a in ritual.actions:
            if a.type.value != "command":
                continue
            normalised = " ".join((a.target or "").split()).lower()
            for pat in self._DESTRUCTIVE:
                if re.search(pat, normalised):
                    return a.target
        return None

    def _safe_ritual(self, ritual: Ritual) -> bool:
        """True when a ritual is safe to accept."""
        return self.dangerous_command(ritual) is None

    def guard_ritual(self, ritual: Ritual) -> None:
        """Raise ValueError naming the command when a write path would store a
        destructive command.

        Applied to import AND to direct creation, because the previous check
        only ran on import — so a paired phone could create the same ritual
        through POST /rituals and skip it entirely.
        """
        bad = self.dangerous_command(ritual)
        if bad is not None:
            raise ValueError(
                f"This command looks destructive and was refused: {bad!r}. "
                "If you meant to shut down or restart the PC, add a Power action instead."
            )

    # ---- integrations -------------------------------------------------------
    def detect_integrations(self) -> list[dict]:
        from pcrituals.integrations import detect_integrations as _det
        return _det()

    def wol_status(self) -> dict:
        return {
            "broadcast": self.config.wol_broadcast,
            "port": self.config.wol_port,
            "supported": True,
            "note": "Target PC must have Wake-on-LAN enabled in its NIC/BIOS settings.",
        }

    def wol_wake(self, mac: str) -> None:
        from pcrituals.wol import WakeOnLan
        wol = WakeOnLan(self.config.wol_broadcast, self.config.wol_port)
        wol.send(mac)

    # ---- voice --------------------------------------------------------------
    def voice_parse(self, phrase: str) -> Optional[dict]:
        """Parse a spoken phrase into a control command. Real execution happens
        only with confirmation for destructive power actions."""
        from pcrituals.voice import VoiceEngine

        def names():
            return [r.name for r in self.store.list_rituals()]

        engine = VoiceEngine(rituals_provider=names)
        cmd = engine.parse(phrase or "")
        if cmd is None:
            return None
        result = {
            "intent": cmd.intent,
            "ritual_name": cmd.ritual_name,
            "needs_confirmation": cmd.needs_confirmation,
            "power_action": cmd.intent if cmd.intent in ("lock", "sleep", "restart", "shutdown") else None,
        }
        # Non-confirmable intents can execute directly (start/stop ritual, lock, sleep).
        if not cmd.needs_confirmation:
            executed = self._execute_voice(cmd)
            result["executed"] = executed
        else:
            result["executed"] = False
            result["message"] = "confirmation required"
        return result

    def _execute_voice(self, cmd) -> dict:
        from pcrituals.voice import VoiceCommand  # noqa: F401
        if cmd.intent == "start_ritual" and cmd.ritual_name:
            for r in self.store.list_rituals():
                if r.name.lower() == cmd.ritual_name.lower():
                    import asyncio
                    asyncio.create_task(self.run_ritual(r.id, triggered_by="voice"))
                    return {"executed": True, "ritual": r.name}
        elif cmd.intent == "stop_ritual":
            for r in self.store.list_rituals():
                if r.name.lower() == cmd.ritual_name.lower():
                    self.registry.stop(r.id)
                    return {"executed": True, "ritual": r.name}
        elif cmd.intent in ("lock", "sleep"):
            self.platform.power(cmd.intent)
            return {"executed": True}
        return {"executed": False}

    def _execute_voice_confirm(self, intent: str) -> bool:
        """Execute a confirmed destructive voice power action."""
        if intent not in ("restart", "shutdown"):
            return False
        try:
            self.platform.power(intent)
            return True
        except Exception:
            return False

    # ---- backups ------------------------------------------------------------
    def backup_now(self, label: str = "manual") -> dict:
        from pcrituals.backup import BackupManager
        return BackupManager(self.config).create_backup(label)

    def list_backups(self) -> list[dict]:
        from pcrituals.backup import BackupManager
        return BackupManager(self.config).list_backups()

    def restore_backup(self, name: str) -> dict:
        from pcrituals.backup import BackupManager
        return BackupManager(self.config).restore(name)

    def delete_backup(self, name: str) -> bool:
        from pcrituals.backup import BackupManager
        return BackupManager(self.config).delete_backup(name)

    def prune_backups(self, keep: int = 10) -> int:
        from pcrituals.backup import BackupManager
        return BackupManager(self.config).prune(keep)

    # ---- deck (Stream Deck) ---------------------------------------------------
    def list_deck(self) -> list[dict]:
        return [b.model_dump() for b in self.store.list_deck()]

    def save_deck_button(self, data: dict) -> dict:
        from pcrituals.models import DeckButton
        btn = DeckButton.model_validate(data)
        # A deck button can carry a command too, so it gets the same guard as a
        # ritual: otherwise the filter is trivially bypassed by creating a button.
        if btn.kind.value == "command":
            from pcrituals.models import Action, ActionType
            probe = Ritual(name="deck-guard",
                           actions=[Action(type=ActionType.COMMAND, target=btn.target)])
            self.guard_ritual(probe)
        if not btn.icon:
            from pcrituals.icons import resolve_icon
            r = resolve_icon(btn.kind.value, btn.target, btn.label)
            btn.icon = r["value"] if r["type"] == "emoji" else r["value"]
        self.store.save_deck_button(btn)
        return btn.model_dump()

    def delete_deck_button(self, bid: str) -> bool:
        return self.store.delete_deck_button(bid)

    def reorder_deck(self, ordered_ids: list[str]) -> None:
        self.store.reorder_deck(ordered_ids)

    def preview_icon(self, kind: str, target: str, label: str = "") -> dict:
        """Return an icon descriptor for the UI (url or emoji)."""
        from pcrituals.icons import resolve_icon, suggest_kind
        if not kind:
            kind = suggest_kind(target)
        return resolve_icon(kind, target, label)

    def suggest_kind(self, target: str) -> str:
        from pcrituals.icons import suggest_kind
        return suggest_kind(target)

    async def press_deck_button(self, bid: str) -> dict:
        """Execute a deck button's action."""
        btn = self.store.get_deck_button(bid)
        if btn is None:
            raise KeyError("deck button not found")
        kind = btn.kind.value
        target = btn.target
        if kind == "app":
            await asyncio.to_thread(self.platform.launch, target, btn.args)
        elif kind == "game":
            await asyncio.to_thread(self.platform.launch_game, target, btn.args)
        elif kind == "website":
            await asyncio.to_thread(self.platform.open_website, target)
        elif kind == "file":
            await asyncio.to_thread(self.platform.open_path, target)
        elif kind == "command":
            await asyncio.to_thread(self.platform.run_command, target,
                                    self.config.action_timeout_default)
        elif kind == "media":
            await asyncio.to_thread(self.media.control, target)
        elif kind == "power":
            await asyncio.to_thread(self.platform.power, target)
        elif kind == "ritual":
            await self.run_ritual(target, triggered_by="deck")
        else:
            raise ValueError(f"unsupported deck kind: {kind}")
        return {"pressed": True, "id": bid, "kind": kind}

    # ---- media ----------------------------------------------------------------
    def media_now_playing(self) -> Optional[dict]:
        """Exact data from Spotify when connected; otherwise best-effort from the
        player's window title (Spotify desktop)."""
        if self.spotify.is_connected():
            try:
                pb = self.spotify.now_playing()
                if pb is not None:
                    try:
                        state = self.spotify.playback_state()
                        pb.shuffle = bool(state.get("shuffle", False))
                        pb.repeat = state.get("repeat", "off") or "off"
                    except Exception:
                        pass
                    return pb.to_dict()
                # Connected but nothing playing.
                return {}
            except Exception:
                pass  # fall through to the window-title method
        np = self.media.now_playing()
        return np.to_dict() if np else None

    def media_control(self, action: str, value=None) -> dict:
        """Prefer Spotify's API (precise, supports shuffle/repeat); fall back to
        OS media keys."""
        if self.spotify.is_connected():
            try:
                self.spotify.control(action, value)
                return {"action": action, "ok": True, "via": "spotify"}
            except Exception:
                pass
        # Media keys only understand transport actions.
        if action in ("play", "pause"):
            self.media.control("play_pause")
        elif action in ("play_pause", "toggle", "next", "previous", "prev", "stop"):
            self.media.control(action)
        else:
            raise ValueError(f"'{action}' needs Spotify connected")
        return {"action": action, "ok": True, "via": "media-keys"}

    # ---- spotify --------------------------------------------------------------
    def spotify_status(self) -> dict:
        return self.spotify.status()

    def spotify_configure(self, client_id: str, client_secret: str,
                          redirect_uri: str | None = None) -> dict:
        return self.spotify.configure(client_id, client_secret, redirect_uri)

    def spotify_auth_url(self) -> str:
        return self.spotify.auth_url()

    def spotify_disconnect(self) -> dict:
        self.spotify.disconnect()
        return self.spotify.status()

    # ---- updater --------------------------------------------------------------
    def update_status(self) -> dict:
        """Return the app version and whether an update is available."""
        from pcrituals import __version__
        return {
            "version": __version__,
            "update_url": self.config.update_url,
            "available": self._update_check_result(),
        }

    def get_settings(self) -> dict:
        """User-facing settings (persisted to settings.json)."""
        from pcrituals import startup
        return {
            "update_url": self.config.update_url,
            "streamer_mode": bool(getattr(self.config, "streamer_mode", False)),
            "tunnel_provider": "cloudflare",
            "tunnel_autostart": bool(getattr(self.config, "tunnel_autostart", False)),
            "notify_enabled": bool(getattr(self.config, "notify_enabled", False)),
            "obs": {
                "port": int(getattr(self.config, "obs_port", 4455)),
                "has_password": bool(getattr(self.config, "obs_password", "")),
                "exe": str(getattr(self.config, "obs_exe", "")),
            },
            # Read live from the registry rather than from settings.json: the
            # installer writes that same Windows value, so the registry is the
            # one source of truth for whether we really start at login.
            "run_at_login": startup.status(),
        }

    def set_settings(self, data: dict) -> dict:
        """Persist runtime settings (currently the update URL)."""
        import json
        current = {}
        if self.config.settings_file.exists():
            try:
                current = json.loads(self.config.settings_file.read_text())
            except Exception:
                current = {}
        if "update_url" in data:
            current["update_url"] = str(data.get("update_url") or "").strip()
        # Not written to settings.json: Windows owns this one. The installer and
        # this toggle write the same registry value, so persisting a copy here
        # would create a second answer that can drift out of sync.
        for key in ("streamer_mode", "tunnel_autostart", "notify_enabled"):
            if key in data:
                current[key] = bool(data.get(key))
        if "tunnel_provider" in data:
            current["tunnel_provider"] = str(data.get("tunnel_provider") or "cloudflare")
        for key in ("obs_password", "obs_exe"):
            if key in data:
                current[key] = str(data.get(key) or "")
        if "obs_host" in data:
            host = str(data.get("obs_host") or "").strip()
            if host and not _is_local_obs_host(host):
                # An authenticated client could otherwise point OBS at any
                # internal host/port and probe it (SSRF). OBS is on this PC.
                raise ValueError("OBS host must be this PC (127.0.0.1, localhost, or a private LAN address)")
            current["obs_host"] = host
        if "obs_port" in data:
            try:
                port = int(data.get("obs_port") or 4455)
            except (TypeError, ValueError):
                port = 4455
            if not (1 <= port <= 65535):
                raise ValueError("OBS port must be between 1 and 65535")
            current["obs_port"] = port
        if "run_at_login" in data:
            from pcrituals import startup
            startup.set_enabled(bool(data.get("run_at_login")))
        self.config.settings_file.write_text(json.dumps(current, indent=2))
        # Reflect in this process, or the OBS tab would not appear until a
        # restart and the toggle would look broken.
        self.config.update_url = current.get("update_url", self.config.update_url)
        if "streamer_mode" in current:
            self.config.streamer_mode = bool(current["streamer_mode"])
        if "tunnel_provider" in current:
            self.config.tunnel_provider = str(current["tunnel_provider"])
        if "tunnel_autostart" in current:
            self.config.tunnel_autostart = bool(current["tunnel_autostart"])
        if "notify_enabled" in current:
            self.config.notify_enabled = bool(current["notify_enabled"])
        for key in ("obs_host", "obs_password", "obs_exe"):
            if key in current:
                setattr(self.config, key, str(current[key]))
        if "obs_port" in current:
            self.config.obs_port = int(current["obs_port"])
        # Invalidate memoized check so it re-runs.
        if hasattr(self, "_update_cache"):
            del self._update_cache
        return current

    def _update_check_result(self) -> Optional[dict]:
        """Memoized update check (checks once per process, silently on failure)."""
        from pcrituals.update import Updater
        if not hasattr(self, "_update_cache"):
            try:
                updater = Updater(self.config.update_url)
                info = updater.check()
                self._update_cache = {
                    "version": info.version,
                    "notes": info.notes,
                    "url": info.url,
                    # Keep the checksum. Dropping it here meant the UI-driven
                    # update path built an UpdateInfo with sha256=None, and
                    # Updater.download() only verifies when a hash is present --
                    # so the in-app updater downloaded unverified even though the
                    # manifest supplied one. That is the whole point of shipping
                    # a checksum.
                    "sha256": info.sha256,
                } if info else None
            except Exception:
                self._update_cache = None
        return self._update_cache

    def update_download(self) -> dict:
        """Download a pending update. Returns the downloaded path info."""
        from pcrituals.update import Updater, UpdateError
        info = self._update_check_result()
        if not info:
            raise UpdateError("no update available")
        updater = Updater(self.config.update_url)
        from pcrituals.update import UpdateInfo
        # Carry the checksum through so download() actually verifies the archive.
        di = UpdateInfo(version=info["version"], url=info["url"], notes=info["notes"],
                        sha256=info.get("sha256"))
        path = updater.download(di)
        return {"version": info["version"], "downloaded": True, "size": path.stat().st_size if path.exists() else 0}

    def update_apply(self) -> dict:
        """Download + apply the pending update over the install directory."""
        from pcrituals.update import Updater, UpdateInfo, UpdateError
        info = self._update_check_result()
        if not info:
            raise UpdateError("no update available")
        updater = Updater(self.config.update_url)
        di = UpdateInfo(version=info["version"], url=info["url"], notes=info["notes"])
        path = updater.download(di)
        applied = updater.apply(di)
        return {"version": info["version"], "applied": applied,
                "restart_required": True}


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]