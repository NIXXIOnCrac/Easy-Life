"""Tests for the local login system (accounts, sessions, API gating)."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals.config import Config
from pcrituals.auth import AuthManager, AuthError, _hash_password, _verify_password


def make_auth(sub="a"):
    d = Path(tempfile.mkdtemp(prefix=f"pcrituals_auth_{sub}_"))
    cfg = Config(data_dir=d)
    cfg.ensure_dirs()
    return AuthManager(cfg)


class TestPasswordHashing:
    def test_hash_is_not_plaintext(self):
        h = _hash_password("hunter2")
        assert "hunter2" not in h
        assert h.startswith("pbkdf2_sha256$")

    def test_verify_correct_and_wrong(self):
        h = _hash_password("correct horse")
        assert _verify_password("correct horse", h)
        assert not _verify_password("wrong", h)

    def test_unique_salts(self):
        assert _hash_password("same") != _hash_password("same")


class TestAccounts:
    def test_starts_with_no_users(self):
        a = make_auth("no_users")
        assert a.has_users() is False

    def test_create_user(self):
        a = make_auth("create")
        a.create_user("alex", "secret123")
        assert a.has_users() is True
        assert a.user_count() == 1

    def test_rejects_short_password(self):
        a = make_auth("shortpw")
        with pytest.raises(AuthError):
            a.create_user("alex", "ab")

    def test_rejects_duplicate_username(self):
        a = make_auth("dup")
        a.create_user("alex", "secret123")
        with pytest.raises(AuthError):
            a.create_user("alex", "other123")

    def test_verify_credentials(self):
        a = make_auth("verify")
        a.create_user("alex", "secret123")
        assert a.verify("alex", "secret123")
        assert a.verify("alex", "nope") is None
        assert a.verify("ghost", "secret123") is None


class TestSessions:
    def test_login_and_validate(self):
        a = make_auth("sess")
        a.create_user("alex", "secret123")
        res = a.login("alex", "secret123")
        assert res["token"] and res["username"] == "alex"
        sess = a.validate_session(res["token"])
        assert sess and sess["username"] == "alex"

    def test_bad_login_rejected(self):
        a = make_auth("badlogin")
        a.create_user("alex", "secret123")
        with pytest.raises(AuthError):
            a.login("alex", "wrong")

    def test_logout_invalidates(self):
        a = make_auth("logout")
        a.create_user("alex", "secret123")
        tok = a.login("alex", "secret123")["token"]
        a.logout(tok)
        assert a.validate_session(tok) is None

    def test_random_token_rejected(self):
        a = make_auth("randtok")
        a.create_user("alex", "secret123")
        assert a.validate_session("not-a-real-token") is None

    def test_throttle_after_repeated_failures(self):
        a = make_auth("throttle")
        a.create_user("alex", "secret123")
        for _ in range(5):
            with pytest.raises(AuthError):
                a.login("alex", "wrong")
        # Now even the right password is blocked.
        with pytest.raises(AuthError):
            a.login("alex", "secret123")

    def test_change_password_invalidates_sessions(self):
        a = make_auth("chpw")
        a.create_user("alex", "secret123")
        tok = a.login("alex", "secret123")["token"]
        uid = a.verify("alex", "secret123")
        a.change_password(uid, "secret123", "newsecret1")
        assert a.validate_session(tok) is None
        assert a.verify("alex", "newsecret1")


class TestApiGating:
    """Once an account exists, protected endpoints require login."""

    @pytest.fixture
    def client_factory(self, tmp_path, monkeypatch):
        def _make(sub):
            monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path / sub))
            from fastapi.testclient import TestClient
            from pcrituals.api import create_app
            return TestClient(create_app())
        return _make

    def test_setup_flow_and_gating(self, client_factory):
        c = client_factory("api_gate")
        # Fresh: needs setup, and no account exists yet.
        st = c.get("/api/auth/status").json()
        assert st["needs_setup"] is True
        assert st["authenticated"] is False

        # Create the account -> returns a session token.
        r = c.post("/api/auth/setup", json={"username": "alex", "password": "secret123"})
        assert r.status_code == 200
        tok = r.json()["token"]
        assert tok

        # Now protected endpoints require the token.
        assert c.get("/api/rituals").status_code == 401
        assert c.get("/api/rituals", headers={"Authorization": f"Bearer {tok}"}).status_code == 200

        # Status reflects the session.
        st2 = c.get("/api/auth/status", headers={"Authorization": f"Bearer {tok}"}).json()
        assert st2["authenticated"] is True and st2["username"] == "alex"
        assert st2["needs_setup"] is False

        # Setup can't run twice.
        assert c.post("/api/auth/setup", json={"username": "bob", "password": "x12345"}).status_code == 409

    def test_login_after_setup(self, client_factory):
        c = client_factory("api_login")
        c.post("/api/auth/setup", json={"username": "alex", "password": "secret123"})
        # Wrong password -> 401
        assert c.post("/api/auth/login", json={"username": "alex", "password": "bad"}).status_code == 401
        # Right password -> token
        r = c.post("/api/auth/login", json={"username": "alex", "password": "secret123"})
        assert r.status_code == 200 and r.json()["token"]

    def test_logout_via_api(self, client_factory):
        c = client_factory("api_logout")
        tok = c.post("/api/auth/setup", json={"username": "alex", "password": "secret123"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        assert c.post("/api/auth/logout", headers=h).status_code == 200
        assert c.get("/api/rituals", headers=h).status_code == 401

class TestOnboarding:
    def test_new_user_not_onboarded(self):
        a = make_auth("onb1")
        a.create_user("alex", "secret123")
        tok = a.login("alex", "secret123")["token"]
        assert a.validate_session(tok)["onboarded"] is False

    def test_mark_onboarded(self):
        a = make_auth("onb2")
        a.create_user("alex", "secret123")
        tok = a.login("alex", "secret123")["token"]
        uid = a.validate_session(tok)["user_id"]
        a.set_onboarded(uid, True)
        assert a.validate_session(tok)["onboarded"] is True

    def test_status_reports_onboarded_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path / "onb_api"))
        from fastapi.testclient import TestClient
        from pcrituals.api import create_app
        c = TestClient(create_app())
        tok = c.post("/api/auth/setup", json={"username": "alex", "password": "secret123"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        assert c.get("/api/auth/status", headers=h).json()["onboarded"] is False
        assert c.post("/api/auth/onboarded", headers=h).status_code == 200
        assert c.get("/api/auth/status", headers=h).json()["onboarded"] is True
