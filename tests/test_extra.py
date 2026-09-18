"""Tests for voice parsing, Wake-on-LAN, and integration detection wiring."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["PCRITUALS_DATA_DIR"] = tempfile.mkdtemp(prefix="pcrituals_extra_")

from pcrituals.voice import VoiceEngine
from pcrituals.wol import WakeOnLan


def make_engine(names=("gaming", "work", "streaming")):
    return VoiceEngine(rituals_provider=lambda: list(names))


class TestVoice:
    def test_start_ritual(self):
        e = make_engine()
        c = e.parse("start gaming")
        assert c is not None
        assert c.intent == "start_ritual"
        assert c.ritual_name == "gaming"

    def test_run_ritual_alias(self):
        e = make_engine()
        c = e.parse("hey run my work routine")
        assert c.intent == "start_ritual"
        assert c.ritual_name == "work"

    def test_stop_ritual(self):
        e = make_engine()
        c = e.parse("stop streaming")
        assert c.intent == "stop_ritual"
        assert c.ritual_name == "streaming"

    def test_lock(self):
        assert make_engine().parse("lock the pc").intent == "lock"

    def test_sleep(self):
        assert make_engine().parse("put my pc to sleep").intent == "sleep"

    def test_restart_requires_confirm(self):
        c = make_engine().parse("please restart the computer")
        assert c.intent == "restart"
        assert c.needs_confirmation is True

    def test_shutdown_requires_confirm(self):
        c = make_engine().parse("shut down")
        assert c.intent == "shutdown"
        assert c.needs_confirmation is True

    def test_no_match_returns_none(self):
        assert make_engine().parse("what is the weather") is None

    def test_case_insensitive(self):
        c = make_engine().parse("START Gaming NOW")
        assert c.intent == "start_ritual"


class TestWakeOnLan:
    def test_packet_construction(self):
        w = WakeOnLan()
        mac = w._parse_mac("AA:BB:CC:DD:EE:FF")
        assert len(mac) == 6
        payload = b"\xff" * 6 + mac * 16
        assert len(payload) == 102
        assert payload[:6] == b"\xff" * 6
        # The 6 MAC bytes repeat 16 times.
        assert payload[6:12] == payload[12:18] == mac

    def test_mac_formats(self):
        w = WakeOnLan()
        assert w._parse_mac("aabb.ccdd.eeff") == w._parse_mac("aa:bb:cc:dd:ee:ff")
        assert w._parse_mac("AA-BB-CC-DD-EE-FF") == w._parse_mac("aa:bb:cc:dd:ee:ff")

    def test_bad_mac_rejected(self):
        w = WakeOnLan()
        import pytest as _p
        with _p.raises(ValueError):
            w._parse_mac("ZZ:ZZ")
        with _p.raises(ValueError):
            w._parse_mac("aa:bb")  # too short
        with _p.raises(ValueError):
            w._parse_mac("aa:bb:cc:dd:ee:ff:00")

    def test_integration_export_shape(self):
        from pcrituals.integrations import detect_integrations
        result = detect_integrations()
        assert isinstance(result, list)
        # Every integration has the expected fields.
        for item in result:
            assert all(k in item for k in
                       ("id", "name", "kind", "installed", "path", "detected_on", "scheme"))
        # On non-Windows, installed should be None (not faked).
        if sys.platform != "win32":
            assert all(i["installed"] is None for i in result)


class TestBackup:
    def test_create_list_restore(self, tmp_path):
        from pcrituals.config import Config
        from pcrituals.backup import BackupManager
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()

        # Seed some state.
        from pcrituals.storage import Store
        from pcrituals.models import Ritual, Action
        store = Store(cfg)
        r = Ritual(name="Original", actions=[Action.website("https://x.com")])
        store.save_ritual(r)

        bm = BackupManager(cfg)
        info = bm.create_backup("manual")
        assert info["files"] >= 1
        backups = bm.list_backups()
        assert len(backups) == 1

        # Mutate state.
        store.save_ritual(Ritual(name="Changed"))

        # Restore then re-read (need a fresh Store to see restored data).
        bm.restore(backups[0]["name"])
        store2 = Store(cfg)
        names = [x.name for x in store2.list_rituals()]
        assert "Original" in names
        assert "Changed" not in names

    def test_delete_and_prune(self, tmp_path):
        from pcrituals.config import Config
        from pcrituals.backup import BackupManager
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()
        bm = BackupManager(cfg)
        for i in range(3):
            bm.create_backup(f"b{i}")
        assert len(bm.list_backups()) == 3
        pruned = bm.prune(keep=2)
        assert pruned == 1
        assert len(bm.list_backups()) == 2
        assert bm.delete_backup(bm.list_backups()[0]["name"])
        assert len(bm.list_backups()) == 1

class TestUpdater:
    def test_version_parsing(self):
        from pcrituals.update import parse_version, is_newer
        assert parse_version("0.2.0") == (0, 2, 0, "")
        assert parse_version("v1.10.2") == (1, 10, 2, "")
        assert parse_version("0.2.0-beta") == (0, 2, 0, "beta")
        assert parse_version("junk") == (0, 0, 0, "")
        # ordering
        assert is_newer("0.2.0", "0.1.9") is True
        assert is_newer("0.1.9", "0.2.0") is False
        assert is_newer("0.2.0", "0.2.0") is False
        assert is_newer("0.2.1", "0.2.0") is True
        assert is_newer("0.2.0", "0.2.0-beta") is True   # stable beats prerelease
        assert is_newer("0.2.0-beta", "0.2.0") is False

    def test_check_no_url_returns_none(self):
        from pcrituals.update import Updater
        u = Updater("")  # no manifest URL
        assert u.check() is None

    def test_check_same_or_older_version_returns_none(self, tmp_path, monkeypatch):
        from pcrituals.update import Updater
        import json
        def fake_urlopen(req, timeout=None, context=None):
            class R:
                def read(self):
                    return json.dumps({"version": "0.0.1"}).encode()
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        u = Updater("https://example.com/update.json", current_version="0.1.0")
        assert u.check() is None  # 0.0.1 is older than 0.1.0

    def test_check_newer_returns_info(self, tmp_path, monkeypatch):
        from pcrituals.update import Updater
        import json
        def fake_urlopen(req, timeout=None, context=None):
            class R:
                def read(self):
                    return json.dumps({"version": "0.9.0", "url": "https://x/upd.zip", "notes": "big update"}).encode()
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        u = Updater("https://example.com/update.json", current_version="0.1.0")
        info = u.check()
        assert info is not None
        assert info.version == "0.9.0"
        assert info.url == "https://x/upd.zip"

    def test_apply_refuses_on_non_windows(self, monkeypatch):
        from pcrituals.update import Updater, UpdateInfo, UpdateError
        monkeypatch.setattr("sys.platform", "linux")
        u = Updater("https://x/update.json", current_version="0.1.0")
        info = UpdateInfo(version="0.2.0", url="https://x/upd.zip")
        try:
            u.apply(info)
            assert False, "should have raised UpdateError"
        except UpdateError as e:
            assert "Windows" in str(e)

    def test_apply_rejects_downgrade(self, monkeypatch):
        from pcrituals.update import Updater, UpdateInfo, UpdateError
        monkeypatch.setattr("sys.platform", "win32")
        u = Updater("https://x/update.json", current_version="0.5.0")
        info = UpdateInfo(version="0.2.0", url="https://x/upd.zip")
        import pathlib, tempfile
        p = pathlib.Path(tempfile.gettempdir()) / "dummy-upd.zip"
        p.write_bytes(b"x")
        info.downloaded_to = p
        try:
            u.apply(info)
            assert False
        except UpdateError as e:
            assert "downgrade" in str(e)


class TestDesktop:
    def test_imports_and_helpers(self):
        import desktop
        # A closed port should read as not-open / not-ready.
        assert desktop._port_open("127.0.0.1", 1) is False
        assert desktop._wait_for_server("127.0.0.1", 1, timeout=0.4) is False
        # Constants present
        assert desktop.APP_TITLE == "Easy Life"

    def test_frozen_data_dir_logic(self, monkeypatch, tmp_path):
        import desktop, sys
        # Not frozen -> should not set a data dir
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delenv("PCRITUALS_DATA_DIR", raising=False)
        desktop._prepare_data_dir()
        import os
        assert "PCRITUALS_DATA_DIR" not in os.environ


class TestUpdaterInstallDir:
    def test_env_var_wins(self, monkeypatch, tmp_path):
        from pcrituals.update import Updater
        monkeypatch.setenv("PCRITUALS_INSTALL_DIR", str(tmp_path))
        u = Updater("", install_dir=None)
        assert u.install_dir == tmp_path

    def test_source_run_uses_project_root_not_venv(self, monkeypatch):
        from pcrituals.update import Updater
        monkeypatch.delenv("PCRITUALS_INSTALL_DIR", raising=False)
        u = Updater("", install_dir=None)
        # Should be the package's parent dir (the project root), never venv/Scripts.
        assert u.install_dir.name != "Scripts"
        assert (u.install_dir / "pcrituals").exists() or u.install_dir.name == "pc-rituals"

    def test_explicit_install_dir_wins(self, monkeypatch, tmp_path):
        from pcrituals.update import Updater
        monkeypatch.setenv("PCRITUALS_INSTALL_DIR", "/somewhere/else")
        u = Updater("", install_dir=tmp_path)
        assert u.install_dir == tmp_path
