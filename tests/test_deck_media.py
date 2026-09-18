"""Tests for the Stream Deck, media control, and icon resolution."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["PCRITUALS_DATA_DIR"] = tempfile.mkdtemp(prefix="pcrituals_deck_")

from pcrituals.icons import resolve_icon, suggest_kind, _domain_from_url
from pcrituals.media import MediaController


class TestSuggestKind:
    def test_url_is_website(self):
        assert suggest_kind("https://youtube.com") == "website"
        assert suggest_kind("www.google.com") == "website"

    def test_exe_is_app(self):
        assert suggest_kind(r"C:\Program Files\Discord\Discord.exe") == "app"

    def test_store_url_is_game(self):
        assert suggest_kind("steam://rungameid/570") == "game"
        assert suggest_kind("riot://launch/valorant") == "game"

    def test_media_actions(self):
        assert suggest_kind("play_pause") == "media"
        assert suggest_kind("next") == "media"

    def test_power_actions(self):
        assert suggest_kind("shutdown") == "power"
        assert suggest_kind("lock") == "power"

    def test_known_game_name(self):
        assert suggest_kind("valorant.exe") == "game" or suggest_kind("Valorant") == "game"


class TestResolveIcon:
    def test_website_gets_favicon(self):
        r = resolve_icon("website", "https://www.twitch.tv")
        assert r["type"] == "url"
        assert "twitch.tv" in r["value"]

    def test_known_app_brand(self):
        r = resolve_icon("app", r"C:\Program Files\Discord\Discord.exe")
        assert r["type"] == "url"
        assert "discord" in r["value"]

    def test_game_brand(self):
        r = resolve_icon("game", "steam://rungameid/570")
        assert r["type"] == "url"
        assert "steam" in r["value"] or "steampowered" in r["value"]

    def test_unknown_app_falls_back_to_emoji(self):
        r = resolve_icon("app", r"C:\Some\Obscure\Thing.exe")
        assert r["type"] == "emoji"
        assert r["value"]

    def test_folder_emoji(self):
        r = resolve_icon("file", r"C:\Users\me\Documents")
        assert r["type"] == "emoji"

    def test_domain_parse(self):
        assert _domain_from_url("https://x.com/path") == "x.com"
        assert _domain_from_url("youtube.com") == "youtube.com"


class TestMediaController:
    def test_control_no_crash_off_windows(self):
        m = MediaController()
        # Must never raise on non-Windows (no-op).
        m.control("play_pause")
        m.control("next")
        m.control("previous")
        m.control("stop")

    def test_invalid_action_raises(self):
        m = MediaController()
        import pytest as _p
        with _p.raises(ValueError):
            m.control("nonsense")

    def test_now_playing_returns_none_off_windows(self):
        m = MediaController()
        if sys.platform != "win32":
            assert m.now_playing() is None


class TestDeckAPI:
    """Deck CRUD + press through the API."""

    def _client(self):
        from fastapi.testclient import TestClient
        from pcrituals.api import create_app
        app = create_app()
        token = app.state.pcrituals.security.register_local()
        return TestClient(app), token, app

    def test_deck_crud_and_icon(self):
        c, token, app = self._client()
        h = {"Authorization": f"Bearer {token}"}
        # Create a website button -> auto icon (favicon url)
        r = c.post("/api/deck", headers=h, json={"kind": "website", "target": "https://twitch.tv", "label": "Twitch"})
        assert r.status_code == 200
        bid = r.json()["id"]
        assert r.json()["icon"], "icon should be auto-filled"

        # List contains it
        assert any(b["id"] == bid for b in c.get("/api/deck", headers=h).json())

        # Icon preview endpoint
        ic = c.get("/api/deck/icon", headers=h, params={"kind": "website", "target": "https://youtube.com"}).json()
        assert ic["type"] == "url" and "youtube" in ic["value"]

        # Suggest endpoint
        sg = c.get("/api/deck/suggest", headers=h, params={"target": "steam://rungameid/1"}).json()
        assert sg["kind"] == "game"

        # Update
        r = c.put(f"/api/deck/{bid}", headers=h, json={"kind": "website", "target": "https://reddit.com", "label": "Reddit"})
        assert r.status_code == 200 and r.json()["label"] == "Reddit"

        # Reorder
        assert c.post("/api/deck/reorder", headers=h, json={"ids": [bid]}).status_code == 200

        # Press (media action is safe to press anywhere)
        m = c.post("/api/deck", headers=h, json={"kind": "media", "target": "play_pause", "label": "PP"}).json()
        assert c.post(f"/api/deck/{m['id']}/press", headers=h).status_code == 200

        # Press unknown -> 404
        assert c.post("/api/deck/doesnotexist/press", headers=h).status_code == 404

        # Delete
        assert c.delete(f"/api/deck/{bid}", headers=h).status_code == 200
        assert c.delete(f"/api/deck/{bid}", headers=h).status_code == 404

    def test_media_endpoints(self):
        c, token, app = self._client()
        h = {"Authorization": f"Bearer {token}"}
        assert c.get("/api/media/now", headers=h).status_code == 200
        assert c.post("/api/media/control", headers=h, json={"action": "play_pause"}).status_code == 200
        assert c.post("/api/media/control", headers=h, json={"action": "bogus"}).status_code == 400

class TestAlbumArt:
    def _mock(self, monkeypatch, payload):
        import json
        def fake_urlopen(req, timeout=None):
            class R:
                def read(self): return json.dumps(payload).encode()
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    def test_matching_helpers(self):
        from pcrituals import media
        assert media._artist_matches("Ken Carson", "Ken Carson")
        assert not media._artist_matches("Ken Carson", "Sofia Carson")
        assert media._title_matches("edm", "edm")
        assert not media._title_matches("edm", "delusional")

    def test_deezer_match_returns_cover(self, monkeypatch):
        from pcrituals import media
        media._art_cache.clear()
        self._mock(monkeypatch, {"data": [
            {"artist": {"name": "Ken Carson"}, "title": "edm",
             "album": {"cover_xl": "https://cdn/correct.jpg"}},
        ]})
        assert media.lookup_album_art("Ken Carson", "edm") == "https://cdn/correct.jpg"

    def test_rejects_wrong_song_art(self, monkeypatch):
        """Regression: 'edm' must NOT get the art for a different song."""
        from pcrituals import media
        media._art_cache.clear()
        self._mock(monkeypatch, {"data": [
            {"artist": {"name": "Ken Carson"}, "title": "delusional",
             "album": {"cover_xl": "https://cdn/WRONG.jpg"}},
        ]})
        assert media.lookup_album_art("Ken Carson", "edm") is None

    def test_falls_back_to_itunes_when_deezer_empty(self, monkeypatch):
        from pcrituals import media
        media._art_cache.clear()
        import json
        calls = {"n": 0}
        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            payload = {"data": []} if calls["n"] == 1 else {
                "results": [{"artistName": "The Weeknd", "trackName": "Blinding Lights",
                             "artworkUrl100": "https://x/100x100bb.jpg"}]}
            class R:
                def read(self): return json.dumps(payload).encode()
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert media.lookup_album_art("The Weeknd", "Blinding Lights") == "https://x/600x600bb.jpg"

    def test_lookup_never_raises_on_network_error(self, monkeypatch):
        from pcrituals import media
        media._art_cache.clear()
        def boom(req, timeout=None): raise OSError("no net")
        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert media.lookup_album_art("A", "B") is None

    def test_now_playing_includes_artwork_key(self):
        from pcrituals.media import NowPlaying
        d = NowPlaying(title="T", artist="A").to_dict()
        assert "artwork" in d


class TestDesktopStreams:
    def test_ensure_streams_fixes_none_stdout(self, monkeypatch, tmp_path):
        import desktop, sys, os
        monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        desktop._ensure_streams()
        assert sys.stdout is not None
        assert sys.stderr is not None
        assert hasattr(sys.stdout, "isatty")
