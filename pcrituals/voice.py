"""Optional modular voice control.

Designed so voice is a bolt-on: if no speech engines are installed/locally
configured, the rest of the app is completely unaffected. There are two layers:

1. A simple offline command matcher that turns a recognized phrase into a
   control action (start/stop ritual by name, lock/sleep PC).
2. A microphone listener that feeds phrases to the matcher. It uses
   `speech_recognition` if available; otherwise it's disabled gracefully.

Destructive power commands (restart, shutdown) require explicit confirmation
before executing.

"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class VoiceCommand:
    """A matched voice command, ready to be acted upon."""

    intent: str                # "start_ritual" | "stop_ritual" | "lock" | "sleep" | ...
    ritual_name: Optional[str] = None
    raw: str = ""
    needs_confirmation: bool = False


# Intent → requires confirmation before executing (destructive).
_CONFIRM_REQUIRED = {"restart": True, "shutdown": True}


class VoiceEngine:
    """Converts recognized speech phrases into VoiceCommand objects."""

    def __init__(self, rituals_provider: Callable[[], list[str]] = list) -> None:
        """rituals_provider returns a list of available ritual names (lowercase)."""
        self._rituals = rituals_provider

    def parse(self, phrase: str) -> Optional[VoiceCommand]:
        """Parse a spoken phrase into a VoiceCommand, or None if no match."""
        lowered = phrase.strip().lower().replace(".", " ")
        if not lowered:
            return None

        # Ritual launch: "start gaming", "run my work ritual", "stop gaming".
        for name in self._rituals():
            n = (name or "").strip().lower()
            if not n:
                continue
            # Match on word boundaries: a plain `in` test made a ritual called
            # "work" fire on any phrase containing the substring, e.g.
            # "the network is down".
            if not re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", lowered):
                continue
            if re.search(r"\b(stop|cancel|kill)\b", lowered):
                return VoiceCommand("stop_ritual", ritual_name=name, raw=phrase)
            # Any start/run/launch keyword or a bare ritual name means start.
            return VoiceCommand("start_ritual", ritual_name=name, raw=phrase)

        # Generic power intents.
        if re.search(r"\b(lock)\b", lowered):
            return VoiceCommand("lock", raw=phrase)
        if re.search(r"\b(sleep|hibernate)\b", lowered):
            return VoiceCommand("sleep", raw=phrase)
        if re.search(r"\b(restart|reboot)\b", lowered):
            return VoiceCommand("restart", raw=phrase,
                                needs_confirmation=_CONFIRM_REQUIRED["restart"])
        if re.search(r"\b(shut\s*down|power\s*off)\b", lowered):
            return VoiceCommand("shutdown", raw=phrase,
                                needs_confirmation=_CONFIRM_REQUIRED["shutdown"])

        return None


async def listen_once(engine: VoiceEngine, recognizer=None,
                      timeout: float = 5.0) -> Optional[VoiceCommand]:
    """Capture one utterance from the microphone and parse it.

    Fully optional: on systems without `speech_recognition` (or no mic) this
    returns None without raising, so callers degrade gracefully.
    """
    try:
        import speech_recognition as sr  # type: ignore
    except Exception:
        return None

    if recognizer is None:
        recognizer = sr.Recognizer()
    try:
        # Opening the microphone is what fails on a machine with no mic (or no
        # PyAudio); it must not escape as an exception and take down the caller.
        with sr.Microphone() as source:
            # Calibrate ambient noise briefly, then listen.
            recognizer.adjust_for_ambient_noise(source, duration=0.4)
            try:
                audio = await asyncio.to_thread(
                    recognizer.listen, source, timeout=timeout, phrase_time_limit=8)
            except Exception:  # noqa: BLE001 - WaitTimeoutError etc.
                return None
    except Exception:  # noqa: BLE001 - no microphone / no PyAudio / driver error
        return None
    try:
        text = await asyncio.to_thread(recognizer.recognize_google, audio)
    except Exception:
        return None
    return engine.parse(text or "")