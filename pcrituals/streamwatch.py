"""Watch OBS and push a notification when the stream drops.

The phone can't be trusted to notice a dead stream — it's usually closed. So
the PC watches OBS itself. On a transition from streaming -> not streaming (or
unreachable), it pushes to every subscribed device.

State machine: we only notify on a real drop, and we do not re-notify until
the stream has come back up and dropped again. Otherwise a flapping stream
spams the phone. The first check only sets a baseline — we never notify on
startup, because we don't know whether the stream was already down.
"""
from __future__ import annotations

import json
import threading
from typing import Callable, Optional


class StreamWatcher:
    def __init__(
        self,
        config,
        notifier,
        obs_factory: Optional[Callable] = None,
        interval: float = 15.0,
    ) -> None:
        self.config = config
        self.notifier = notifier
        self._obs_factory = obs_factory or self._default_obs
        self._interval = interval
        self._was_streaming: Optional[bool] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def _default_obs(config):
        from pcrituals.obs import OBS
        return OBS(config)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="stream-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.check_once()
            except Exception:  # noqa: BLE001 - a watcher must never die
                pass

    # -- the actual check --------------------------------------------------
    def check_once(self) -> None:
        """One poll. Notifies on a streaming->down transition. Never throws."""
        if not (self.config.streamer_mode and self.config.notify_enabled):
            # Feature off: reset the baseline so turning it on later starts
            # fresh instead of notifying about a drop that happened while off.
            self._was_streaming = None
            return
        try:
            st = self._obs_factory(self.config).status()
        except Exception:  # noqa: BLE001
            # OBS unreachable in a way status() didn't swallow: treat as down.
            st = {"streaming": False}
        streaming = bool(st.get("streaming"))

        if self._was_streaming is None:
            self._was_streaming = streaming
            return

        if self._was_streaming and not streaming:
            self._notify_drop()
        self._was_streaming = streaming

    def _notify_drop(self) -> None:
        payload = json.dumps({
            "title": "Stream went down",
            "body": "OBS stopped streaming. Tap to open Easy Life and fix it.",
            "url": "/#/obs",
        })
        try:
            self.notifier.send_to_all(payload)
        except Exception:  # noqa: BLE001 - a failed push must not kill the watcher
            pass


# ---------------------------------------------------------------------------
# One watcher per process.
#
# create_app() is called many times (every TestClient, every factory launch),
# and starting a watcher per app left a growing pile of sleeping threads. The
# watcher is a process-wide concern: it watches OBS, not a particular app.
# ---------------------------------------------------------------------------
_watcher: Optional["StreamWatcher"] = None
_watcher_lock = threading.Lock()


def start_watcher(config, notifier, interval: float = 15.0) -> "StreamWatcher":
    """Start the process-wide watcher, or return the one already running."""
    global _watcher
    with _watcher_lock:
        if _watcher is not None and _watcher._thread is not None and _watcher._thread.is_alive():
            return _watcher
        _watcher = StreamWatcher(config, notifier, interval=interval)
        _watcher.start()
        return _watcher


def stop_watcher() -> None:
    """Stop the process-wide watcher (used by tests, and at shutdown)."""
    global _watcher
    with _watcher_lock:
        if _watcher is not None:
            _watcher.stop()
            _watcher = None
