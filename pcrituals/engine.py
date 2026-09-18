"""Ritual execution engine.

Runs a Ritual's actions sequentially with progress tracking, cancellation,
timeouts, continue-on-error / stop-on-error behavior, and retries.

The engine is transport-agnostic: callers subscribe to `on_event` to receive
live progress updates, which the API layer fans out over SSE/WebSocket.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from pcrituals.models import (
    HistoryEntry, HistoryStep, Ritual, RitualStatus,
)
from pcrituals.platform import Platform, PlatformError, get_platform


class RunState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


# An action timeout above this is almost certainly a typo, and a raw huge value
# reached subprocess/asyncio as an OverflowError that surfaced to the user as an
# incomprehensible step failure. One day is a sane ceiling.
MAX_ACTION_TIMEOUT_S = 86_400.0


# Events:
#   {"type":"started", ...}
#   {"type":"step_start", "index":..., "action":{...}, "step":{...}}
#   {"type":"step_done",  "index":..., "step":{...}}
#   {"type":"step_error", "index":..., "step":{...}, "error":str}
#   {"type":"step_retry", "index":..., "attempt":n}
#   {"type":"step_failed","index":..., "error":str, "continue_on_error":bool}
#   {"type":"cancelling"}
#   {"type":"finished", "status":str, "error":str|None}
# Every event carries run_id, ritual_id, ritual_name, state, current_index,
# actions_total, actions_completed, actions_failed, timestamp.
EventListener = Callable[[dict[str, Any]], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_int(value: Any, default: int = 0) -> int:
    """Best-effort int conversion for values coming from user JSON.

    `params` is a free-form dict, so `retries` can arrive as "2", "two" or
    None. Casting with a bare int() raised out of `run()` for non-numeric
    values, which aborted the whole run (the run never reached a finished
    state and its history was never marked complete).
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class RitualRunner:
    """Executes one Ritual. One instance per run."""

    def __init__(
        self,
        ritual: Ritual,
        platform: Optional[Platform] = None,
        triggered_by: str = "local",
        on_event: Optional[EventListener] = None,
        *,
        action_timeout_default: float = 30.0,
    ) -> None:
        self.ritual = ritual
        self.platform = platform or get_platform()
        self.triggered_by = triggered_by
        self.on_event = on_event or (lambda _: None)
        self.action_timeout_default = action_timeout_default

        self.run_id = ""
        self.history: Optional[HistoryEntry] = None

        self.state = RunState.IDLE
        self._cancel_requested = asyncio.Event()
        # A threading.Event mirroring the asyncio one, for work running in a
        # worker thread (a command action) where asyncio primitives cannot be
        # awaited. Without it, Stop could not interrupt a running command.
        self._thread_cancel = __import__("threading").Event()
        self.current_step_index: int = -1
        self.actions_completed = 0
        self.actions_failed = 0
        self.started_at: Optional[datetime] = None

    # ---- event emission ---------------------------------------------------
    def _emit(self, **payload: Any) -> None:
        base = {
            "run_id": self.run_id,
            "ritual_id": self.ritual.id,
            "ritual_name": self.ritual.name,
            "state": self.state.value,
            "current_index": self.current_step_index,
            "actions_total": len(self.ritual.enabled_actions),
            "actions_completed": self.actions_completed,
            "actions_failed": self.actions_failed,
            "timestamp": _utcnow().isoformat(),
        }
        base.update(payload)
        self.on_event(base)

    def cancel(self) -> None:
        """Request cancellation.

        An in-flight command is killed; anything else stops at the next step
        boundary. Note the state becomes CANCELLING first and only becomes
        CANCELLED once the run loop actually unwinds, so callers must not
        report "stopped" the moment this returns.
        """
        self._cancel_requested.set()
        self._thread_cancel.set()
        if self.state == RunState.RUNNING:
            self.state = RunState.CANCELLING
            self._emit(type="cancelling")

    # ---- public run ---------------------------------------------------------
    async def run(self) -> RunState:
        self.state = RunState.RUNNING
        # run_id must be a string: it is emitted over SSE and rendered by the UI.
        self.run_id = self.run_id or f"run-{id(self)}"
        self.started_at = _utcnow()
        self.history = HistoryEntry(
            ritual_id=self.ritual.id,
            ritual_name=self.ritual.name,
            started_at=self.started_at,
            status=RitualStatus.RUNNING,
            triggered_by=self.triggered_by,
            actions_total=len(self.ritual.enabled_actions),
        )
        self._emit(type="started")

        steps = self.ritual.enabled_actions
        stop_requested = False
        failed_error: Optional[str] = None

        for index, action in enumerate(steps):
            if self._cancel_requested.is_set():
                return self._finish(RunState.CANCELLED)

            failed = await self._run_action(action, index, len(steps))
            if failed is not None:
                failed_error = failed
                if self._stop_on_error(action):
                    stop_requested = True
                    break
            if self._cancel_requested.is_set():
                return self._finish(RunState.CANCELLED)

        if self._cancel_requested.is_set():
            return self._finish(RunState.CANCELLED)

        if stop_requested:
            return self._finish(RunState.FAILED, error=failed_error)
        # The whole sequence executed (continue-on-error): the run finished.
        # Failures are surfaced step-by-step; a partially-failed completed run
        # reports as completed with an informational error note.
        note = f"{self.actions_failed} action(s) failed" if self.actions_failed else None
        return self._finish(RunState.COMPLETED, error=note)

    def _stop_on_error(self, action) -> bool:
        if action.continue_on_error is not None:
            return not action.continue_on_error
        return self.ritual.stop_on_error

    async def _run_action(self, action, index: int, total: int) -> Optional[str]:
        """Run a single action. Returns an error string if the action failed, else None."""
        self.current_step_index = index
        step = HistoryStep(
            action_id=action.id,
            action_name=action.name or str(action.type.value),
            action_type=action.type.value,
            started_at=_utcnow(),
            status="running",
        )
        self.history.steps.append(step)
        self._emit(type="step_start", index=index, action=action.model_dump(mode="json"), step=step.model_dump(mode="json"))

        timeout = action.timeout if action.timeout is not None else self.action_timeout_default
        # A zero/negative timeout would fail the action instantly (asyncio.timeout
        # expires as soon as it is awaited), which is never what the user means
        # by leaving the field empty/zero in the UI.
        if timeout <= 0:
            timeout = self.action_timeout_default if self.action_timeout_default > 0 else 30.0
        # A huge value (1e9) reached subprocess/asyncio as a raw OverflowError
        # and surfaced to the user as an incomprehensible step failure. Cap it.
        timeout = min(float(timeout), MAX_ACTION_TIMEOUT_S)
        retries = max(0, _as_int(action.params.get("retries", 0)))

        last_error: Optional[str] = None
        attempts = retries + 1
        for attempt in range(attempts):
            if self._cancel_requested.is_set():
                return "cancelled"  # treated as failed but loop will stop
            try:
                async with asyncio.timeout(timeout):
                    await self._dispatch(action)
                step.status = "success"
                step.detail = None
                step.finished_at = _utcnow()
                step.duration_ms = _ms_since(step.started_at)
                self.actions_completed += 1
                self._emit(type="step_done", index=index, step=step.model_dump(mode="json"))
                return None
            except asyncio.TimeoutError as _t:
                last_error = f"action '{action.name or action.id}' timed out after {timeout:g}s"
                if attempt < retries:
                    step.status = "running"
                    step.detail = f"retry {attempt + 1}/{retries}: {last_error}"
                    self._emit(type="step_retry", index=index, attempt=attempt + 1, error=last_error)
                    await asyncio.sleep(0.5)
                    continue
                break
            except Exception as e:  # noqa: BLE001 - classified below
                last_error = str(e)
                if attempt < retries:
                    step.status = "running"
                    step.detail = f"retry {attempt + 1}/{retries}: {e}"
                    self._emit(type="step_retry", index=index, attempt=attempt + 1, error=str(e))
                    await asyncio.sleep(0.5)
                    continue
                break

        # Action ultimately failed.
        self.actions_failed += 1
        step.status = "error"
        step.finished_at = _utcnow()
        step.duration_ms = _ms_since(step.started_at)
        step.detail = last_error
        cont = self._stop_on_error(action)
        self._emit(type="step_error", index=index, step=step.model_dump(mode="json"), error=last_error)
        self._emit(type="step_failed", index=index, error=last_error, continue_on_error=cont)
        return last_error

    async def _dispatch(self, action) -> None:
        from pcrituals.actions import dispatch_action
        await dispatch_action(self.platform, action, self._thread_cancel)

    def _finish(self, state: RunState, error: Optional[str] = None) -> RunState:
        self.state = state
        finished = _utcnow()
        status = {
            RunState.COMPLETED: RitualStatus.COMPLETED,
            RunState.CANCELLED: RitualStatus.STOPPED,
            RunState.FAILED: RitualStatus.FAILED,
        }[state]

        if self.history is not None:
            self.history.finished_at = finished
            self.history.status = status
            self.history.error = error
            self.history.duration_ms = int(
                (finished - self.history.started_at).total_seconds() * 1000)
            self.history.actions_completed = self.actions_completed

        self._emit(type="finished", status=status.value, error=error)
        return state


