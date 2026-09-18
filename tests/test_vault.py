"""Tests for the vault (build_vault / apply_vault).

Uses a real App on a temp data dir so the import path — including the
dangerous-command block in app.import_rituals — is exercised for real.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals.app import App
from pcrituals.config import Config
from pcrituals.vault import VAULT_VERSION, apply_vault, build_vault


@pytest.fixture
def app(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    return App(cfg)


def ritual(name="Morning", action=None):
    return {"name": name, "actions": [action or {"type": "delay", "params": {"seconds": 1}}]}


def add_ritual(app, name="Morning"):
    return app.create_ritual(ritual(name))


def add_deck(app, label="Lock", kind="power", target="lock"):
    return app.save_deck_button({"label": label, "kind": kind, "target": target})


class TestBuildVault:
    def test_shape_and_version(self, app):
        vault = build_vault(app)
        assert vault["version"] == VAULT_VERSION
        assert set(vault) == {"version", "exported_at", "rituals", "deck", "settings"}
        assert vault["exported_at"]

    def test_is_json_serialisable(self, app):
        add_ritual(app)
        add_deck(app)
        assert json.dumps(build_vault(app))

    def test_carries_rituals_as_importable_objects(self, app):
        """Guards a real bug: export_imports() dumps Ritual models with
        default=str, so a naive json.loads gives repr strings that cannot be
        imported again. The vault must always hold objects."""
        add_ritual(app, "Morning")
        vault = build_vault(app)
        assert len(vault["rituals"]) == 1
        assert isinstance(vault["rituals"][0], dict)
        assert vault["rituals"][0]["name"] == "Morning"
        assert isinstance(vault["rituals"][0]["actions"], list)

    def test_uses_export_imports_when_it_returns_objects(self, app):
        class FakeApp:
            def export_imports(self):
                return json.dumps({"version": 1, "rituals": [{"name": "FromExport",
                                                              "actions": []}]})

            class store:  # noqa: N801 - minimal stand-in
                @staticmethod
                def list_rituals():
                    raise AssertionError("should not need the store")

                @staticmethod
                def list_deck():
                    return []

            @staticmethod
            def get_settings():
                return {}

        vault = build_vault(FakeApp())
        assert vault["rituals"][0]["name"] == "FromExport"

    def test_deck_buttons_are_dicts(self, app):
        add_deck(app, "Lock")
        deck = build_vault(app)["deck"]
        assert len(deck) == 1
        assert deck[0]["label"] == "Lock"
        assert deck[0]["kind"] == "power"

    def test_settings_strip_credentials(self, app, monkeypatch):
        monkeypatch.setattr(app, "get_settings", lambda: {
            "update_url": "http://example.test/update.json",
            "session_token": "abc",
            "device_token": "def",
            "pairing_secret": "ghi",
            "password": "hunter2",
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "nested": {"api_token": "xyz", "keep": 1},
            "list": [{"secret": "no"}, {"keep": "yes"}],
        })
        vault = build_vault(app)
        settings = vault["settings"]
        assert settings["update_url"] == "http://example.test/update.json"
        for leaked in ("session_token", "device_token", "pairing_secret",
                       "password", "mac_address"):
            assert leaked not in settings
        assert settings["nested"] == {"keep": 1}
        # A secret nested in a list is stripped; the harmless sibling survives.
        assert settings["list"][1] == {"keep": "yes"}
        assert "no" not in json.dumps(settings)
        assert json.dumps(build_vault(app))  # still serialisable after stripping

    def test_vault_is_small_and_free_of_local_state(self, app):
        add_ritual(app)
        vault = build_vault(app)
        # No backup list, no MAC, no device state anywhere in the payload.
        blob = json.dumps(vault).lower()
        for fragment in ("mac_address", "backup", "device_token", "pairing"):
            assert fragment not in blob


class TestApplyVault:
    def test_replace_wipes_then_imports(self, app):
        add_ritual(app, "Local")
        add_deck(app, "Local button")
        vault = {"version": 1, "rituals": [ritual("FromVault")],
                 "deck": [{"label": "Vault button", "kind": "power", "target": "lock"}],
                 "settings": {}}

        result = apply_vault(app, vault, mode="replace")
        assert result["mode"] == "replace"
        assert result["rituals"] == 1
        assert result["deck"] == 1
        assert result["errors"] == []
        assert [r["name"] for r in app.list_rituals()] == ["FromVault"]
        assert [b["label"] for b in app.list_deck()] == ["Vault button"]

    def test_merge_keeps_existing(self, app):
        add_ritual(app, "Local")
        vault = {"version": 1, "rituals": [ritual("FromVault")], "deck": [], "settings": {}}
        result = apply_vault(app, vault, mode="merge")
        assert result["mode"] == "merge"
        names = sorted(r["name"] for r in app.list_rituals())
        assert names == ["FromVault", "Local"]

    def test_default_mode_is_replace(self, app):
        add_ritual(app, "Local")
        apply_vault(app, {"version": 1, "rituals": [], "deck": [], "settings": {}})
        assert app.list_rituals() == []

    def test_round_trip_through_a_vault(self, app):
        add_ritual(app, "Morning")
        add_deck(app, "Lock")
        vault = build_vault(app)

        # Wipe locally via replace, then apply the vault back.
        result = apply_vault(app, vault, mode="replace")
        assert result["errors"] == []
        assert [r["name"] for r in app.list_rituals()] == ["Morning"]
        assert [b["label"] for b in app.list_deck()] == ["Lock"]

    def test_imported_rituals_get_fresh_ids(self, app):
        add_ritual(app, "Morning")
        original_id = app.list_rituals()[0]["id"]
        vault = build_vault(app)
        apply_vault(app, vault, mode="merge")
        assert original_id in [r["id"] for r in app.list_rituals()]
        assert len(app.list_rituals()) == 2  # merged copy, not an overwrite

    def test_deck_buttons_get_new_ids(self, app):
        button = add_deck(app, "Lock")
        vault = build_vault(app)
        apply_vault(app, vault, mode="replace")
        recreated = app.list_deck()
        assert len(recreated) == 1
        assert recreated[0]["id"] != button["id"]
        assert recreated[0]["position"] == 0

    def test_dangerous_command_is_blocked_and_reported(self, app):
        vault = {"version": 1,
                 "rituals": [ritual("Bad", {"type": "command", "target": "rm -rf /"}),
                             ritual("Good")],
                 "deck": [], "settings": {}}
        result = apply_vault(app, vault, mode="replace")
        assert result["rituals"] == 1
        assert len(result["errors"]) == 1
        assert "dangerous command" in result["errors"][0]
        assert [r["name"] for r in app.list_rituals()] == ["Good"]

    def test_bad_deck_entry_is_reported_not_fatal(self, app):
        vault = {"version": 1, "rituals": [], "deck": [
            {"label": "Bad", "kind": "not-a-kind", "target": "x"},
            {"label": "Fine", "kind": "power", "target": "lock"},
        ], "settings": {}}
        result = apply_vault(app, vault, mode="replace")
        assert result["deck"] == 1
        assert len(result["errors"]) == 1
        assert [b["label"] for b in app.list_deck()] == ["Fine"]

    def test_unknown_settings_keys_are_ignored(self, app):
        vault = {"version": 1, "rituals": [], "deck": [],
                 "settings": {"update_url": "http://example.test/u.json",
                              "something_unknown": "nope"}}
        result = apply_vault(app, vault, mode="replace")
        assert result["settings"] == 1
        assert app.get_settings()["update_url"] == "http://example.test/u.json"

    def test_secret_settings_are_not_applied(self, app):
        vault = {"version": 1, "rituals": [], "deck": [],
                 "settings": {"session_token": "abc", "password": "hunter2"}}
        result = apply_vault(app, vault, mode="replace")
        assert result["settings"] == 0
        assert result["errors"] == []

    def test_returns_the_documented_keys(self, app):
        result = apply_vault(app, {"version": 1, "rituals": [], "deck": [], "settings": {}})
        assert set(result) == {"mode", "rituals", "deck", "settings", "errors"}
        assert isinstance(result["errors"], list)


class TestValidation:
    @pytest.mark.parametrize("bad", [
        ["not", "a", "dict"],
        "nope",
        None,
        42,
    ])
    def test_non_object_vault_rejected(self, app, bad):
        with pytest.raises(ValueError):
            apply_vault(app, bad)

    def test_wrong_version_rejected(self, app):
        with pytest.raises(ValueError):
            apply_vault(app, {"version": 2, "rituals": [], "deck": []})

    def test_missing_version_rejected(self, app):
        with pytest.raises(ValueError):
            apply_vault(app, {"rituals": [], "deck": []})

    def test_non_list_rituals_rejected(self, app):
        with pytest.raises(ValueError):
            apply_vault(app, {"version": 1, "rituals": "nope", "deck": []})

    def test_non_list_deck_rejected(self, app):
        with pytest.raises(ValueError):
            apply_vault(app, {"version": 1, "rituals": [], "deck": {}})

    def test_bad_mode_rejected(self, app):
        with pytest.raises(ValueError):
            apply_vault(app, {"version": 1, "rituals": [], "deck": []}, mode="overwrite")

    def test_invalid_vault_leaves_local_data_alone(self, app):
        add_ritual(app, "Local")
        with pytest.raises(ValueError):
            apply_vault(app, {"version": 9, "rituals": []})
        assert [r["name"] for r in app.list_rituals()] == ["Local"]
