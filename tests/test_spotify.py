"""Tests for the Spotify integration (OAuth plumbing + playback mapping)."""
import os
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["PCRITUALS_DATA_DIR"] = tempfile.mkdtemp(prefix="pcrituals_spotify_")

from pcrituals.config import Config
from pcrituals.spotify import SpotifyClient, SpotifyPlayback, SpotifyError, SCOPES


def make_client(tmp_path):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    return SpotifyClient(cfg)


class TestSpotifyConfig:
    def test_starts_unconfigured(self, tmp_path):
        c = make_client(tmp_path)
        assert not c.is_configured()
        assert not c.is_connected()
        st = c.status()
        assert st["configured"] is False and st["connected"] is False

    def test_configure_saves_locally(self, tmp_path):
        c = make_client(tmp_path)
        st = c.configure("my-client-id", "my-secret")
        assert st["configured"] is True
        assert st["client_id"] == "my-client-id"
        # Persisted to disk with restricted perms.
        p = tmp_path / "spotify.json"
        assert p.exists()
        saved = json.loads(p.read_text())
        assert saved["client_id"] == "my-client-id"
        assert saved["client_secret"] == "my-secret"
        if os.name != "nt":
            assert (p.stat().st_mode & 0o777) == 0o600

    def test_disconnect_clears_tokens(self, tmp_path):
        c = make_client(tmp_path)
        c.configure("id", "secret")
        c._data["refresh_token"] = "tok"
        c._save()
        assert c.is_connected()
        c.disconnect()
        assert not c.is_connected()

    def test_default_redirect_is_loopback(self, tmp_path):
        c = make_client(tmp_path)
        assert c.redirect_uri.startswith("http://127.0.0.1:")
        assert c.redirect_uri.endswith("/callback")


class TestAuthUrl:
    def test_auth_url_contains_scopes_and_state(self, tmp_path):
        c = make_client(tmp_path)
        c.configure("abc123", "secret")
        url = c.auth_url()
        assert url.startswith("https://accounts.spotify.com/authorize")
        assert "client_id=abc123" in url
        assert "response_type=code" in url
        assert "127.0.0.1" in url
        assert "user-read-currently-playing" in url.replace("%20", " ")
        assert c._state  # CSRF state generated

    def test_auth_url_requires_credentials(self, tmp_path):
        c = make_client(tmp_path)
        import pytest
        with pytest.raises(SpotifyError):
            c.auth_url()


class TestTokenExchange:
    def test_exchange_rejects_bad_state(self, tmp_path, monkeypatch):
        c = make_client(tmp_path)
        c.configure("id", "secret")
        c._state = "expected"
        import pytest
        with pytest.raises(SpotifyError):
            c.exchange_code("code123", state="WRONG")

    def test_exchange_stores_tokens(self, tmp_path, monkeypatch):
        c = make_client(tmp_path)
        c.configure("id", "secret")
        c._state = "st"
        # Mock the token POST and the /me GET.
        monkeypatch.setattr(c, "_post_token", lambda form: {
            "access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        monkeypatch.setattr(c, "_get", lambda path, timeout=10: {"display_name": "Tester"})
        st = c.exchange_code("code123", state="st")
        assert st["connected"] is True
        assert st["user"] == "Tester"
        assert c._data["access_token"] == "AT"


class TestPlayback:
    def test_now_playing_maps_fields(self, tmp_path, monkeypatch):
        c = make_client(tmp_path)
        c.configure("id", "secret")
        c._data["refresh_token"] = "RT"
        c._data["access_token"] = "AT"
        c._data["expires_at"] = 9e18  # far future, no refresh
        monkeypatch.setattr(c, "_get", lambda path, timeout=10: {
            "is_playing": True,
            "progress_ms": 42000,
            "item": {
                "name": "edm",
                "duration_ms": 180000,
                "id": "track1",
                "artists": [{"name": "Ken Carson"}],
                "album": {"name": "xperiment", "images": [{"url": "https://art/cover.jpg"}]},
            },
        })
        pb = c.now_playing()
        d = pb.to_dict()
        assert d["title"] == "edm"
        assert d["artist"] == "Ken Carson"
        assert d["artwork"] == "https://art/cover.jpg"
        assert d["position"] == 42.0
        assert d["duration"] == 180.0
        assert d["is_playing"] is True

    def test_now_playing_none_when_idle(self, tmp_path, monkeypatch):
        c = make_client(tmp_path)
        c._data["refresh_token"] = "RT"
        c._data["access_token"] = "AT"
        c._data["expires_at"] = 9e18
        monkeypatch.setattr(c, "_get", lambda path, timeout=10: None)
        assert c.now_playing() is None

    def test_control_rejects_unknown(self, tmp_path):
        c = make_client(tmp_path)
        c._data["refresh_token"] = "RT"
        c._data["access_token"] = "AT"
        c._data["expires_at"] = 9e18
        import pytest
        with pytest.raises(SpotifyError):
            c.control("nonsense")


class TestScopes:
    def test_required_scopes_present(self):
        for s in ("user-read-currently-playing", "user-read-playback-state",
                  "user-modify-playback-state"):
            assert s in SCOPES