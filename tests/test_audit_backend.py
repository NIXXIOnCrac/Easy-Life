"""Regression tests for the backend-audit fixes.

A prior audit pass found and fixed several real bugs but was interrupted before
writing its tests. These cover each fix so the behaviour cannot silently regress.

Bugs covered:
  1. A pairing code typed in lower case *validated* but was never *consumed*,
     because redemption deleted the raw user input while validation upper-cased
     it. That defeated single-use enforcement — the code stayed redeemable.
  2. The per-install server secret was generated lazily without a lock, so two
     threads could each write a different secret; tokens derived from the first
     were then invalid.
  3. A delay action with a non-numeric `seconds` raised a raw ValueError, which
     surfaced to the user as a Python error rather than a readable failure.
  4. Integration detection only searched Program Files, so per-user installs
     (Discord/Spotify/Store apps) were reported as "Not detected".
  5. The updater interpolated a manifest-supplied version string straight into a
     file path, so a hostile manifest could escape the temp directory.
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from pydantic import ValidationError

from pcrituals.actions import _seconds
from pcrituals.config import load_config
from pcrituals.models import Action
from pcrituals.platform import PlatformError


def _config(tmp_path, monkeypatch):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path))
    return load_config()


# ---- 1. single-use pairing code -------------------------------------------
def test_pairing_code_is_consumed_even_if_typed_lowercase(tmp_path, monkeypatch):
    from pcrituals.security import Security
    sec = Security(_config(tmp_path, monkeypatch))
    pc = sec.create_pairing_code()
    lowered = pc.code.lower()

    # It still validates (matching is case-insensitive)...
    assert sec.validate_pairing_code(lowered) is not None
    # ...and must therefore also be *consumed* by redemption.
    redeemed = sec.redeem_pairing_code(lowered)
    assert redeemed is not None, "lower-case code validated but was not consumed"
    # Second use must fail: the code is single-use.
    assert sec.redeem_pairing_code(pc.code) is None
    assert sec.validate_pairing_code(pc.code) is None


def test_pairing_code_case_insensitive_round_trip(tmp_path, monkeypatch):
    from pcrituals.security import Security
    sec = Security(_config(tmp_path, monkeypatch))
    pc = sec.create_pairing_code()
    assert sec.validate_pairing_code(f"  {pc.code.lower()}  ") is not None
    assert sec.validate_pairing_code("") is None
    assert sec.validate_pairing_code("NOPE99") is None


# ---- 2. server secret ------------------------------------------------------
def test_server_secret_is_stable_across_threads(tmp_path, monkeypatch):
    from pcrituals.security import Security
    cfg = _config(tmp_path, monkeypatch)
    # Point several independent Security objects (as several app instances
    # would) at the same data dir and read the secret concurrently.
    secs = [Security(cfg) for _ in range(8)]
    seen: list[str] = []
    lock = threading.Lock()

    def read(sec):
        val = sec.server_secret
        with lock:
            seen.append(val)

    threads = [threading.Thread(target=read, args=(s,)) for s in secs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(seen)) == 1, f"server secret was not stable: {set(seen)}"
    assert len(seen[0]) >= 32


# ---- 3. delay validation ---------------------------------------------------
def test_delay_seconds_rejects_junk_with_a_clear_error():
    with pytest.raises(PlatformError) as err:
        _seconds(Action(type="delay", params={"seconds": "soon"}))
    assert "number" in str(err.value).lower()


def test_delay_seconds_accepts_numbers_and_defaults():
    assert _seconds(Action(type="delay", params={"seconds": 2})) == 2.0
    assert _seconds(Action(type="delay", params={"seconds": "3.5"})) == 3.5
    assert _seconds(Action(type="delay", params={"seconds": ""})) == 1.0
    assert _seconds(Action(type="delay", params={}, target="4")) == 4.0


# ---- 4. integration detection ---------------------------------------------
def test_integration_detection_finds_per_user_installs(tmp_path):
    from pcrituals.integrations import WindowsDetector

    local = tmp_path / "AppData" / "Local"
    (local / "Discord").mkdir(parents=True)
    env = {"LOCALAPPDATA": str(local), "ProgramFiles": str(tmp_path / "nope")}
    det = WindowsDetector(env=env)
    # Discord is a classic per-user (Squirrel) install — Program Files only
    # searching used to miss it entirely.
    found = det._find_anywhere("Discord")
    assert found is not None, "per-user install was not detected"
    assert "Discord" in str(found)


def test_integration_detection_handles_missing_env():
    from pcrituals.integrations import WindowsDetector
    det = WindowsDetector(env={})
    assert det._program_files_dirs()          # falls back to C:\ defaults
    assert det._find_anywhere("definitely-not-installed") is None


# ---- 5. updater path safety ------------------------------------------------
def test_update_version_cannot_escape_the_temp_directory():
    from pcrituals.update import _safe_component
    for hostile in ["../../evil", "..\\..\\evil", "a/b", "a\\b", "", "...", "  "]:
        out = _safe_component(hostile)
        assert "/" not in out and "\\" not in out
        assert ".." not in out
        assert out != ""
    assert _safe_component("1.2.3") == "1.2.3"


def test_updater_rejects_unsafe_archive_entries(tmp_path, monkeypatch):
    """A zip containing an absolute or parent path must be refused."""
    import zipfile
    from pcrituals.update import UpdateError, Updater

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../pwned.txt", "x")

    up = Updater("", install_dir=tmp_path / "install")
    from pcrituals.update import UpdateInfo
    info = UpdateInfo(version="9.9.9", url="http://x/y.zip", downloaded_to=archive)
    monkeypatch.setattr("sys.platform", "win32")
    with pytest.raises(UpdateError) as err:
        up.apply(info)
    assert "unsafe" in str(err.value).lower()
