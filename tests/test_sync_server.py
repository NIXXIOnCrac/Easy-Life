"""Tests for the standalone sync server (accounts, tokens, vault revisions).

Everything runs through fastapi.testclient.TestClient against a temp database,
so no real server is started and no other test's data is touched.
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from pcrituals.auth import _hash_token
from pcrituals.sync_server import (
    MAX_VAULT_BYTES,
    SyncConfig,
    SyncServerError,
    SyncStore,
    create_app,
    default_db_path,
)


def make_client(tmp_path, **overrides):
    config = SyncConfig(db_path=Path(tmp_path) / "sync" / "sync.db", **overrides)
    app = create_app(config)
    return TestClient(app), app


def register(client, username="alex", password="secret123"):
    return client.post("/sync/register", json={"username": username, "password": password})


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def sample_vault(name="Morning"):
    return {"version": 1, "exported_at": "2026-01-01T00:00:00+00:00",
            "rituals": [{"name": name, "actions": [{"type": "delay", "target": "1"}]}],
            "deck": [], "settings": {}}


class TestRegisterAndLogin:
    def test_register_returns_token_and_zero_revision(self, tmp_path):
        client, _ = make_client(tmp_path)
        r = register(client)
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == "alex"
        assert body["revision"] == 0
        assert len(body["token"]) > 20

    def test_login_returns_a_new_token(self, tmp_path):
        client, _ = make_client(tmp_path)
        first = register(client).json()["token"]
        r = client.post("/sync/login", json={"username": "alex", "password": "secret123"})
        assert r.status_code == 200
        assert r.json()["token"] != first

    def test_duplicate_username_rejected_case_insensitively(self, tmp_path):
        client, _ = make_client(tmp_path)
        assert register(client, "alex").status_code == 200
        r = client.post("/sync/register", json={"username": "ALEX", "password": "secret123"})
        assert r.status_code == 400
        assert "taken" in r.json()["detail"]

    def test_short_password_rejected(self, tmp_path):
        client, _ = make_client(tmp_path)
        r = client.post("/sync/register", json={"username": "alex", "password": "short"})
        assert r.status_code == 400
        assert "8 characters" in r.json()["detail"]

    def test_username_rules(self, tmp_path):
        client, _ = make_client(tmp_path)
        for bad in ("ab", "a" * 33, "has space", "sla/sh", "emoji😀"):
            r = client.post("/sync/register", json={"username": bad, "password": "secret123"})
            assert r.status_code == 400, bad

    def test_bad_password_is_generic_401(self, tmp_path):
        client, _ = make_client(tmp_path)
        register(client)
        r = client.post("/sync/login", json={"username": "alex", "password": "wrongpass"})
        assert r.status_code == 401
        assert r.json()["detail"] == "invalid username or password"
        r2 = client.post("/sync/login", json={"username": "ghost", "password": "secret123"})
        assert r2.status_code == 401
        # Unknown user and wrong password must be indistinguishable.
        assert r2.json()["detail"] == r.json()["detail"]

    def test_login_rejects_a_non_object_body(self, tmp_path):
        client, _ = make_client(tmp_path)
        r = client.post("/sync/login", json=["not", "an", "object"])
        # FastAPI's own body validation answers first (422); our own guard uses 400.
        assert r.status_code in (400, 422)


class TestTokens:
    def test_token_is_stored_hashed_only(self, tmp_path):
        client, app = make_client(tmp_path)
        token = register(client).json()["token"]
        store = app.state.sync_store
        assert store.token_hashes() == [_hash_token(token)]
        assert token not in store.token_hashes()

    def test_password_is_never_stored_in_plaintext(self, tmp_path):
        client, app = make_client(tmp_path)
        register(client, password="supersecret9")
        row = app.state.sync_store._conn.execute(
            "SELECT pw_hash FROM accounts").fetchone()
        assert "supersecret9" not in row["pw_hash"]
        assert row["pw_hash"].startswith("pbkdf2_sha256$")

    def test_me_requires_a_token(self, tmp_path):
        client, _ = make_client(tmp_path)
        assert client.get("/sync/me").status_code == 401
        assert client.get("/sync/me", headers=auth("bogus")).status_code == 401

    def test_me_returns_username_and_revision(self, tmp_path):
        client, _ = make_client(tmp_path)
        token = register(client).json()["token"]
        body = client.get("/sync/me", headers=auth(token)).json()
        assert body == {"username": "alex", "revision": 0, "updated_at": None}

    def test_expired_token_is_rejected_and_deleted(self, tmp_path):
        client, app = make_client(tmp_path)
        token = register(client).json()["token"]
        store = app.state.sync_store
        store.expire_token(token)
        assert client.get("/sync/me", headers=auth(token)).status_code == 401
        # The dead row is cleaned up rather than lingering.
        assert store.token_hashes() == []

    def test_vault_endpoints_require_a_token(self, tmp_path):
        client, _ = make_client(tmp_path)
        assert client.get("/sync/vault").status_code == 401
        assert client.put("/sync/vault", json={"vault": {}, "base_revision": 0}).status_code == 401
        assert client.put("/sync/vault/force", json={"vault": {}}).status_code == 401
        assert client.request("DELETE", "/sync/account", json={"password": "x"}).status_code == 401


class TestVault:
    def test_never_pushed_vault_is_empty(self, tmp_path):
        client, _ = make_client(tmp_path)
        token = register(client).json()["token"]
        assert client.get("/sync/vault", headers=auth(token)).json() == {
            "vault": None, "revision": 0, "updated_at": None}

    def test_push_then_pull_round_trip(self, tmp_path):
        client, _ = make_client(tmp_path)
        token = register(client).json()["token"]
        vault = sample_vault()
        r = client.put("/sync/vault", json={"vault": vault, "base_revision": 0},
                       headers=auth(token))
        assert r.status_code == 200
        assert r.json()["revision"] == 1

        pulled = client.get("/sync/vault", headers=auth(token)).json()
        assert pulled["vault"]["rituals"][0]["name"] == "Morning"
        assert pulled["revision"] == 1
        assert pulled["updated_at"]

    def test_stale_base_revision_conflicts(self, tmp_path):
        client, _ = make_client(tmp_path)
        token = register(client).json()["token"]
        client.put("/sync/vault", json={"vault": sample_vault(), "base_revision": 0},
                   headers=auth(token))
        r = client.put("/sync/vault", json={"vault": sample_vault("Evening"), "base_revision": 0},
                       headers=auth(token))
        assert r.status_code == 409
        assert r.json()["current_revision"] == 1
        # The conflicting write must not have landed.
        assert client.get("/sync/vault", headers=auth(token)).json()["revision"] == 1

    def test_valid_base_revision_increments(self, tmp_path):
        client, _ = make_client(tmp_path)
        token = register(client).json()["token"]
        client.put("/sync/vault", json={"vault": sample_vault(), "base_revision": 0},
                   headers=auth(token))
        r = client.put("/sync/vault", json={"vault": sample_vault("Evening"), "base_revision": 1},
                       headers=auth(token))
        assert r.status_code == 200
        assert r.json()["revision"] == 2

    def test_force_push_ignores_revision(self, tmp_path):
        client, _ = make_client(tmp_path)
        token = register(client).json()["token"]
        client.put("/sync/vault", json={"vault": sample_vault(), "base_revision": 0},
                   headers=auth(token))
        r = client.put("/sync/vault/force", json={"vault": sample_vault("Forced")},
                       headers=auth(token))
        assert r.status_code == 200
        assert r.json()["revision"] == 2
        assert client.get("/sync/vault", headers=auth(token)).json()["vault"]["rituals"][0]["name"] == "Forced"

    def test_vault_must_be_an_object(self, tmp_path):
        client, _ = make_client(tmp_path)
        token = register(client).json()["token"]
        r = client.put("/sync/vault", json={"vault": "nope", "base_revision": 0},
                       headers=auth(token))
        assert r.status_code == 400

    def test_base_revision_must_be_an_integer(self, tmp_path):
        client, _ = make_client(tmp_path)
        token = register(client).json()["token"]
        r = client.put("/sync/vault", json={"vault": {}, "base_revision": "one"},
                       headers=auth(token))
        assert r.status_code == 400

    def test_oversize_vault_is_413(self, tmp_path):
        client, _ = make_client(tmp_path)
        token = register(client).json()["token"]
        big = {"version": 1, "blob": "x" * (MAX_VAULT_BYTES + 1024)}
        r = client.put("/sync/vault", json={"vault": big, "base_revision": 0},
                       headers=auth(token))
        assert r.status_code == 413
        # And it must not have been stored.
        assert client.get("/sync/vault", headers=auth(token)).json()["revision"] == 0

    def test_size_limit_is_configurable(self, tmp_path):
        client, _ = make_client(tmp_path, max_vault_bytes=64)
        token = register(client).json()["token"]
        r = client.put("/sync/vault/force", json={"vault": {"blob": "y" * 500}},
                       headers=auth(token))
        assert r.status_code == 413


class TestThrottle:
    def test_login_rate_limited_after_five_failures(self, tmp_path):
        client, _ = make_client(tmp_path)
        register(client)
        for _ in range(5):
            r = client.post("/sync/login", json={"username": "alex", "password": "wrongpass"})
            assert r.status_code == 401
        r = client.post("/sync/login", json={"username": "alex", "password": "wrongpass"})
        assert r.status_code == 429
        # Even the correct password is refused while the window is open.
        r = client.post("/sync/login", json={"username": "alex", "password": "secret123"})
        assert r.status_code == 429

    def test_register_attempts_are_rate_limited(self, tmp_path):
        client, _ = make_client(tmp_path)
        for _ in range(5):
            assert client.post(
                "/sync/register",
                json={"username": "alex", "password": "short"}).status_code == 400
        r = client.post("/sync/register", json={"username": "alex", "password": "short"})
        assert r.status_code == 429

    def test_successful_login_clears_failures(self, tmp_path):
        client, app = make_client(tmp_path)
        register(client)
        for _ in range(4):
            client.post("/sync/login", json={"username": "alex", "password": "wrongpass"})
        assert client.post("/sync/login",
                           json={"username": "alex", "password": "secret123"}).status_code == 200
        assert app.state.sync_limiter.allowed("login|alex|testserver")


class TestAccountDeletion:
    def test_delete_removes_account_tokens_and_vault(self, tmp_path):
        client, app = make_client(tmp_path)
        token = register(client).json()["token"]
        client.put("/sync/vault/force", json={"vault": sample_vault()}, headers=auth(token))

        r = client.request("DELETE", "/sync/account", json={"password": "secret123"},
                           headers=auth(token))
        assert r.status_code == 200
        assert r.json() == {"deleted": True}

        store = app.state.sync_store
        assert store.count_accounts() == 0
        assert store.count_tokens() == 0
        assert client.get("/sync/me", headers=auth(token)).status_code == 401
        assert client.post("/sync/login",
                           json={"username": "alex", "password": "secret123"}).status_code == 401

    def test_delete_requires_the_password(self, tmp_path):
        client, _ = make_client(tmp_path)
        token = register(client).json()["token"]
        r = client.request("DELETE", "/sync/account", json={"password": "nope12345"},
                           headers=auth(token))
        assert r.status_code == 403
        assert client.get("/sync/me", headers=auth(token)).status_code == 200


class TestStore:
    def test_open_for_missing_parent_directory(self, tmp_path):
        store = SyncStore(tmp_path / "deep" / "nested" / "s.db")
        assert store.count_accounts() == 0

    def test_duplicate_account_raises(self, tmp_path):
        store = SyncStore(tmp_path / "s.db")
        store.create_account("alex", "secret123")
        with pytest.raises(SyncServerError):
            store.create_account("ALEX", "secret123")

    def test_verify_and_password_matches(self, tmp_path):
        store = SyncStore(tmp_path / "s.db")
        account = store.create_account("alex", "secret123")
        assert store.verify("alex", "secret123") == account["id"]
        assert store.verify("alex", "bad") is None
        assert store.password_matches(account["id"], "secret123")
        assert not store.password_matches(account["id"], "bad")

    def test_vault_round_trip_and_revision(self, tmp_path):
        store = SyncStore(tmp_path / "s.db")
        account = store.create_account("alex", "secret123")
        assert store.get_vault(account["id"])["revision"] == 0
        result = store.put_vault(account["id"], {"a": 1}, base_revision=0)
        assert result["revision"] == 1
        assert store.get_vault(account["id"])["vault"] == {"a": 1}

    def test_force_push_from_zero(self, tmp_path):
        store = SyncStore(tmp_path / "s.db")
        account = store.create_account("alex", "secret123")
        assert store.put_vault(account["id"], {}, force=True)["revision"] == 1

    def test_corrupt_vault_row_does_not_raise(self, tmp_path):
        store = SyncStore(tmp_path / "s.db")
        account = store.create_account("alex", "secret123")
        store._conn.execute(
            "INSERT INTO vaults (account_id, data, revision, updated_at) VALUES (?,?,?,?)",
            (account["id"], "{not json", 3, "now"))
        store._conn.commit()
        assert store.get_vault(account["id"])["vault"] is None

    def test_default_db_path_prefers_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PCRITUALS_SYNC_DB", str(tmp_path / "custom.db"))
        assert default_db_path() == tmp_path / "custom.db"

    def test_default_db_path_lives_under_the_data_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PCRITUALS_SYNC_DB", raising=False)
        monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
        assert default_db_path() == tmp_path / "sync" / "sync.db"


def test_register_stores_expected_columns(tmp_path):
    """Schema sanity: the columns the rest of the code queries must exist."""
    client, app = make_client(tmp_path)
    register(client)
    row = app.state.sync_store._conn.execute(
        "SELECT id, username, pw_hash, created_at FROM accounts").fetchone()
    assert set(row.keys()) == {"id", "username", "pw_hash", "created_at"}
    assert row["created_at"]
    assert json.dumps(dict(row))  # JSON-safe timestamps


def test_tempdir_used_is_isolated(tmp_path):
    """Guard: the fixtures must never fall back to the shared default db."""
    client, app = make_client(tmp_path)
    assert str(app.state.sync_config.db_path).startswith(str(tmp_path))
    with tempfile.TemporaryDirectory() as other:
        client2, app2 = make_client(other)
        register(client2)
        assert str(app2.state.sync_config.db_path).startswith(other)
