"""Verify that a built Easy Life bundle really contains what the app needs.

Runs after PyInstaller - on Windows from build_windows.bat, in CI from
.github/workflows/release.yml - and on any OS against a bundle folder you point
it at (the checks are plain file inspections plus an optional HTTP smoke test of
the real .exe).

WHY this exists: the two packaging bugs that already shipped were

  1. the whole `pcrituals/web` frontend was not bundled -> the app opened a
     BLANK window with no error message anywhere, and
  2. an excluded stdlib module (`locale`) crashed the app at launch with
     ModuleNotFoundError.

(1) is caught here without running anything, for both build targets. (2) is what
`--smoke` is for: it starts the built exe, waits for its local server and asks
for the UI files over HTTP - if the bundle is broken, that fails loudly here
instead of on the user's desktop.

Usage:
    python scripts/verify_bundle.py "dist/Easy Life"                # folder build
    python scripts/verify_bundle.py "dist/Easy Life.exe"            # portable file
    python scripts/verify_bundle.py "dist/Easy Life" --smoke
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Sequence

# Everything the frontend needs at runtime. If one of these is missing from the
# bundle the window is blank, the PWA will not install, or the icon is default.
REQUIRED_WEB_FILES = (
    "index.html",
    "app.js",
    "styles.css",
    "tokens.css",
    "liquid-glass.css",
    "manifest.json",
    "sw.js",
    "icons/pcrituals.ico",
    "icons/icon-192.png",
    "icons/icon-512.png",
    "icons/favicon.png",
)

# The exe name the spec/installer/shortcuts all agree on.
APP_EXE = "Easy Life.exe"

# A one-file bundle is ~40-120 MB; anything tiny means the build went wrong.
MIN_PORTABLE_BYTES = 5 * 1024 * 1024

# Pages the running app must serve without a login (they are the static UI).
SMOKE_PATHS = ("/", "/sw.js", "/styles.css", "/icons/icon-192.png")


def find_web_dir(bundle_root: Path) -> Optional[Path]:
    """Locate the bundled pcrituals/web folder.

    PyInstaller puts collected data in `_internal/` for a one-folder build and
    in the extracted _MEIPASS directory for a one-file build, so the folder is
    searched for rather than assumed.
    """
    for candidate in bundle_root.rglob("index.html"):
        parent = candidate.parent
        if parent.name == "web" and parent.parent.name == "pcrituals":
            return parent
    return None


def check_bundle(bundle: Path) -> tuple[list[str], list[str], Optional[Path]]:
    """Inspect a bundle. Returns (problems, notes, web_dir).

    A problem means the app is broken for the user; a note is something worth
    printing that does not fail the build.
    """
    problems: list[str] = []
    notes: list[str] = []

    if not bundle.exists():
        return [f"nothing was built at {bundle}"], notes, None

    # --- the portable single-file build -----------------------------------
    if bundle.is_file():
        size = bundle.stat().st_size
        notes.append(f"portable file: {bundle} ({size / 1_048_576:.1f} MB)")
        if bundle.suffix.lower() != ".exe":
            problems.append(f"{bundle.name} is not a .exe")
        if size < MIN_PORTABLE_BYTES:
            problems.append(
                f"{bundle.name} is only {size / 1_048_576:.2f} MB - far too small "
                "for a PyInstaller bundle, so the build did not finish properly."
            )
        notes.append(
            "the contents of a one-file .exe cannot be inspected without running "
            "it - use --smoke to prove it boots and serves the UI."
        )
        return problems, notes, None

    # --- the folder build shipped by the installer ------------------------
    exe = bundle / APP_EXE
    if not exe.exists():
        problems.append(
            f"{exe} is missing - the installer would install nothing to launch."
        )
    web = find_web_dir(bundle)
    if web is None:
        problems.append(
            "the web frontend (pcrituals/web/index.html) is not bundled: the app "
            "would open a BLANK window. Check the datas= line in pcrituals.spec."
        )
    else:
        notes.append(f"web frontend: {web.relative_to(bundle)}")
        for rel in REQUIRED_WEB_FILES:
            if not (web / rel).exists():
                problems.append(f"bundled web frontend is missing {rel}")
    if not (bundle / "_internal").is_dir():
        notes.append(
            "_internal/ not found - that is normal for a one-file build, but a "
            "one-folder build has it."
        )
    return problems, notes, web


def _free_port() -> int:
    """Ask Windows/macOS/Linux for an unused port instead of fighting over 8765."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_get(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.read(4096)


