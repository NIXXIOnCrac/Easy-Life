"""Off-Windows verification of the Windows packaging + release pipeline.

The assistant that wrote installer/, build_windows.bat, pcrituals.spec and
.github/workflows/release.yml has NO Windows machine: a PyInstaller .exe and an
Inno Setup installer cannot be produced (or run) on Linux. So instead of
pretending, this file verifies the parts that CAN be verified anywhere:

  * what pcrituals.spec bundles and, more importantly, what it refuses to
    exclude - the regression test for the release that died at startup with
    `ModuleNotFoundError: No module named 'locale'`,
  * that the update manifest the release workflow publishes is readable by the
    real in-app updater (pcrituals/update.py) - executed end to end against a
    local HTTP server, checksum included,
  * that the installer never deletes the user's rituals (their data folder is
    outside the install folder, and no delete in the .iss can reach it),
  * that every script BUILD.md tells a human to run actually exists.

What is still NOT verified here (and cannot be): that the .exe starts on a real
Windows box, that the Inno script compiles, and that the installer's shortcuts
and uninstaller behave. BUILD.md says so out loud, and the release workflow
covers the first two on a Windows runner.
"""
from __future__ import annotations

import ast
import http.server
import json
import re
import socketserver
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import make_update_manifest as manifest_mod  # noqa: E402
import verify_bundle as vb  # noqa: E402

SPEC = ROOT / "pcrituals.spec"
ISS = ROOT / "installer" / "pc-rituals.iss"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
BUILD_BAT = ROOT / "build_windows.bat"
SETUP_BAT = ROOT / "setup_windows.bat"
FIREWALL_BAT = ROOT / "scripts" / "add_firewall_rule.bat"
BUILD_MD = ROOT / "BUILD.md"
UPDATE_PY = ROOT / "pcrituals" / "update.py"
CONFIG_PY = ROOT / "pcrituals" / "config.py"

APP_SOURCES = sorted(ROOT.glob("pcrituals/*.py")) + [ROOT / "desktop.py", ROOT / "launcher.py"]

# Stdlib modules the packaged app needs at runtime. Excluding any of these is how
# the "locale" crash shipped: `locale` is imported indirectly by subprocess and
# multiprocessing, so PyInstaller happily dropped it and the .exe died at launch.
NEVER_EXCLUDE = {
    "locale", "subprocess", "multiprocessing", "os", "sys", "io", "time", "json",
    "logging", "importlib", "codecs", "encodings", "typing", "asyncio", "socket",
    "threading", "sqlite3", "ssl", "uuid", "warnings", "traceback", "collections",
    "re", "shutil", "tempfile", "pathlib", "dataclasses",
}

