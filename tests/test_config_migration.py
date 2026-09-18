"""F19 regression: legacy data-dir adoption must not adopt a stranger's account.

`migrate_legacy_data()` exists so an upgrade (source install -> packaged exe, or
an old data location) keeps the user's rituals. It used to include
`tempfile.gettempdir()/pcrituals` as a source, and it copied the whole database
— so on Linux a brand-new install adopted `/tmp/pcrituals/pcrituals.db`,
including its `users` and `sessions` tables: anyone who could write to
world-writable `/tmp` could plant the account the app then adopted.

Each test below pins one rule of the fix:
  * a data directory under the system temp area is never a migration source;
  * a source that is group-/world-writable, or owned by another user, or a
    symlink is refused;
  * a target that already holds files (or a database) is never written over;
  * credential tables and credential files are never adopted — only ritual
    data, so an upgraded install starts with no account.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pcrituals import config as config_mod
from pcrituals.config import Config, migrate_legacy_data

# pytest's tmp_path lives *under* the system temp directory, so tests that need
# a "trusted" source directory point the temp roots somewhere harmless. The
# temp-skip test (below) uses the real temp root on purpose.
NO_TEMP = ("_no_such_temp_dir_",)


def trusted_tmp(monkeypatch, tmp_path: Path) -> None:
    """Make `tmp_path` count as a normal (non-temp) directory."""
    monkeypatch.setattr(config_mod, "_temp_roots",
                        lambda: [tmp_path / NO_TEMP[0]])


def use_legacy_source(monkeypatch, legacy: Path) -> None:
    """Point the migration at exactly one candidate directory."""
    monkeypatch.setattr(config_mod, "_legacy_data_dirs", lambda: [legacy])


def target_is_empty(path: Path) -> bool:
    """A brand-new target: either never created, or only empty dirs (the
    migration itself calls ensure_dirs(), which makes `rituals/`)."""
    if not path.exists():
        return True
    return all(entry.is_dir() and not any(entry.iterdir())
               for entry in path.iterdir())


def _plant_legacy(path: Path, *, ritual_name: str = "KEPT", with_account: bool = True) -> Config:
    from pcrituals.auth import AuthManager
    from pcrituals.models import Action, Ritual
    from pcrituals.security import Security
    from pcrituals.storage import Store

    cfg = Config(data_dir=path)
    cfg.ensure_dirs()
    Store(cfg).save_ritual(
        Ritual(name=ritual_name, actions=[Action.website("https://example.com")]))
    if with_account:
        AuthManager(cfg).create_user("planted", "hunter2")
    Security(cfg)  # creates the devices + pairing_codes tables
    # Credential files that must never travel with a migration.
    (cfg.data_dir / "server_secret").write_text("planted-secret")
    (cfg.data_dir / "settings.json").write_text('{"update_url": ""}')
    return cfg


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(str(db))
    try:
        return {str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 1. the temp directory is not a migration source at all
# --------------------------------------------------------------------------
def test_temp_dir_is_not_a_legacy_candidate():
    """The reviewer's repro: `/tmp/pcrituals/pcrituals.db` exists, so a fresh
    data dir came up already containing another directory's database."""
    temp = Path(config_mod.tempfile.gettempdir())
    candidates = [p.resolve() for p in config_mod._legacy_data_dirs()]
    assert all(temp not in c.parents and c != temp for c in candidates)
    assert "lives in the system temp directory" == config_mod.legacy_dir_rejection(
        temp / "pcrituals")


def test_planted_database_in_the_temp_dir_is_not_adopted(monkeypatch, tmp_path):
    """A brand-new data dir must stay empty even when a `pcrituals.db` is sitting
    in the temp area — that path is world-writable, so the database may be an
    attacker's, users/sessions included."""
    fake_temp = tmp_path / "faketmp"
    planted = fake_temp / "pcrituals"
    planted.mkdir(parents=True)
    _plant_legacy(planted)
    # Both the candidate list and the temp check resolve the temp dir now.
    monkeypatch.setattr(config_mod.tempfile, "gettempdir", lambda: str(fake_temp))
    use_legacy_source(monkeypatch, planted)

    target = tmp_path / "brand_new"
    cfg = Config(data_dir=target)
    assert migrate_legacy_data(cfg) is False
    assert not cfg.db_file.exists()
    assert not (target / "server_secret").exists()
    assert target_is_empty(target)


# --------------------------------------------------------------------------
# 2. only directories we can verify are ours
# --------------------------------------------------------------------------
def test_group_or_world_writable_legacy_dir_is_refused(monkeypatch, tmp_path):
    trusted_tmp(monkeypatch, tmp_path)
    legacy = tmp_path / "sloppy"
    legacy.mkdir()
    _plant_legacy(legacy)
    os.chmod(legacy, 0o777)
    use_legacy_source(monkeypatch, legacy)
    assert config_mod.legacy_dir_rejection(legacy) == "is group- or world-writable"

    cfg = Config(data_dir=tmp_path / "fresh")
    assert migrate_legacy_data(cfg) is False
    assert not cfg.db_file.exists()


