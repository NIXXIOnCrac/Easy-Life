"""Turn a sentence into a Play.

Asking people to assemble steps from dropdowns is fine once you know what the
steps are called. Most people don't — they know what they want to happen, in
order:

    "i want a play to open obs then switch to MAIN 1 scene then start stream
     to twitch and i want the name to be 1234"

This turns that into Actions.

**Deliberately rule-based and offline.** Easy Life's promise is that nothing
leaves the PC; putting an AI model behind this would mean an API key, a monthly
bill and a network round trip to resolve a vocabulary of about twenty verbs —
and it would stop working offline, which is when a rescue Play matters most.

**It fails loudly.** Anything it cannot understand comes back in `unmatched`
and the UI shows it, rather than quietly building a Play that does less than
the user asked for. A Play that silently drops "start stream" is worse than one
that says it didn't understand.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from pcrituals.models import Action, ActionType

# "i want a play to …", "make a play that …" — polite noise before the verbs.
PREAMBLE = re.compile(
    r"^\s*(?:hi|hey|please)?\s*,?\s*"
    r"(?:i\s+(?:want|would like|need)\s+(?:a\s+play\s+)?(?:to\s+|that\s+)?"
    r"|make\s+(?:me\s+)?(?:a\s+)?play\s+(?:that\s+|to\s+)?"
    r"|create\s+(?:a\s+)?play\s+(?:that\s+|to\s+)?"
    r"|a\s+play\s+that\s+"
    r"|play\s+that\s+)",
    re.I,
)

NAME_PATTERNS = (
    re.compile(r"(?:i\s+want\s+)?the\s+name\s+(?:to\s+be|should\s+be|is|as)\s+(.+)", re.I),
    re.compile(r"name\s+it\s+(?:to\s+be\s+|as\s+)?(.+)", re.I),
    re.compile(r"call\s+it\s+(.+)", re.I),
    re.compile(r"named\s+(.+)", re.I),
    re.compile(r"name\s*[:=]\s*(.+)", re.I),
)

# The clause separators. Bare "and" is handled separately, and only when what
# follows actually starts a command — otherwise "rock and roll" would split.
# NOTE: no "." — a full stop is a far less common separator than a domain, and
# including it split "open youtube.com" into "open youtube" + "com".
SEPARATOR = re.compile(
    r"\s*(?:,|;|\bthen\b|\band\s+then\b|\bafter\s+that\b|\bnext\b|"
    r"\bfollowed\s+by\b|\bthen\s+also\b)\s*",
    re.I,
)

# Apps worth resolving by hand: the name is not the executable.
KNOWN_APPS = {
    "obs": "obs",
    "obs studio": "obs",
    "steam": "steam",
    "discord": "discord",
    "spotify": "spotify",
    "chrome": "chrome",
    "google chrome": "chrome",
    "firefox": "firefox",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "notepad": "notepad",
    "explorer": "explorer",
    "file explorer": "explorer",
    "task manager": "taskmgr",
    "valorant": "valorant",
    "fortnite": "fortnite",
}

URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?$", re.I)
DOMAIN_ONLY = re.compile(r"^[a-z0-9-]+\.(?:com|net|org|io|gg|tv|co|uk|de|fr|app|dev|me)$", re.I)

POWER_WORDS = {
    "lock": "lock", "lock the pc": "lock", "lock pc": "lock",
    "sleep": "sleep", "sleep the pc": "sleep",
    "shutdown": "shutdown", "shut down": "shutdown", "shut the pc down": "shutdown",
    "turn off the pc": "shutdown", "power off": "shutdown",
    "restart": "restart", "reboot": "restart",
}


def _strip_trailing_join(text: str) -> str:
    """Trim a conjunction left dangling at the end of a clause.

    Removing a name from "start stream to twitch and i want the name to be 1234"
    leaves "...to twitch and", and that "and" leaked into the destination, so
    the note read "can't switch destination to twitch and".
    """
    text = re.sub(r"[,\s]*\b(?:and|then|also)\b\s*$", "", text, flags=re.I)
    return _clean(text.rstrip(",; "))


def _obs_exe() -> str:
    """OBS's executable, found WITHOUT loading the app config.

    Parsing must stay side-effect free: load_config() creates the data dir and
    migrates legacy data, so reaching for it here meant that merely typing a
    sentence into the builder created a database.
    """
    import os

    from pcrituals.obs import OBS_EXE_CANDIDATES

    for candidate in OBS_EXE_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return "obs64"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).strip(" .!?")


def _strip_preamble(text: str) -> str:
    previous = None
    out = text
    while previous != out:
        previous = out
        out = PREAMBLE.sub("", out, count=1)
    return _clean(out)


def _extract_name(text: str) -> tuple[str, str]:
    """Pull "call it 1234" out of the sentence, returning (name, remainder)."""
    for pattern in NAME_PATTERNS:
        for m in reversed(list(pattern.finditer(text))):
            raw = _clean(m.group(1))
            # Drop a trailing clause only when it is itself a command, so
            # "call it Gaming then open steam" yields "Gaming" while a name like
            # "rock and roll" is left alone.
            parts = re.split(r"\s+\b(?:and|then)\b\s+", raw, flags=re.I)
            if len(parts) > 1 and _looks_like_command(parts[-1]):
                raw = " and ".join(parts[:-1])
            name = _clean(raw)
            if name:
                return name[:60], _strip_trailing_join(
                    text[: m.start()] + " " + text[m.end():])
    return "", text


def _split_clauses(text: str) -> list[str]:
    parts = [p for p in SEPARATOR.split(text) if _clean(p)]
    out: list[str] = []
    for part in parts:
        # "open obs and start stream" — only split on a bare "and" when what
        # follows really is a new command, so ordinary phrases survive.
        bits = re.split(r"\s+\band\b\s+", part, flags=re.I)
        if len(bits) > 1:
            head, tail = bits[0], " and ".join(bits[1:])
            if _looks_like_command(tail):
                out.extend(_split_clauses(head))
                out.extend(_split_clauses(tail))
                continue
        out.append(_clean(part))
    return out


_COMMAND_STARTS = re.compile(
    r"^(?:switch|change|set|go|open|launch|start|stop|end|run|wait|delay|pause|"
    r"close|quit|exit|lock|sleep|shutdown|shut|restart|reboot|visit)\b",
    re.I,
)


THIRD_PERSON = {
    "opens": "open", "launches": "launch", "starts": "start", "closes": "close",
    "runs": "run", "switches": "switch", "changes": "change", "waits": "wait",
    "stops": "stop", "ends": "end", "quits": "quit", "locks": "lock",
    "restarts": "restart", "plays": "play", "visits": "visit",
}


def _normalize_verbs(clause: str) -> str:
    """People write "a play that opens discord and starts streaming".

    Both verbs are third person because they follow "that". Normalising the
    leading verb keeps the rest of the rules simple.
    """
    words = clause.split()
    if words:
        first = words[0].lower()
        if first in THIRD_PERSON:
            words[0] = THIRD_PERSON[first]
    # ...and again after a bare "and", which _split_clauses may have joined back.
    for i, w in enumerate(words):
        low = w.lower()
        if i and words[i - 1].lower() == "and" and low in THIRD_PERSON:
            words[i] = THIRD_PERSON[low]
    return " ".join(words)


def _looks_like_command(text: str) -> bool:
    return bool(_COMMAND_STARTS.match(_normalize_verbs(_clean(text))))


# --------------------------------------------------------------------------
# The rules. Order matters: the most specific patterns are matched first, so
# "start stream" is not eaten by the generic "start <app>" rule.
# --------------------------------------------------------------------------
def _rule_obs_scene(clause: str) -> Action | None:
    m = (re.match(r"(?:switch|change|set|go)\s+(?:the\s+)?(?:obs\s+)?"
                  r"(?:scene\s+to|to\s+scene|to|scene)\s+(.+)$", clause, re.I))
    if not m:
        return None
    scene = _clean(re.sub(r"\s+scene$", "", m.group(1), flags=re.I))
    if not scene:
        return None
    return Action(type=ActionType.OBS_SCENE, target=scene,
                  name=f"OBS: switch to {scene}")


def _rule_stream_stop(clause: str) -> Action | None:
    if re.match(r"^(?:stop|end|kill)\s+(?:the\s+)?(?:stream|streaming|live)$", clause, re.I):
        return Action(type=ActionType.OBS_STREAM_STOP, name="OBS: stop streaming")
    return None


def _rule_stream_start(clause: str) -> Action | None:
    m = re.match(r"^(?:start|begin|go)\s+(?:the\s+)?(?:stream|streaming|live)"
                 r"(?:\s+(?:to|on)\s+(.+))?$", clause, re.I)
    if not m:
        return None
    action = Action(type=ActionType.OBS_STREAM_START, name="OBS: start streaming")
    dest = _clean(m.group(1) or "")
    if dest:
        # Easy Life cannot pick the destination: that lives in OBS's own stream
        # settings. Say so instead of pretending the step does it.
        action.notes = (f"Goes to whatever OBS is set to — Easy Life can't switch "
                        f"destination to {dest}.")
    return action


def _rule_website(clause: str) -> Action | None:
    m = re.match(r"^(?:open|go\s+to|visit|show)\s+(?:the\s+)?"
                 r"(?:website|site|url|page)\s+(.+)$", clause, re.I)
    if m:
        target = _clean(m.group(1))
        if target:
            if not re.match(r"^https?://", target, re.I):
                target = "https://" + target
            return Action(type=ActionType.WEBSITE, target=target,
                          name=f"Open {target}")
    # A bare domain is a website, not an app.
    bare = _clean(re.sub(r"^(?:open|go\s+to|visit)\s+", "", clause, flags=re.I))
    if bare and (DOMAIN_ONLY.match(bare) or URL_RE.match(bare)) and " " not in bare:
        target = bare if re.match(r"^https?://", bare, re.I) else "https://" + bare
        return Action(type=ActionType.WEBSITE, target=target, name=f"Open {bare}")
    return None


def _rule_launch_app(clause: str) -> Action | None:
    m = re.match(r"^(?:open|launch|start|run|fire\s+up)\s+(?:up\s+)?(.+)$", clause, re.I)
    if not m:
        return None
    raw = _clean(re.sub(r"^(?:the|my)\s+", "", _clean(m.group(1)), flags=re.I))
    if not raw:
        return None
    key = raw.lower()
    if key in KNOWN_APPS:
        target = KNOWN_APPS[key]
        if target == "obs":
            target = _obs_exe()
        return Action(type=ActionType.APP, target=target, name=f"Open {raw}")
    # A path, or anything else the platform can resolve.
    return Action(type=ActionType.APP, target=raw, name=f"Open {raw}")


def _rule_close_app(clause: str) -> Action | None:
    m = re.match(r"^(?:close|quit|exit|kill)\s+(?:the\s+)?(.+)$", clause, re.I)
    if not m:
        return None
    raw = _clean(re.sub(r"^(?:the|my)\s+", "", _clean(m.group(1)), flags=re.I))
    if not raw:
        return None
    return Action(type=ActionType.CLOSE, target=raw, name=f"Close {raw}")


def _rule_delay(clause: str) -> Action | None:
    m = re.match(r"^(?:wait|pause|delay|sleep)\s*(?:for\s+)?"
                 r"(\d+(?:\.\d+)?)\s*"
                 r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?$", clause, re.I)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "seconds").lower()
    if unit.startswith("m"):
        value *= 60
    elif unit.startswith("h"):
        value *= 3600
    return Action(type=ActionType.DELAY, params={"seconds": value},
                  name=f"Wait {value:g}s")


def _rule_power(clause: str) -> "Action | list[Action] | None":
    text = _clean(clause)
    # "lock the pc after 10 minutes" is two steps, not one. Dropping the time
    # would silently do something different from what was asked.
    delay = None
    m = re.search(r"\bafter\s+(\d+(?:\.\d+)?)\s*"
                  r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)?\b", text, re.I)
    if m:
        value = float(m.group(1))
        unit = (m.group(2) or "seconds").lower()
        if unit.startswith("m"):
            value *= 60
        elif unit.startswith("h"):
            value *= 3600
        delay = Action(type=ActionType.DELAY, params={"seconds": value},
                       name=f"Wait {value:g}s")
        text = _clean(text[:m.start()] + " " + text[m.end():])

    phrase = _clean(re.sub(r"^(?:please\s+)?", "", text, flags=re.I)).lower()
    phrase = re.sub(r"\b(?:the|my)\s+(?:pc|computer|machine)\b", "", phrase).strip()
    phrase = re.sub(r"^(?:put|take)\s+", "", phrase).strip()
    for word, power in POWER_WORDS.items():
        if phrase == word or phrase.endswith(" " + word):
            action = Action(type=ActionType.POWER, target=power,
                            params={"action": power}, name=f"{power.title()} the PC")
            return [delay, action] if delay else action
    return None


def _rule_command(clause: str) -> Action | None:
    m = re.match(r"^(?:run|execute)\s+(?:the\s+)?(?:command|cmd)\s+(.+)$", clause, re.I)
    if not m:
        return None
    raw = _clean(m.group(1))
    return Action(type=ActionType.COMMAND, target=raw,
                  name=f"Run: {raw[:40]}") if raw else None


RULES: tuple[Callable[[str], "Action | list[Action] | None"], ...] = (
    _rule_obs_scene,
    _rule_stream_stop,      # before start: "stop streaming" contains "stream"
    _rule_stream_start,
    _rule_website,          # before launch_app: a domain is not an app name
    _rule_command,          # before launch_app: "run command X" is not "run X"
    _rule_launch_app,
    _rule_close_app,
    _rule_delay,
    _rule_power,
    _rule_command,
)


def _describe(action: Action) -> str:
    return action.name or action.type.value


def draft(text: str) -> dict:
    """Parse a description into a draft Play. Never saves anything."""
    original = _clean(text)
    if not original:
        return {"name": "", "steps": [], "understood": [], "unmatched": [],
                "notes": ["Type what you want the Play to do."]}

    name, rest = _extract_name(original)
    body = _strip_preamble(rest)

    steps: list[Action] = []
    understood: list[str] = []
    unmatched: list[str] = []
    notes: list[str] = []

    for clause in _split_clauses(body):
        clause = _strip_trailing_join(clause)
        if not clause:
            continue
        produced: list[Action] = []
        for rule in RULES:
            try:
                result = rule(_normalize_verbs(clause))
            except Exception:  # noqa: BLE001 - a bad rule must not kill the draft
                result = None
            if result is None:
                continue
            produced = result if isinstance(result, list) else [result]
            break
        if not produced:
            unmatched.append(clause)
            continue
        for action in produced:
            if action.notes:
                notes.append(action.notes)
            steps.append(action)
            understood.append(f"{clause}  →  {_describe(action)}")

    return {
        "name": name,
        "steps": [s.model_dump(mode="json") for s in steps],
        "understood": understood,
        "unmatched": unmatched,
        "notes": notes,
    }
