"""Action dispatch: maps an Action to a real platform operation."""
from __future__ import annotations

import asyncio
import math
from typing import Any

from pcrituals.models import Action, ActionType, PowerAction
from pcrituals.platform import Platform, PlatformError


async def dispatch_action(platform: Platform, action: Action,
                          cancel_event: Any = None) -> None:
    """Execute one action on the given platform.

    `cancel_event` is forwarded to the handlers that can be interrupted
    (currently the command action); others are short-lived by nature.
    """
    switch = {
        ActionType.APP: _launch_app,
        ActionType.GAME: _launch_game,
        ActionType.WEBSITE: _open_website,
        ActionType.FILE: _open_path,
        ActionType.COMMAND: _run_command,
        ActionType.DELAY: _delay,
        ActionType.CLOSE: _close_app,
        ActionType.POWER: _power,
        ActionType.OBS_SCENE: _obs_scene,
        ActionType.OBS_STREAM_START: _obs_stream_start,
        ActionType.OBS_STREAM_STOP: _obs_stream_stop,
    }
    handler = switch.get(action.type)
    if handler is None:
        raise PlatformError(f"unsupported action type: {action.type}")
    # Forward the cancel event to the handlers that can be interrupted. The
    # command handler kills its process tree; the delay handler sleeps in
    # slices and checks the event. Everything else is short-lived by nature.
    if handler in (_run_command, _delay):
        await handler(platform, action, cancel_event)
    else:
        await handler(platform, action)


async def _launch_app(platform: Platform, action: Action) -> None:
    _require(action.target, "application")
    # Run the blocking launch in the default thread pool; it's fast but may block.
    await asyncio.to_thread(platform.launch, action.target, action.args)


async def _launch_game(platform: Platform, action: Action) -> None:
    _require(action.target, "game")
    await asyncio.to_thread(platform.launch_game, action.target, action.args)


async def _open_website(platform: Platform, action: Action) -> None:
    _require(action.target, "website URL")
    await asyncio.to_thread(platform.open_website, action.target)


async def _open_path(platform: Platform, action: Action) -> None:
    _require(action.target, "file/folder path")
    await asyncio.to_thread(platform.open_path, action.target)


async def _run_command(platform: Platform, action: Action,
                       cancel_event: Any = None) -> None:
    _require(action.target, "command")
    timeout = action.timeout if action.timeout is not None else 30.0
    # The cancel event lets a running command actually be interrupted (the
    # platform kills the process tree) instead of running to completion while
    # the UI claims it stopped.
    await asyncio.to_thread(platform.run_command, action.target, timeout,
                            cancel_event=cancel_event)


async def _delay(platform: Platform, action: Action,
                 cancel_event: Any = None) -> None:
    seconds = _seconds(action)
    if seconds < 0:
        raise PlatformError("delay cannot be negative")
    if cancel_event is None:
        await asyncio.sleep(seconds)
        return
    # Sleep in small slices so a cancel can interrupt a long delay. A single
    # asyncio.sleep(seconds) is not cancellable by the engine's cancel_event,
    # so a 24h delay would sit in "cancelling" until it finished. We RETURN
    # (not raise) on cancel: the engine checks its own cancel flag between
    # steps and finishes CANCELLED itself — raising CancelledError here broke
    # that path.
    import time as _time
    end = _time.monotonic() + seconds
    while _time.monotonic() < end:
        if cancel_event.is_set():
            return
        await asyncio.sleep(min(0.25, end - _time.monotonic()))


def _seconds(action: Action) -> float:
    """Read the wait duration from params['seconds'] (falling back to target).

    params is free-form user JSON, so the value can be a string or junk. A bare
    float() raised ValueError, which the engine reports as a raw Python error
    instead of a comprehensible "delay" failure.
    """
    raw = action.params.get("seconds", action.target or 1)
    if raw is None or raw == "":
        raw = 1
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as e:
        raise PlatformError(f"delay seconds must be a number, got {raw!r}") from e
    # JSON allows NaN/Infinity literals, and float("nan") parses as a valid
    # float; asyncio.sleep then rejected it with "Invalid value NaN (not a
    # number) for timeout", which says nothing about the delay step.
    if not math.isfinite(seconds):
        raise PlatformError(f"delay seconds must be a finite number, got {raw!r}")
    return seconds


async def _close_app(platform: Platform, action: Action) -> None:
    _require(action.target, "process/app name")
    ok = await asyncio.to_thread(platform.close_application, action.target)
    if not ok:
        raise PlatformError(f"could not close application '{action.target}'")


async def _power(platform: Platform, action: Action) -> None:
    power = action.params.get("action")
    # `params` is free-form user JSON, so `action` can be a list/dict. Those are
    # unhashable and blew the set-membership test up with a TypeError, which the
    # engine surfaced as a raw Python error instead of a rejected action.
    if not isinstance(power, str) or power not in _POWER_ACTIONS:
        raise PlatformError(f"unsupported power action: {power!r}")
    await asyncio.to_thread(platform.power, power)


# Whitelist of the only strings that may reach a destructive power call.
_POWER_ACTIONS = frozenset(p.value for p in PowerAction)


def _obs_client(platform: Platform):
    """The OBS controls for this platform's config.

    Reads the config off the platform when it carries one (the packaged app
    does) and falls back to load_config(), so a Play works either way.
    """
    from pcrituals.obs import OBS

    cfg = getattr(platform, "config", None)
    if cfg is None:
        from pcrituals.config import load_config
        cfg = load_config()
    return OBS(cfg)


async def _obs_scene(platform: Platform, action: Action) -> None:
    """Switch OBS to a scene. `target` names it."""
    _require(action.target, "scene name")
    await _obs_client(platform).a_set_scene(action.target)


async def _obs_stream_start(platform: Platform, action: Action) -> None:
    await _obs_client(platform).a_start_stream()


async def _obs_stream_stop(platform: Platform, action: Action) -> None:
    await _obs_client(platform).a_stop_stream()


def _require(value: str, what: str) -> None:
    if not value:
        raise PlatformError(f"missing {what}")