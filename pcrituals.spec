# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec - builds the Easy Life desktop app (native window).

Two build targets, chosen with the PCRITUALS_TARGET environment variable:

    PCRITUALS_TARGET=portable   (default - same output as before this change)
        dist/Easy Life.exe         one single file, portable, no Python needed

    PCRITUALS_TARGET=installed
        dist/Easy Life/            a folder bundle, shipped by the installer
        dist/Easy Life/Easy Life.exe

Why two targets:

  * `portable` is the download-it-once single file. A one-file exe unpacks its
    whole payload into %TEMP% on EVERY launch, so it starts slowly (and some
    antivirus products scan the unpack each time).
  * `installed` is what installer/pc-rituals.iss ships. It starts immediately,
    and the in-app updater (pcrituals/update.py) can merge a new version over
    the folder without a re-download of a 100 MB file to %TEMP% each time.

Build (ON WINDOWS ONLY - PyInstaller cannot cross-compile a .exe from Linux):

    pyinstaller --noconfirm --clean pcrituals.spec

build_windows.bat runs both targets and then the installer, in that order.

DO NOT BREAK THESE (each one is a bug that already shipped once):

  * `datas` must bundle the WHOLE pcrituals/web folder. Without it the window
    opens blank because there is no UI to serve.
  * `excludes` must stay tiny. Excluding stdlib modules (the old list tried to
    drop `locale`) makes the packaged app die at launch with
    `ModuleNotFoundError: No module named 'locale'` - subprocess and
    multiprocessing import it.
  * `console=False` must stay False (a console window would flash up for the
    user), and the app's own stream guard in desktop.py must stay because a
    windowed build has sys.stdout == None, which crashes uvicorn's logging
    (`'NoneType' object has no attribute 'isatty'`).
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
web_dir = root / "pcrituals" / "web"
ico = str(web_dir / "icons" / "pcrituals.ico")

# Fail loudly at build time instead of shipping a window with no UI / no icon.
if not (web_dir / "index.html").exists():
    raise SystemExit(f"[spec] the web frontend is missing: {web_dir}")
if not Path(ico).exists():
    raise SystemExit(f"[spec] the app icon is missing: {ico}")

# --------------------------------------------------------------------------
# Build target
# --------------------------------------------------------------------------
TARGET = (os.environ.get("PCRITUALS_TARGET") or "portable").strip().lower()
if TARGET not in ("portable", "installed"):
    raise SystemExit(
        "[spec] PCRITUALS_TARGET must be 'portable' or 'installed', got: "
        f"{TARGET!r}"
    )

# The web frontend is static data that MUST be bundled next to the app.
# If this is missing the app opens a blank window.
datas = [(str(web_dir), "pcrituals/web")]

hiddenimports = [
    # Collected again below via collect_all("webview"); named here too so the
    # requirement is visible (and testable) in the spec itself.
    "webview",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "qrcode",
    # Only imported inside pcrituals/cert.py's functions (and absent on some
    # source installs), so it is named here rather than left to analysis.
    "cryptography",
]

# Modules imported through a *string* in the source, e.g. `__import__("uuid")`
# in app.py/backup.py. PyInstaller's static analysis cannot follow a string
# argument, so they have to be named here or the app breaks the first time the
# user clones a ritual or reads a backup manifest.
# (tests/test_packaging.py fails the build if a new string-import appears in the
# app without being listed here.)
hiddenimports += ["uuid", "json", "threading"]

binaries = []

# --- pywebview (native window / WebView2) ---
try:
    wv_datas, wv_binaries, wv_hidden = collect_all("webview")
    datas += wv_datas
    binaries += wv_binaries
    hiddenimports += wv_hidden
except Exception as e:  # noqa: BLE001
    print(f"[spec] pywebview not collected: {e}")

# Every application module, so a future lazily-imported module (importlib) can
# never be left out. Best-effort: a missing submodule must not fail the build.
try:
    from PyInstaller.utils.hooks import collect_submodules
    hiddenimports += collect_submodules("pcrituals")
except Exception as e:  # noqa: BLE001
    print(f"[spec] pcrituals submodules not collected: {e}")

# pythonnet/clr powers the Edge WebView2 backend on Windows. Listed explicitly
# (not via a loop) so tests/test_packaging.py can assert they are all present.
hiddenimports += ["clr", "clr_loader", "pythonnet"]

a = Analysis(
    [str(root / "desktop.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # NOTE: never exclude stdlib modules like 'locale' - subprocess/
    # multiprocessing import it and the packaged app will crash on launch.
    excludes=["test", "unittest", "pydoc"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if TARGET == "installed":
    # Folder bundle: exe + _internal\ next to it. This is what the installer
    # ships, and what the in-app updater replaces in place.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="Easy Life",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,          # no console window -> a real app. True to debug.
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ico,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="Easy Life",
    )
else:
    # One single portable file (the default, unchanged from earlier releases).
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="Easy Life",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,          # no console window -> a real app. Set True to debug.
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ico,
    )

print(f"[spec] target={TARGET} entry={root / 'desktop.py'} web={web_dir}")
print("[spec] excludes=test,unittest,pydoc (deliberately tiny - see the header)")