def _ms_since(started: datetime) -> int:
    return int((_utcnow() - started).total_seconds() * 1000)


class RunnerRegistry:
    """Tracks running Ritual(s) app-wide."""

    def __init__(self) -> None:
        self._runners: dict[str, RitualRunner] = {}
        # FastAPI endpoints run in a thread pool while runs finish on the event
        # loop, so mutations here are concurrent; an unguarded iteration can
        # raise "dictionary changed size during iteration".
        self._lock = threading.Lock()

    def start(self, runner: RitualRunner) -> RitualRunner:
        with self._lock:
            self._runners[runner.ritual.id] = runner
        return runner

    def get(self, ritual_id: str) -> Optional[RitualRunner]:
        with self._lock:
            return self._runners.get(ritual_id)

    def stop(self, ritual_id: str) -> bool:
        with self._lock:
            runner = self._runners.get(ritual_id)
        if runner and runner.state in (RunState.RUNNING, RunState.CANCELLING):
            runner.cancel()
            return True
        return False

    def remove(self, ritual_id: str) -> None:
        with self._lock:
            self._runners.pop(ritual_id, None)

    def running(self) -> list[str]:
        with self._lock:
            items = list(self._runners.items())
        return [rid for rid, r in items
                if r.state in (RunState.RUNNING, RunState.CANCELLING)]

    def all(self) -> dict[str, RitualRunner]:
        with self._lock:
            return dict(self._runners)