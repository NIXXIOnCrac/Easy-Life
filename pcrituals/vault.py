"""The syncable snapshot of a user's data (the "vault").

Why this module exists
----------------------
Sync needs ONE canonical, credential-free bundle of everything worth carrying
between two machines: rituals, deck buttons and user settings. Wiring that
bundle together in the API layer would put the "what is safe to upload" policy
next to routing code, where it is easy to forget; putting it here keeps the
policy in one testable place.

A vault is deliberately NOT a full backup. Pairing tokens, device tokens,
session tokens, the backup list and the PC's own MAC address all describe *this*
machine: copying them to another PC either hands an attacker working credentials
or makes the second PC impersonate the first. So anything under a key whose name
looks secret is stripped before the vault can leave the machine, and the file
list/MAC are simply not exported at all.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

VAULT_VERSION = 1

# Key-name fragments that mark device/account credentials. Checked
# case-insensitively against every dict key, at every nesting depth.
_SECRET_KEY_MARKERS = ("token", "secret", "password", "mac")


def build_vault(app: Any) -> dict:
    """Build a JSON-serialisable vault from a live App instance.

    `app` is duck-typed (export_imports/import_rituals/get_settings/store) so a
    test double with the same surface works.
    """
    exported = json.loads(app.export_imports())
    rituals = _exported_rituals(app, exported)

    deck = [button.model_dump(mode="json") for button in app.store.list_deck()]
    settings = _strip_secrets(app.get_settings())

    vault = {
        "version": VAULT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "rituals": rituals,
        "deck": deck,
        "settings": settings,
    }
    # Round-trip through json so any datetime/Path left in a model dump becomes a
    # plain string now, instead of exploding later in the HTTP layer.
    return json.loads(json.dumps(vault, default=str))


def _exported_rituals(app: Any, exported: dict) -> list:
    """The ritual objects from export_imports(), guaranteed to be dicts.

    export_imports() dumps the Ritual models with json.dumps(default=str), which
    turns them into repr strings rather than objects. Importing reprs would
    fail ("entry missing name/actions") and push a useless vault, so whenever an
    entry is not an object we fall back to the store's JSON dump. That keeps
    this module correct with the current export AND with a future fix to it.
    """
    rituals = exported.get("rituals", []) if isinstance(exported, dict) else []
    if isinstance(rituals, list) and all(isinstance(item, dict) for item in rituals):
        return rituals
    return [ritual.model_dump(mode="json") for ritual in app.store.list_rituals()]


def apply_vault(app: Any, vault: dict, *, mode: str = "replace") -> dict:
    """Write a vault into the local app.

    mode="replace" wipes local rituals+deck first (the vault is the truth),
    mode="merge" keeps what is there and adds the vault's items.

    Returns {"mode", "rituals", "deck", "settings", "errors"} where the counts
    are how many items were written and "errors" lists per-item failures
    (including the dangerous-command blocks reported by import_rituals).
    """
    _validate(vault, mode)
    errors: list[str] = []

    if mode == "replace":
        for ritual in app.store.list_rituals():
            app.store.delete_ritual(ritual.id)
        for button in app.store.list_deck():
            app.store.delete_deck_button(button.id)

    # Rituals go through the app's own importer: it re-validates the models and
    # refuses destructive commands. Bypassing it here would make a vault a way
    # to smuggle "format c:" onto a machine.
    try:
        imported = app.import_rituals({"rituals": vault.get("rituals", [])})
        rituals = int(imported.get("imported", 0))
        errors.extend(imported.get("errors", []) or [])
    except Exception as exc:  # noqa: BLE001 - surface, never abort the whole apply
        rituals = 0
        errors.append(f"rituals: {exc}")

    deck = _restore_deck(app, vault.get("deck", []), errors)
    settings = _apply_settings(app, vault.get("settings", {}), errors)

    return {"mode": mode, "rituals": rituals, "deck": deck,
            "settings": settings, "errors": errors}


# ---- internals -------------------------------------------------------------

def _validate(vault: Any, mode: str) -> None:
    if mode not in ("replace", "merge"):
        raise ValueError("mode must be 'replace' or 'merge'")
    if not isinstance(vault, dict):
        raise ValueError("vault must be an object")
    if vault.get("version") != VAULT_VERSION:
        raise ValueError(f"unsupported vault version: {vault.get('version')!r}")
    for key in ("rituals", "deck"):
        value = vault.get(key)
        if value is not None and not isinstance(value, list):
            raise ValueError(f"'{key}' must be a list")


def _restore_deck(app: Any, items: Iterable[Any], errors: list[str]) -> int:
    """Recreate deck buttons locally with fresh ids.

    Fresh ids matter: the local deck may already hold the buttons from the
    exporting machine (merge mode), and reusing an id would silently overwrite
    an unrelated button.
    """
    from pcrituals.models import DeckButton

    saved = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append("deck entry is not an object")
            continue
        payload = dict(item)
        payload.pop("id", None)
        payload["id"] = uuid.uuid4().hex[:12]
        payload["position"] = index
        try:
            # Route through app.save_deck_button, NOT store.save_deck_button:
            # the store has no idea a command button can be destructive, while
            # the app applies the same dangerous-command guard a ritual gets.
            # Bypassing it here would make a vault a way to smuggle `rm -rf /`
            # onto the deck and run it on press.
            app.save_deck_button(payload)
            saved += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"deck '{payload.get('label') or payload.get('target')}': {exc}")
    return saved


def _apply_settings(app: Any, incoming: Any, errors: list[str]) -> int:
    """Apply only settings keys the local app already knows about.

    Copying unknown keys blindly would let a vault introduce config the app has
    never validated (and would happily persist it forever).
    """
    if not isinstance(incoming, dict):
        return 0
    current = app.get_settings()
    if not isinstance(current, dict):
        return 0
    updates = {
        key: value for key, value in incoming.items()
        if key in current and not _is_secret_key(key)
    }
    if not updates:
        return 0
    try:
        app.set_settings(updates)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"settings: {exc}")
        return 0
    return len(updates)


def _strip_secrets(value: Any) -> Any:
    """Recursively drop credential-shaped keys from a settings tree."""
    if isinstance(value, dict):
        return {k: _strip_secrets(v) for k, v in value.items() if not _is_secret_key(k)}
    if isinstance(value, list):
        return [_strip_secrets(v) for v in value]
    return value


def _is_secret_key(key: Any) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)
