"""The in-app updater must verify the update archive's checksum.

An installer-pipeline audit found that `App._update_check_result()` dropped the
`sha256` field from the manifest, and `update_download()` then built an
UpdateInfo with sha256=None. `Updater.download()` only verifies when a hash is
present, so the UI-driven update path downloaded UNVERIFIED even though the
manifest carried a checksum — which is the entire reason for shipping one. A
tampered manifest or a hostile mirror would have been applied silently.

These tests serve a real manifest + archive over local HTTP and drive the real
App code path.
"""
import hashlib
import http.server
import json
import socket
import sys
import threading
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pcrituals.config import load_config
from pcrituals.update import UpdateError


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_archive(path: Path, payload: str = "hello") -> str:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pcrituals/VERSION", payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def update_server(tmp_path):
    """Serve a manifest + archive over real HTTP; yields a mutable holder."""
    port = _free_port()
    served = tmp_path / "served"
    served.mkdir()
    archive = served / "update.zip"
    good_hash = _make_archive(archive)
    state = {"version": "9.9.9", "sha256": good_hash}

    def write_manifest():
        manifest = {
            "version": state["version"],
            "url": f"http://127.0.0.1:{port}/update.zip",
            "notes": "test build",
            "sha256": state["sha256"],
        }
        (served / "update.json").write_text(json.dumps(manifest))

    write_manifest()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(served), **kw)

        def log_message(self, *a):  # keep the test output clean
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield {"url": f"http://127.0.0.1:{port}/update.json", "state": state,
               "good_hash": good_hash, "rewrite": write_manifest}
    finally:
        httpd.shutdown()


def _app(tmp_path, monkeypatch, manifest_url):
    monkeypatch.setenv("PCRITUALS_DATA_DIR", str(tmp_path / "data"))
    cfg = load_config()
    cfg.update_url = manifest_url
    from pcrituals.app import App
    return App(cfg)


def test_update_check_keeps_the_checksum(tmp_path, monkeypatch, update_server):
    """The field must survive into what the UI path uses."""
    app = _app(tmp_path, monkeypatch, update_server["url"])
    info = app._update_check_result()
    assert info is not None, "no update reported"
    assert info.get("sha256") == update_server["good_hash"], \
        f"checksum was dropped: {info}"


def test_ui_update_download_accepts_a_matching_checksum(tmp_path, monkeypatch, update_server):
    app = _app(tmp_path, monkeypatch, update_server["url"])
    result = app.update_download()
    assert result["downloaded"] is True, result


def test_ui_update_download_REJECTS_a_tampered_archive(tmp_path, monkeypatch, update_server):
    """The security property: a mismatching checksum must abort the download."""
    app = _app(tmp_path, monkeypatch, update_server["url"])
    # A manifest that advertises the wrong hash — a tampered manifest, a
    # corrupted upload, or a hostile mirror.
    update_server["state"]["sha256"] = "0" * 64
    update_server["rewrite"]()
    # The app memoizes the check, so clear it to re-read the manifest.
    if hasattr(app, "_update_cache"):
        del app._update_cache

    with pytest.raises((UpdateError, Exception)) as err:
        app.update_download()
    assert "checksum" in str(err.value).lower() or "hash" in str(err.value).lower(), \
        f"expected a checksum failure, got: {err.value}"


def test_a_manifest_without_a_checksum_still_works(tmp_path, monkeypatch, update_server):
    """Optional on purpose: an unsigned manifest must not break the feature."""
    app = _app(tmp_path, monkeypatch, update_server["url"])
    update_server["state"]["sha256"] = None
    update_server["rewrite"]()
    if hasattr(app, "_update_cache"):
        del app._update_cache
    result = app.update_download()
    assert result["downloaded"] is True
    assert app._update_check_result().get("sha256") is None