# The only excludes the spec is allowed to have. Anything more has to be argued
# for in review - a smaller bundle is never worth a crash on the user's PC.
ALLOWED_EXCLUDES = {"test", "unittest", "pydoc"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _spec_tree() -> ast.Module:
    return ast.parse(SPEC.read_text(encoding="utf-8"), filename=str(SPEC))


def _assign_to(name: str):
    """The value node of the first top-level `name = ...` assignment in the spec."""
    for node in _spec_tree().body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    raise AssertionError(f"pcrituals.spec has no `{name} = ...` assignment")


def _spec_keyword(kw: str):
    """The AST of `kw=...` in the spec's Analysis(...) call."""
    for node in ast.walk(_spec_tree()):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis":
            for keyword in node.keywords:
                if keyword.arg == kw:
                    return keyword.value
    raise AssertionError(f"pcrituals.spec's Analysis(...) has no `{kw}=` argument")


def _hiddenimports() -> set[str]:
    """Every module name the spec adds to hiddenimports (all three idioms used)."""
    names: set[str] = set()
    for node in _spec_tree().body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "hiddenimports" for t in node.targets
        ):
            names |= {c.value for c in ast.walk(node.value) if isinstance(c, ast.Constant)
                      and isinstance(c.value, str)}
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "hiddenimports":
            names |= {c.value for c in ast.walk(node.value) if isinstance(c, ast.Constant)
                      and isinstance(c.value, str)}
    for node in ast.walk(_spec_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "append" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "hiddenimports":
            names |= {c.value for c in ast.walk(node) if isinstance(c, ast.Constant)
                      and isinstance(c.value, str)}
    return names


def _imported_top_level_modules() -> dict[str, set[str]]:
    """Top-level module name -> the app files that import it."""
    found: dict[str, set[str]] = {}
    for path in APP_SOURCES:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.setdefault(alias.name.split(".")[0], set()).add(path.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import inside the package
                    continue
                if node.module:
                    found.setdefault(node.module.split(".")[0], set()).add(path.name)
    return found


def _dynamic_import_names() -> dict[str, set[str]]:
    """Modules named in a *string*: `__import__("json")`, `import_module("x")`."""
    found: dict[str, set[str]] = {}
    for path in APP_SOURCES:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            is_dyn = (isinstance(func, ast.Name) and func.id == "__import__") or (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            )
            arg = node.args[0]
            if is_dynamic_import_arg := (
                is_dyn and isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ):
                found.setdefault(arg.value.split(".")[0], set()).add(path.name)
    return found


def _iss_text() -> str:
    return ISS.read_text(encoding="utf-8")


def _iss_sections() -> dict[str, list[str]]:
    """Parse the .iss into {section: [lines]} (comment lines dropped)."""
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in _iss_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        match = re.match(r"^\[(.+)\]$", line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return sections


def _section_entry_lines(name: str) -> list[str]:
    """Entries of an Inno section: its lines that actually do something.

    Inno has no blank lines inside sections and `#` lines are preprocessor
    directives, so everything returned here is a real entry of that section.
    """
    return [ln for ln in _iss_sections().get(name, []) if not ln.startswith("#")]


def _fit_default_data_dir() -> str:
    """Where pcrituals/config.py puts the user's data on Windows.

    Read out of the source rather than guessed, so this test fails if the app
    ever moves its data folder somewhere the uninstaller could reach.
    """
    source = CONFIG_PY.read_text(encoding="utf-8")
    win32_branch = source.split('if sys.platform == "win32":', 1)[1].split("return", 2)
    assert "LOCALAPPDATA" in win32_branch[0]
    assert '"Easy Life" / "data"' in source, (
        "config.py no longer builds the data dir as %LOCALAPPDATA%/Easy Life/data - "
        "the installer's data-safety reasoning in installer/pc-rituals.iss and "
        "BUILD.md must be revisited."
    )
    return r"%LOCALAPPDATA%\Easy Life\data"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the temp dir used by the update tests, without log spam."""

    def log_message(self, *args):  # noqa: D102 - silence
        pass


class _LocalServer:
    """A throwaway HTTP server: the update manifest is only allowed http(s) URLs."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        handler = lambda *a, **kw: _QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_LocalServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}"


# ===========================================================================
# pcrituals.spec - what gets bundled
# ===========================================================================
def test_spec_parses_and_keeps_the_real_entry_point():
    """A spec that does not parse builds nothing at all."""
    tree = _spec_tree()  # raises SyntaxError with a useful message if broken
    assert isinstance(tree, ast.Module)

    entry = _assign_to("root")  # sanity: the spec still locates the project root
    assert isinstance(entry, ast.Call)
    scripts_arg = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis":
            scripts_arg = node.args[0]
    assert scripts_arg is not None, "the spec no longer calls Analysis(...)"
    text = ast.unparse(scripts_arg)
    assert "desktop.py" in text, "the packaged entry point must stay desktop.py (native window)"
    assert (ROOT / "desktop.py").exists()
    assert (ROOT / "launcher.py").exists()


def test_spec_excludes_nothing_the_app_imports():
    """Regression: `excludes=["locale", ...]` shipped an app that crashed at launch.

    subprocess/multiprocessing import `locale`, so dropping it from the bundle
    produced `ModuleNotFoundError: No module named 'locale'` on the user's PC,
    before the window ever appeared.
    """
    excludes = {ast.literal_eval(c) for c in _spec_keyword("excludes").elts}
    assert excludes <= ALLOWED_EXCLUDES, (
        f"pcrituals.spec excludes {sorted(excludes - ALLOWED_EXCLUDES)} - new excludes "
        "have to be justified; they are how the 'locale' startup crash shipped."
    )

    imported = _imported_top_level_modules()
    broken = sorted(name for name in excludes if name in imported)
    assert not broken, (
        f"the spec excludes {broken}, which the app imports "
        f"({ {name: sorted(imported[name]) for name in broken} })"
    )

    dangerous = sorted(excludes & NEVER_EXCLUDE)
    assert not dangerous, (
        f"the spec excludes required stdlib module(s) {dangerous}: this is exactly the "
        "'ModuleNotFoundError: No module named ...' crash the release notes warn about."
    )
    assert "locale" not in excludes


def test_spec_bundles_the_whole_web_frontend():
    """The frontend is data, not code: if it is not bundled the window is blank."""
    datas = _assign_to("datas")
    assert isinstance(datas, ast.List) and datas.elts, "datas must list the web folder"
    first = datas.elts[0]
    assert isinstance(first, ast.Tuple) and len(first.elts) == 2
    source_arg, dest_arg = first.elts
    assert isinstance(source_arg, ast.Call) and getattr(source_arg.func, "id", "") == "str", \
        "the web folder source must be resolved through str(web_dir)"
    assert isinstance(dest_arg, ast.Constant) and dest_arg.value == "pcrituals/web", (
        "the frontend must be bundled to pcrituals/web - api.py looks for exactly that path"
    )
    assert "web_dir" in ast.unparse(source_arg)

    # Every file the UI actually loads must be on disk to be bundled at all.
    web = ROOT / "pcrituals" / "web"
    for rel in vb.REQUIRED_WEB_FILES:
        assert (web / rel).exists(), f"the bundled UI is missing {rel}"

    # The spec must refuse to build a UI-less app rather than ship a blank window.
    spec_text = SPEC.read_text(encoding="utf-8")
    assert "SystemExit" in spec_text and "index.html" in spec_text


def test_spec_hiddenimports_cover_uvicorn_pywebview_and_string_imports():
    """PyInstaller cannot see imports made through a string, so they must be named."""
    hidden = _hiddenimports()
    for required in (
        "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on", "qrcode",
        "webview", "clr", "clr_loader", "pythonnet",
    ):
        assert required in hidden, f"pcrituals.spec must hidden-import {required}"

    missing = {
        name: sorted(files) for name, files in _dynamic_import_names().items()
        if name not in hidden
    }
    assert not missing, (
        f"these modules are imported through a string and are not in hiddenimports: "
        f"{missing} - PyInstaller's static analysis cannot follow a string argument"
    )


def test_spec_keeps_the_console_less_build_and_its_stream_guard():
    """Regression: a windowed build has sys.stdout is None; uvicorn then crashed.

    `'NoneType' object has no attribute 'isatty'` came from uvicorn's default log
    config. desktop.py works around it, so the guard must stay.
    """
    spec_text = SPEC.read_text(encoding="utf-8")
    assert "console=False" in spec_text, (
        "the app must not open with a console window (it is a consumer product)"
    )
    assert spec_text.count("console=False") >= 2, "both build targets must be windowed"

    desktop = (ROOT / "desktop.py").read_text(encoding="utf-8")
    assert "_ensure_streams" in desktop, "desktop.py's stream guard was removed"
    assert "log_config=None" in desktop, (
        "desktop.py must keep log_config=None: uvicorn's default config calls "
        "sys.stdout.isatty() and crashes in a console-less build"
    )
    assert 'sys.executable' in desktop  # frozen-mode paths still handled


def test_spec_icon_paths_exist_on_disk():
    """An icon path that does not exist makes PyInstaller fail on Windows only."""
    ico = ROOT / "pcrituals" / "web" / "icons" / "pcrituals.ico"
    assert ico.exists(), "the app icon the spec and the installer both reference is gone"
    spec_text = SPEC.read_text(encoding="utf-8")
    assert 'icons' in spec_text and "pcrituals.ico" in spec_text
    assert r"..\pcrituals\web\icons\pcrituals.ico" in _iss_text()


def test_spec_default_target_stays_backward_compatible():
    """`pyinstaller pcrituals.spec` with no env var must still yield the old output."""
    spec_text = SPEC.read_text(encoding="utf-8")
    assert 'os.environ.get("PCRITUALS_TARGET") or "portable"' in spec_text, (
        "the default target must remain 'portable' so existing instructions still work"
    )
    assert 'name="Easy Life"' in spec_text
    # The portable branch is one-file: it hands a.binaries/a.datas to EXE itself.
    assert spec_text.count("exclude_binaries=True") == 1, (
        "only the installed (folder) target may use exclude_binaries"
    )
    assert "COLLECT(" in spec_text, "the installed target must produce a folder bundle"
    assert 'dist/Easy Life.exe' in spec_text or "portable" in spec_text


# ===========================================================================
# release pipeline: the update manifest contract
# ===========================================================================
def _keys_the_updater_reads() -> set[str]:
    """Every manifest key pcrituals/update.py pulls out of the JSON."""
    tree = ast.parse(UPDATE_PY.read_text(encoding="utf-8"), filename=str(UPDATE_PY))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
    return keys


def test_manifest_field_names_match_the_real_updater():
    """The manifest the workflow publishes must be the one update.py reads.

    A renamed field (say `sha` for `sha256`) would silently disable checksum
    verification instead of failing, so this is asserted against the updater's
    own source rather than a copy of the format.
    """
    read = _keys_the_updater_reads()
    assert {"version", "url", "notes", "sha256"} <= read, (
        f"pcrituals/update.py no longer reads the expected manifest fields: {sorted(read)}"
    )
    assert set(manifest_mod.MANIFEST_FIELDS) == {"version", "url", "notes", "sha256"}


def test_manifest_builder_always_writes_all_four_fields(tmp_path: Path):
    built = manifest_mod.build_manifest("v0.2.0", "https://example.test/x.zip",
                                        "  Fixed   the\nclose-app step  ", "ab" * 32)
    assert list(built) == list(manifest_mod.MANIFEST_FIELDS)
    assert built["version"] == "0.2.0", "a leading v must be stripped"
    assert built["notes"] == "Fixed the close-app step"
    assert built["sha256"] == "ab" * 32

    empty = manifest_mod.build_manifest("0.2.0", "https://example.test/x.zip")
    assert empty["sha256"] is None and empty["notes"] == ""

    for bad in ("", "not-a-url", "ftp://example.test/x.zip"):
        with pytest.raises(ValueError):
            manifest_mod.build_manifest("0.2.0", bad)
    with pytest.raises(ValueError):
        manifest_mod.build_manifest("", "https://example.test/x.zip")
    with pytest.raises(ValueError):
        manifest_mod.build_manifest("0.2.0", "https://example.test/x.zip", sha256="tooshort")

    out = manifest_mod.write_manifest(tmp_path / "update.json", built)
    assert json.loads(out.read_text(encoding="utf-8"))["version"] == "0.2.0"


def test_manifest_cli_hashes_the_archive(tmp_path: Path, capsys):
    """The exact command the release workflow runs must work."""
    archive = tmp_path / "Easy-Life-0.2.0-update.zip"
    archive.write_bytes(b"pretend this is 40 MB of bundle")
    out = tmp_path / "update.json"
    rc = manifest_mod.main([
        "--version", "0.2.0",
        "--url", "https://example.test/Easy-Life-0.2.0-update.zip",
        "--sha256-file", str(archive),
        "--notes", "Changes",
        "--out", str(out),
    ])
    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["sha256"] == manifest_mod.sha256_file(archive)
    assert len(written["sha256"]) == 64

    # Missing archive: fail loudly, do not write a manifest without a checksum.
    assert manifest_mod.main([
        "--version", "0.2.0", "--url", "https://example.test/x.zip",
        "--sha256-file", str(tmp_path / "nope.zip"), "--out", str(out),
    ]) == 2


def test_release_manifest_is_accepted_by_the_real_in_app_updater(tmp_path: Path):
    """End-to-end: manifest -> Update.check() -> download() -> checksum.

    This is the whole in-app update path except for the Windows-only apply(),
    executed here with the real pcrituals/update.py code against a local server.
    """
    from pcrituals.update import UpdateError, Updater

    served = tmp_path / "served"
    served.mkdir()
    archive = served / "Easy-Life-9.9.9-update.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Easy Life.exe", b"exe")
        zf.writestr("_internal/pcrituals/web/index.html", b"<html>Easy Life</html>")

    with _LocalServer(served) as server:
        digest = manifest_mod.sha256_file(archive)
        built = manifest_mod.build_manifest(
            "9.9.9", server.url(archive.name), "Test release", digest)
        manifest_mod.write_manifest(served / "update.json", built)

        updater = Updater(server.url("update.json"), current_version="0.1.0")
        info = updater.check()
        assert info is not None, "the updater could not read the manifest we generate"
        assert info.version == "9.9.9"
        assert info.notes == "Test release"
        assert info.sha256 == digest

        downloaded = updater.download(info)
        assert downloaded.exists(), "the updater failed to download the update archive"
        assert downloaded.stat().st_size == archive.stat().st_size

        # A tampered manifest must be rejected, not applied.
        bad = dict(built, sha256="0" * 64)
        manifest_mod.write_manifest(served / "update.json", bad)
        tampered = Updater(server.url("update.json"), current_version="0.1.0").check()
        assert tampered is not None
        with pytest.raises(UpdateError):
            Updater(server.url("update.json"), current_version="0.1.0").download(tampered)

        # No newer version -> nothing offered (the app must not nag).
        assert Updater(server.url("update.json"), current_version="9.9.9").check() is None
        assert Updater(server.url("update.json"), current_version="10.0.0").check() is None

        # apply() stays Windows-only - documented behaviour, asserted so nobody
        # claims an in-app update happens on the development machine.
        if sys.platform != "win32":
            with pytest.raises(UpdateError):
                Updater(server.url("update.json")).apply(info)


# ===========================================================================
# release workflow
# ===========================================================================
def _workflow_doc() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    # YAML 1.1 parses the bare key `on` as the boolean True.
    if True in doc and "on" not in doc:
        doc["on"] = doc.pop(True)
    return doc


def test_release_workflow_triggers_on_version_tags_and_runs_on_windows():
    doc = _workflow_doc()
    triggers = doc.get("on")
    assert triggers, "the workflow must have an `on:` trigger"
    assert "v*" in triggers.get("push", {}).get("tags", []), "tags like v0.2.0 must trigger it"
    assert doc["permissions"]["contents"] == "write", "publishing a release needs write access"

    jobs = doc["jobs"]
    assert len(jobs) == 1
    job = next(iter(jobs.values()))
    assert job["runs-on"] == "windows-latest", (
        "a Windows .exe can only be built on Windows - there is no cross-compiling"
    )


def test_release_workflow_runs_tests_builds_both_targets_and_smoke_tests():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "python -m pytest tests -q" in text, "the release must run the test suite first"
    assert "PCRITUALS_TARGET: portable" in text
    assert "PCRITUALS_TARGET: installed" in text
    assert text.count("pcrituals.spec") >= 2, "both build targets must be built"
    assert "scripts/verify_bundle.py" in text and "--smoke" in text, (
        "the built app must be checked (and actually started) before it is published"
    )
    assert "installer\\pc-rituals.iss" in text or "installer/pc-rituals.iss" in text


def test_release_workflow_builds_the_installer_with_the_app_version():
    """The .iss refuses to compile without /DAppVersion - the workflow must pass it."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "/DAppVersion=$ver" in text or "/DAppVersion=" in text
    assert "innosetup" in text.lower(), "the runner needs Inno Setup installed"
    assert 'steps.ver.outputs.version' in text


def test_release_workflow_publishes_the_four_release_files_and_the_manifest():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/make_update_manifest.py" in text, (
        "the manifest must be written by the tested script, never by hand"
    )
    assert "--sha256-file" in text, "the published manifest must carry a sha256"
    assert "gh release upload" in text
    for asset in ("Easy-Life-Setup-", "-portable.exe", "-update.zip", "update.json"):
        assert asset in text, f"the release must include {asset}"
    assert "Compress-Archive -Path \"dist\\Easy Life\\*\"" in text, (
        "the update zip must contain the app folder's CONTENTS, because the app "
        "unpacks it straight over its install folder"
    )
    assert "releases/download/$tag/Easy-Life-$ver-update.zip" in text, (
        "the manifest's url must point at the uploaded archive"
    )
    create = [ln for ln in text.splitlines() if "gh release create" in ln]
    assert create, "the workflow must create the release"
    assert all("--draft" not in ln and "--prerelease" not in ln for ln in create), (
        "releases/latest/download/update.json only serves normal, non-draft releases"
    )


# ===========================================================================
# installer (Inno Setup)
# ===========================================================================
def test_installer_has_the_expected_structure():
    doc = _iss_text()
    assert ISS.exists() and len(doc) > 2000
    sections = _iss_sections()
    for required in ("Setup", "Languages", "Tasks", "Files", "Icons", "Registry",
                     "Run", "UninstallRun", "Code"):
        assert required in sections, f"the .iss is missing [{required}]"

    setup = dict(
        (ln.split("=", 1)[0].strip().lower(), ln.split("=", 1)[1].strip())
        for ln in _section_entry_lines("Setup") if "=" in ln
    )
    assert setup["appname"] in ("Easy Life", "{#AppName}")
    assert setup["appversion"] == "{#AppVersion}"
    assert "Easy-Life-Setup-{#AppVersion}" in setup["outputbasefilename"]
    assert setup["outputdir"].replace("/", "\\") == r"..\dist"
    assert setup["uninstalldisplayicon"].endswith("{#AppExeName}")
    assert "closeapplications=yes" in _iss_text().lower().replace(" ", "")

    # A per-user install so the in-app updater (which writes into the install
    # folder) never needs an administrator prompt, and so uninstalling does not
    # either. Program Files would break both.
    assert setup["defaultdirname"] == r"{localappdata}\Programs\{#AppName}"
    assert setup["privilegesrequired"] == "lowest"
    assert "{autopf}" not in doc and "{pf}" not in doc
    header = doc.split("[Setup]", 1)[0]
    assert "update.py" in header, (
        "the script must keep the written justification for the per-user install"
    )


def test_installer_requires_a_version_and_an_existing_bundle():
    doc = _iss_text()
    assert "#ifndef AppVersion" in doc and "#error AppVersion is not defined" in doc, (
        "a hard-coded fallback version would silently ship the wrong version number"
    )
    assert "#if !FileExists(" in doc and r"..\dist\Easy Life\Easy Life.exe" in doc, (
        "compiling without the built app folder must fail with a plain-English message"
    )


def test_installer_supports_silent_installs():
    doc = _iss_text().lower()
    assert "/silent" in doc, "the wizard (and scripted deployments) need /SILENT"
    # SuppressibleMsgBox is skipped in silent mode; a plain MsgBox would hang a
    # silent install waiting for someone to click a button.
    code = _iss_sections().get("Code", [])
    assert any("SuppressibleMsgBox" in line for line in code)
    assert not [ln for ln in code if re.search(r"\bMsgBox\(", ln)], (
        "use SuppressibleMsgBox in [Code] so /SILENT cannot block"
    )


def test_installer_creates_start_menu_desktop_shortcut_and_optional_autostart():
    icons = _section_entry_lines("Icons")
    start_menu = [ln for ln in icons if ln.startswith('Name: "{autoprograms}')]
    desktop = [ln for ln in icons if ln.startswith('Name: "{autodesktop}')]
    assert start_menu and "Tasks:" not in start_menu[0], (
        "the Start Menu entry must always be created"
    )
    assert desktop and "Tasks: desktopicon" in desktop[0], (
        "the desktop shortcut must be the optional one (user's requirement)"
    )
    assert "Filename: \"{app}\\{#AppExeName}\"" in desktop[0]

    tasks = _section_entry_lines("Tasks")
    assert any(ln.startswith('Name: "desktopicon"') for ln in tasks)
    assert any(ln.startswith('Name: "startupicon"') and "unchecked" in ln for ln in tasks), (
        "'run at login' must be offered but off by default"
    )

    registry = _section_entry_lines("Registry")
    run_key = [ln for ln in registry if "CurrentVersion\\Run" in ln]
    assert run_key and "Tasks: startupicon" in run_key[0] and "uninsdeletevalue" in run_key[0]

    files = _section_entry_lines("Files")
    assert any(ln.startswith(r'Source: "..\dist\Easy Life\*"') for ln in files), (
        "the installer must ship the app folder built by PCRITUALS_TARGET=installed"
    )
    assert any(ln.startswith(r'Source: "..\scripts\add_firewall_rule.bat"') for ln in files)
    assert FIREWALL_BAT.exists()


def test_installer_never_deletes_the_users_data():
    """The rituals are the product: uninstalling must not take them away.

    Provable statically, which is the best that is possible without a Windows PC:
    the data folder is not inside {app}, and no delete in the script mentions it.
    """
    data_dir = _fit_default_data_dir()
    assert data_dir == r"%LOCALAPPDATA%\Easy Life\data"

    sections = _iss_sections()
    setup = dict(
        (ln.split("=", 1)[0].strip().lower(), ln.split("=", 1)[1].strip())
        for ln in _section_entry_lines("Setup") if "=" in ln
    )
    install_dir = setup["defaultdirname"].replace("{localappdata}", r"%LOCALAPPDATA%") \
                                        .replace("{#AppName}", "Easy Life").replace("\\", "/")
    data_norm = data_dir.replace("\\", "/")
    assert not data_norm.startswith(install_dir + "/"), (
        f"the data folder {data_norm} is inside the program folder {install_dir}: "
        "uninstalling would delete the user's rituals"
    )

    # Nothing may be deleted outside the program folder.
    delete_entries = _section_entry_lines("UninstallDelete")
    assert delete_entries == [], (
        f"[UninstallDelete] must stay empty, found: {delete_entries}"
    )
    install_delete = _section_entry_lines("InstallDelete")
    for ln in install_delete:
        assert ln.startswith('Type: filesandordirs; Name: "{app}\\'), (
            f"an [InstallDelete] entry reaches outside {{app}}: {ln}"
        )

    # The cleanup in [Code] must only ever target paths under {app}.
    code = "\n".join(sections.get("Code", []))
    for match in re.finditer(r"(DelTree|DeleteFile|RemoveDir)\(([^\n]*)", code, re.IGNORECASE):
        target = match.group(2)
        assert "{app}" in target, f"a delete in [Code] does not stay inside {{app}}: {match.group(0)}"
        assert "Easy Life\\data" not in target and "#DataDir" not in target.replace(
            "ExpandConstant", ""
        ), f"a delete in [Code] could reach the data folder: {match.group(0)}"

    # And it tells the user, in the uninstaller, where their data still is.
    assert "Your rituals and settings were KEPT" in code
    assert "Easy Life\\data" in code

    # An upgrade must not touch data either - [InstallDelete] only clears the
    # program's own payload folder.
    assert any(r"_internal" in ln for ln in install_delete)
    assert not any("data" in ln.lower() for ln in install_delete + delete_entries)


def test_installer_offers_phone_control_without_blocking_the_install():
    doc = _iss_text()
    tasks = _section_entry_lines("Tasks")
    firewall_task = [ln for ln in tasks if ln.startswith('Name: "firewallrule"')]
    assert firewall_task and "unchecked" in firewall_task[0], (
        "the firewall rule must be optional and off by default"
    )
    runs = _section_entry_lines("Run")
    assert any("add_firewall_rule.bat" in ln and "Tasks: firewallrule" in ln for ln in runs)
    assert any('Flags: nowait postinstall skipifsilent' in ln for ln in runs), (
        "'Run Easy Life now' must be offered non-blockingly, and not in silent installs"
    )
    assert "8765" in FIREWALL_BAT.read_text(encoding="utf-8"), (
        "the firewall helper must open the port the app actually binds"
    )
    # The uninstaller tries to take the firewall rule away again.
    assert any("Easy Life" in ln for ln in _section_entry_lines("UninstallRun"))


# ===========================================================================
# batch scripts
# ===========================================================================
@pytest.mark.parametrize("path", [BUILD_BAT, SETUP_BAT, FIREWALL_BAT])
def test_batch_scripts_are_windows_safe(path: Path):
    """LF-only .bat files and unquoted %~dp0 are classic "it worked for me" bugs."""
    raw = path.read_bytes()
    assert raw.startswith(b"@echo off"), f"{path.name} must start with @echo off"
    assert b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n"), (
        f"{path.name} must use Windows CRLF line endings"
    )
    text = raw.decode("utf-8", "replace")
    assert "setlocal" in text and "endlocal" in text
    assert 'cd /d "%~dp0"' in text, f"{path.name} must cd to its own folder, quoted"
    assert "cd /d %~dp0" not in text, f"{path.name} would break in a folder with spaces"
    # Something must hold the window open long enough to be read.
    assert "pause" in text.lower() or "timeout" in text.lower()
    # Every label that is jumped to must exist.
    labels = {m.group(1).lower() for m in re.finditer(r"^:(\w+)", text, re.MULTILINE)}
    for target in re.findall(r"goto\s+:?(\w+)", text, re.IGNORECASE):
        assert target.lower() in labels, f"{path.name} jumps to :{target}, which does not exist"


def test_build_batch_produces_both_the_portable_app_and_the_installer():
    text = BUILD_BAT.read_text(encoding="utf-8")
    assert 'set "PCRITUALS_TARGET=portable"' in text
    assert 'set "PCRITUALS_TARGET=installed"' in text
    assert "dist\\Easy Life.exe" in text
    assert "scripts\\verify_bundle.py" in text, "the build must check what it produced"
    assert "/DAppVersion=" in text, "the installer needs the app version passed in"
    assert "Inno Setup" in text
    # Missing tools must be explained, not crash the window.
    for hint in ("python.org/downloads", "Add python.exe to PATH", "install Inno Setup",
                 "winget install"):
        assert hint in text, f"build_windows.bat must tell the user about: {hint}"
    assert "pause" in text.lower()


def test_setup_batch_is_a_real_wizard_for_a_non_developer():
    text = SETUP_BAT.read_text(encoding="utf-8")
    assert "python.org/downloads" in text and "Add python.exe to PATH" in text
    assert "-m venv" in text and "-r \"!INSTALL_DIR!\\requirements.txt\"" in text
    assert "verify_bundle.py --smoke-source" in text, (
        "the wizard must prove the app starts before making shortcuts"
    )
    assert "CreateShortcut" in text and "Desktop" in text and "Programs" in text
    assert "add_firewall_rule.bat" in text, "phone control is offered by the wizard"
    assert "/SILENT" in text, "a ready-made installer is used silently when present"
    assert "build_windows.bat" in text, "the wizard offers to build the real installer"
    assert "Easy Life install.txt" in text, (
        "uninstall_windows.bat reads the install path from that file"
    )
    # Data must be described as living outside the program folder.
    assert r"%LOCALAPPDATA%\Easy Life\data" in text
    assert r"%LOCALAPPDATA%\Programs\Easy Life" in text


# ===========================================================================
# documentation promises
# ===========================================================================
def test_every_script_build_md_tells_the_user_to_run_exists():
    doc = BUILD_MD.read_text(encoding="utf-8")
    referenced = set()
    for token in re.findall(r"`([^`\n]+)`", doc):
        cleaned = token.strip().strip("\\").replace("\\", "/")
        stem = Path(cleaned).name.rsplit(".", 1)[0]
        if not stem:
            # A bare extension like `.bat` is prose, not a file to run.
            continue
        if cleaned.endswith(".bat") or cleaned.endswith(".iss"):
            referenced.add(cleaned)
        elif cleaned.startswith("scripts/") and cleaned.endswith(".py"):
            referenced.add(cleaned)
        elif cleaned.startswith(".github/"):
            referenced.add(cleaned)
    assert referenced, "BUILD.md no longer names any script"
    for rel in sorted(referenced):
        candidates = [ROOT / rel, ROOT / Path(rel).name]
        assert any(c.exists() for c in candidates), (
            f"BUILD.md tells the user to run {rel}, which does not exist"
        )


def test_build_md_is_honest_about_what_could_not_be_verified():
    doc = BUILD_MD.read_text(encoding="utf-8")
    assert "What I could not verify" in doc, (
        "BUILD.md must keep an explicit list of what needs a real Windows machine"
    )
    assert "cannot run on Linux" in doc or "no Windows machine" in doc
    assert r"%LOCALAPPDATA%\Easy Life\data" in doc, "document where the rituals live"
    assert "Windows" in doc and ".exe" in doc


# ===========================================================================
# the bundle checker itself (executable here)
# ===========================================================================
def _fake_bundle(root: Path, *, with_web: bool = True, missing: str | None = None) -> Path:
    bundle = root / "Easy Life"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / vb.APP_EXE).write_bytes(b"x" * 1024)
    if with_web:
        web = bundle / "_internal" / "pcrituals" / "web"
        web.mkdir(parents=True)
        for rel in vb.REQUIRED_WEB_FILES:
            if rel == missing:
                continue
            (web / rel).parent.mkdir(parents=True, exist_ok=True)
            (web / rel).write_bytes(b"data")
    return bundle


def test_verify_bundle_accepts_a_complete_bundle(tmp_path: Path):
    bundle = _fake_bundle(tmp_path)
    problems, notes, web = vb.check_bundle(bundle)
    assert problems == [], problems
    assert web is not None and vb.find_web_dir(bundle) == web


def test_verify_bundle_catches_a_missing_ui_file(tmp_path: Path):
    """Regression: a missing web folder shipped an app with a blank window."""
    problems, _, _ = vb.check_bundle(_fake_bundle(tmp_path, missing="manifest.json"))
    assert any("manifest.json" in p for p in problems), problems

    problems, _, _ = vb.check_bundle(_fake_bundle(tmp_path / "b", with_web=False))
    assert any("BLANK window" in p for p in problems), (
        "a bundle without the frontend must be rejected with an explanation"
    )

    (tmp_path / "tiny.exe").write_bytes(b"not really an app")
    problems, _, _ = vb.check_bundle(tmp_path / "tiny.exe")
    assert any("too small" in p for p in problems), problems


def test_verify_bundle_reports_a_missing_bundle(tmp_path: Path):
    problems, _, _ = vb.check_bundle(tmp_path / "nothing-here")
    assert problems and "nothing was built" in problems[0]


_SERVING_SHIM = """\
import os, sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
port = int(os.environ.get("PCRITUALS_PORT", "0"))
os.chdir(sys.argv[1])
HTTPServer(("127.0.0.1", port), SimpleHTTPRequestHandler).serve_forever()
"""

_EXITING_SHIM = "raise SystemExit(1)\n"
_IDLE_SHIM = "import time\nwhile True: time.sleep(1)\n"


def _shim(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def test_smoke_test_passes_for_an_app_that_serves_its_ui(tmp_path: Path):
    """The smoke check is what catches a packaged app that does not boot."""
    served = tmp_path / "ui"
    (served / "icons").mkdir(parents=True)
    (served / "index.html").write_text("<title>Easy Life</title>", encoding="utf-8")
    for rel in ("sw.js", "styles.css", "icons/icon-192.png"):
        (served / rel).write_bytes(b"asset")
    shim = _shim(tmp_path, "serving.py", _SERVING_SHIM)

    problems = vb.smoke_test([sys.executable, str(shim), str(served)], tmp_path,
                             timeout=20, log=lambda *a: None)
    assert problems == [], problems


def test_smoke_test_catches_a_crashing_app(tmp_path: Path):
    """Regression: the 'locale' release died instantly and nobody noticed."""
    shim = _shim(tmp_path, "crash.py", _EXITING_SHIM)
    problems = vb.smoke_test([sys.executable, str(shim)], tmp_path, timeout=20,
                             log=lambda *a: None)
    assert any("exited immediately" in p for p in problems), problems


def test_smoke_test_times_out_on_an_app_that_never_answers(tmp_path: Path):
    shim = _shim(tmp_path, "idle.py", _IDLE_SHIM)
    problems = vb.smoke_test([sys.executable, str(shim)], tmp_path, timeout=2,
                             log=lambda *a: None)
    assert any("did not answer" in p for p in problems), problems


def test_verify_bundle_cli_works_from_the_command_line(tmp_path: Path):
    """BUILD.md and the workflow call it as a program; that must work."""
    bundle = _fake_bundle(tmp_path)
    rc = subprocess.call([sys.executable, str(ROOT / "scripts" / "verify_bundle.py"),
                          str(bundle)])
    assert rc == 0
    rc = subprocess.call([sys.executable, str(ROOT / "scripts" / "verify_bundle.py"),
                          str(tmp_path / "not-there")])
    assert rc == 1
