"""Data models for Rituals and their actions."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    APP = "app"            # Launch a desktop application
    GAME = "game"          # Launch a game (often a store app / URL)
    WEBSITE = "website"    # Open a URL in the browser
    FILE = "file"          # Open a file or folder
    COMMAND = "command"    # Run a shell/command
    DELAY = "delay"        # Wait N seconds
    CLOSE = "close"        # Close an application by name
    POWER = "power"        # Power control (lock/sleep/restart/shutdown)
    # OBS. These exist so a Play can drive a stream, which is what makes the
    # "if my stream dies while I'm out" workflow a Play rather than a script.
    OBS_SCENE = "obs_scene"              # Switch to a scene (target = scene name)
    OBS_STREAM_START = "obs_stream_start"
    OBS_STREAM_STOP = "obs_stream_stop"


class PowerAction(str, Enum):
    LOCK = "lock"
    SLEEP = "sleep"
    RESTART = "restart"
    SHUTDOWN = "shutdown"


# Ids are interpolated into HTML attributes and inline handlers in the
# frontend. They used to be free-form strings straight from the client, so a
# POST /api/rituals with a markup-bearing id executed script on render.
# Constrain them at the model so a client can never supply markup.
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class Action(BaseModel):
    """A single step in a Ritual."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12], pattern=ID_PATTERN)
    type: ActionType
    name: str = ""                       # Human label; auto-filled if empty.
    target: str = ""                     # App path, URL, file path, command, process name (varies by type)
    args: list[str] = Field(default_factory=list)   # CLI args for command/app actions
    params: dict[str, Any] = Field(default_factory=dict)
    timeout: Optional[float] = None      # Per-action override of default timeout
    enabled: bool = True
    continue_on_error: Optional[bool] = None  # Override ritual-level behavior
    notes: str = ""

    # Convenience helpers for building actions of a given type.
    @classmethod
    def delay(cls, seconds: float, label: str | None = None) -> "Action":
        return cls(type=ActionType.DELAY, params={"seconds": seconds},
                   name=label or f"Wait {seconds:g}s")

    @classmethod
    def website(cls, url: str, label: str | None = None) -> "Action":
        return cls(type=ActionType.WEBSITE, target=url, name=label or f"Open {url}")

    @classmethod
    def app(cls, path: str, label: str | None = None, args: list[str] | None = None) -> "Action":
        return cls(type=ActionType.APP, target=path, name=label or PathName.nice(path) or "Launch application",
                   args=args or [])

    @classmethod
    def game(cls, store_app: str, label: str | None = None) -> "Action":
        """Launch a game. store_app is something like `steam://rungameid/570` or `leagueoflegends`."""
        return cls(type=ActionType.GAME, target=store_app, name=label or f"Launch game")

    @classmethod
    def file(cls, path: str, label: str | None = None) -> "Action":
        return cls(type=ActionType.FILE, target=path, name=label or f"Open {PathName.nice(path) or path}")

    @classmethod
    def command(cls, cmd: str, label: str | None = None, timeout: float | None = None) -> "Action":
        return cls(type=ActionType.COMMAND, target=cmd, name=label or f"Run: {cmd}", timeout=timeout)

    @classmethod
    def close(cls, process_name: str, label: str | None = None) -> "Action":
        return cls(type=ActionType.CLOSE, target=process_name, name=label or f"Close {process_name}")

    @classmethod
    def power(cls, action: PowerAction, label: str | None = None) -> "Action":
        return cls(type=ActionType.POWER, params={"action": action.value},
                   name=label or f"Power: {action.value.title()}")


class PathName:
    """Small utility for turning paths into friendly names."""

    @staticmethod
    def nice(path: str) -> str:
        try:
            return Path(path.rstrip("/\\")).name
        except Exception:
            return path


class RitualStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class Ritual(BaseModel):
    """A named, ordered sequence of actions."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12], pattern=ID_PATTERN)
    name: str
    description: str = ""
    actions: list[Action] = Field(default_factory=list)
    stop_on_error: bool = True            # continue-on-error if False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True

    def set_updated(self) -> None:
        self.updated_at = datetime.now(timezone.utc)

    def reorder(self, new_order: list[str]) -> None:
        """Reorder actions by their ids (drag & drop support)."""
        by_id = {a.id: a for a in self.actions}
        # Preserve any ids not mentioned (append in original order).
        ordered = []
        for aid in new_order:
            if aid in by_id:
                ordered.append(by_id.pop(aid))
        for a in by_id.values():
            ordered.append(a)
        self.actions = ordered
        self.set_updated()

    @property
    def enabled_actions(self) -> list[Action]:
        return [a for a in self.actions if a.enabled]


class RitualListItem(BaseModel):
    """Summary view of a Ritual for list endpoints."""
    id: str
    name: str
    description: str = ""
    status: RitualStatus = RitualStatus.DRAFT
    action_count: int = 0
    tags: list[str] = Field(default_factory=list)
    updated_at: datetime


class HistoryEntry(BaseModel):
    """A record of one Ritual execution."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12], pattern=ID_PATTERN)
    ritual_id: str
    ritual_name: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: RitualStatus
    error: Optional[str] = None
    triggered_by: str = "local"          # local | phone | other
    duration_ms: Optional[int] = None
    actions_total: int = 0
    actions_completed: int = 0
    steps: list["HistoryStep"] = Field(default_factory=list)


class HistoryStep(BaseModel):
    action_id: str
    action_name: str
    action_type: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    status: str = "pending"              # pending | running | success | error | skipped | cancelled
    detail: Optional[str] = None
    duration_ms: Optional[int] = None


class DeckKind(str, Enum):
    APP = "app"              # launch an application
    GAME = "game"            # launch a game (store URL / exe)
    WEBSITE = "website"      # open a URL
    FILE = "file"            # open a file/folder
    COMMAND = "command"      # run a command
    RITUAL = "ritual"        # run a saved ritual
    MEDIA = "media"          # media control (play/pause/next/prev)
    POWER = "power"          # power control


class DeckButton(BaseModel):
    """A Stream Deck button. `icon` may be an emoji, an image URL, or empty to
    auto-resolve (favicon for sites, executable icon for apps, etc.)."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12], pattern=ID_PATTERN)
    label: str = ""
    kind: DeckKind = DeckKind.APP
    target: str = ""               # path / url / ritual id / media action / power action
    args: list[str] = Field(default_factory=list)
    icon: str = ""                 # emoji or image URL ("" = auto)
    color: str = ""                # optional accent tint
    position: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def resolved_label(self) -> str:
        if self.label:
            return self.label
        if self.kind == DeckKind.MEDIA:
            return {"play_pause": "Play/Pause", "next": "Next", "previous": "Previous",
                    "stop": "Stop"}.get(self.target, "Media")
        if self.kind == DeckKind.POWER:
            return self.target.title()
        if self.target:
            return PathName.nice(self.target) or self.target
        return str(self.kind.value)