def smoke_test(command: list[str], workdir: Path, timeout: float = 60.0,
               cwd: Optional[Path] = None, log=print) -> list[str]:
    """Start the app (built or from source) and check it serves its UI over HTTP.

    This is the only check that proves the app actually runs: it would have
    caught both shipped crashes (missing `locale`, the None-stdout logging
    crash) before they reached the user.

    `command` is what to launch - the built .exe, or [python, desktop.py] for a
    source checkout (setup_windows.bat uses that).
    """
    problems: list[str] = []
    port = _free_port()
    data_dir = workdir / "smoke-data"
    env = dict(os.environ)
    env.update({
        "PCRITUALS_PORT": str(port),
        "PCRITUALS_HOST": "127.0.0.1",
        "PCRITUALS_LOOPBACK": "1",
        # Keep the smoke run out of the real user's data folder.
        "PCRITUALS_DATA_DIR": str(data_dir),
    })
    label = Path(command[0]).name
    log(f"[smoke] starting {label} on port {port} (data: {data_dir})")

    popen_kwargs: dict = {"env": env, "cwd": str(cwd or Path(command[0]).parent)}
    if sys.platform == "win32":
        # No flashing console window, and its own process group so the whole
        # tree (including WebView2 helpers) can be killed later.
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    proc = subprocess.Popen([str(c) for c in command], **popen_kwargs)

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    ready = False
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                problems.append(
                    f"the app exited immediately (code {proc.returncode}) - it "
                    "crashed on startup, which is what a missing bundled module "
                    "looks like."
                )
                return problems
            try:
                status, body = _http_get(base + "/", timeout=2.0)
                if status == 200 and b"Easy Life" in body:
                    ready = True
                    break
            except Exception:
                time.sleep(0.5)
        if not ready:
            problems.append(
                f"the app did not answer on {base}/ within {timeout:.0f}s"
            )
            return problems

        log("[smoke] server is up; checking the bundled UI files")
        for path in SMOKE_PATHS:
            try:
                status, body = _http_get(base + path)
            except urllib.error.HTTPError as exc:
                problems.append(f"{path} returned HTTP {exc.code}")
                continue
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{path} could not be fetched: {exc}")
                continue
            if status != 200:
                problems.append(f"{path} returned HTTP {status}")
            elif not body:
                problems.append(f"{path} served an empty file")
        if not problems:
            log("[smoke] the bundled app boots and serves its UI")
    finally:
        _kill(proc, log)
    return problems


def _kill(proc: subprocess.Popen, log=print) -> None:
    """Stop the app and any WebView2 children it spawned."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, check=False)
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Check a built Easy Life bundle.")
    parser.add_argument("bundle", nargs="?",
                        help="dist/Easy Life (folder) or dist/Easy Life.exe")
    parser.add_argument("--smoke", action="store_true",
                        help="also launch the built app and check it serves the UI")
    parser.add_argument("--smoke-source", action="store_true",
                        help="no bundle: launch desktop.py from THIS checkout with "
                             "the current Python (used by setup_windows.bat)")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="seconds to wait for the app to answer (default 60)")
    args = parser.parse_args(argv)

    problems: list[str] = []

    # --- source check: used before installing, to catch a broken environment --
    if args.smoke_source:
        root = Path(__file__).resolve().parent.parent
        entry = root / "desktop.py"
        if not entry.exists():
            print(f"[verify] cannot smoke test: {entry} does not exist")
            return 1
        print(f"[verify] checking that the app starts from source: {entry}")
        with tempfile.TemporaryDirectory(prefix="pcrituals-smoke-") as tmp:
            problems = smoke_test([sys.executable, str(entry)], Path(tmp),
                                  timeout=args.timeout, cwd=root)
        if problems:
            print("\n[verify] FAILED - the app did not start cleanly:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("[verify] OK - the app starts and serves its interface.")
        return 0

    if not args.bundle:
        parser.error("give a bundle path, or use --smoke-source")

    bundle = Path(args.bundle)
    notes: list[str] = []
    web: Optional[Path] = None
    problems, notes, web = check_bundle(bundle)

    for note in notes:
        print(f"[verify] {note}")

    if args.smoke:
        if problems:
            print("[verify] skipping the smoke test: the bundle is already broken")
        else:
            exe = bundle if bundle.is_file() else bundle / APP_EXE
            if not exe.exists():
                problems.append(f"cannot smoke test: {exe} does not exist")
            else:
                with tempfile.TemporaryDirectory(prefix="pcrituals-smoke-") as tmp:
                    problems += smoke_test([str(exe)], Path(tmp), timeout=args.timeout)

    if problems:
        print("\n[verify] FAILED - the built app would not work for the user:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("[verify] OK - the bundle contains everything the app needs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