def test_legacy_dir_owned_by_another_user_is_refused(monkeypatch, tmp_path):
    trusted_tmp(monkeypatch, tmp_path)
    legacy = tmp_path / "someone_elses"
    legacy.mkdir()
    _plant_legacy(legacy)
    real_uid = legacy.stat().st_uid
    monkeypatch.setattr(config_mod.os, "getuid", lambda: real_uid + 1, raising=False)
    use_legacy_source(monkeypatch, legacy)

    reason = config_mod.legacy_dir_rejection(legacy)
    assert reason is not None and "owned by another user" in reason
    cfg = Config(data_dir=tmp_path / "fresh")
    assert migrate_legacy_data(cfg) is False
    assert not cfg.db_file.exists()


def test_symlinked_legacy_dir_is_refused(monkeypatch, tmp_path):
    trusted_tmp(monkeypatch, tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    _plant_legacy(real)
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert config_mod.legacy_dir_rejection(link) == "is a symlink"


def test_a_directory_without_a_rituals_database_is_not_adopted(monkeypatch, tmp_path):
    trusted_tmp(monkeypatch, tmp_path)
    legacy = tmp_path / "not_a_db"
    legacy.mkdir()
    (legacy / "pcrituals.db").write_text("this is not sqlite")  # planted junk
    use_legacy_source(monkeypatch, legacy)
    cfg = Config(data_dir=tmp_path / "fresh")
    assert migrate_legacy_data(cfg) is False
    assert not cfg.db_file.exists()


# --------------------------------------------------------------------------
# 3. the user's own data always wins
# --------------------------------------------------------------------------
def test_never_adopts_over_a_target_that_holds_files(monkeypatch, tmp_path):
    trusted_tmp(monkeypatch, tmp_path)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _plant_legacy(legacy, ritual_name="THEIRS")

    target = tmp_path / "mine"
    target.mkdir()
    (target / "settings.json").write_text('{"update_url": ""}')
    use_legacy_source(monkeypatch, legacy)

    cfg = Config(data_dir=target)
    assert migrate_legacy_data(cfg) is False
    assert not cfg.db_file.exists()
    assert (target / "settings.json").read_text() == '{"update_url": ""}'


def test_never_replaces_an_existing_database(monkeypatch, tmp_path):
    from pcrituals.models import Ritual
    from pcrituals.storage import Store

    trusted_tmp(monkeypatch, tmp_path)
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _plant_legacy(legacy, ritual_name="THEIRS")

    cfg = Config(data_dir=tmp_path / "mine")
    cfg.ensure_dirs()
    store = Store(cfg)
    store.save_ritual(Ritual(name="MINE"))
    use_legacy_source(monkeypatch, legacy)

    assert migrate_legacy_data(cfg) is False
    assert [r.name for r in Store(cfg).list_rituals()] == ["MINE"]


# --------------------------------------------------------------------------
# 4. a genuine upgrade keeps the rituals — and never the account
# --------------------------------------------------------------------------
def test_genuine_upgrade_keeps_rituals(monkeypatch, tmp_path):
    trusted_tmp(monkeypatch, tmp_path)
    legacy = tmp_path / "old_install"
    legacy.mkdir()
    _plant_legacy(legacy, ritual_name="MY RITUAL")
    use_legacy_source(monkeypatch, legacy)

    cfg = Config(data_dir=tmp_path / "new_install")
    assert migrate_legacy_data(cfg) is True

    from pcrituals.storage import Store
    assert [r.name for r in Store(cfg).list_rituals()] == ["MY RITUAL"]
    # Non-credential files still travel.
    assert (cfg.data_dir / "settings.json").exists()


def test_migration_never_adopts_an_account(monkeypatch, tmp_path):
    """Adopting another install's account is never what the user wants: the
    rituals come across, users/sessions/devices/pairing codes do not."""
    trusted_tmp(monkeypatch, tmp_path)
    legacy = tmp_path / "old_install"
    legacy.mkdir()
    src = _plant_legacy(legacy, ritual_name="MY RITUAL")
    assert "users" in _tables(src.db_file)
    use_legacy_source(monkeypatch, legacy)

    cfg = Config(data_dir=tmp_path / "new_install")
    assert migrate_legacy_data(cfg) is True

    tables = _tables(cfg.db_file)
    for table in config_mod.CREDENTIAL_TABLES:
        assert table not in tables, f"{table} was adopted from the legacy install"
    assert "rituals" in tables
    # Nothing to pair or sign in with: no account, no device tokens, and the
    # server secret is not adopted either.
    assert not (cfg.data_dir / "server_secret").exists()

    from pcrituals.auth import AuthManager
    assert AuthManager(cfg).has_users() is False
