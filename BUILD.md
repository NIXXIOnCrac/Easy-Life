# Building Easy Life

Everything about turning this project into something you (or your brother)
double-click: the app itself, the one-click installer, and the release pipeline.

There are three things you can do, in increasing order of "finished product":

| Goal | Double-click this | You get |
|------|-------------------|---------|
| **A. Run it as an app on this PC** | `setup_windows.bat` | A real app with a Start Menu entry, no building needed |
| **B. Build the finished app + installer** | `build_windows.bat` | `dist\Easy Life.exe`, `dist\Easy-Life-Setup-<version>.exe` |
| **C. Publish a release** | push a tag (GitHub Actions) | Installer + portable app + in-app update feed |

> **A Windows .exe can only be built on Windows.** PyInstaller cannot
> cross-compile, and no amount of scripting changes that. Everything in this
> file that *builds* something was written on a Linux machine and has to be run
> by you on a Windows 10/11 PC (or by the GitHub Actions workflow in
> `.github/workflows/release.yml`, which runs on a real Windows machine).

---

## What you must run on Windows yourself

1. **To use the app on this PC:** double-click **`setup_windows.bat`**.
2. **To give your brother a finished product:** install Inno Setup 6 once
   (see below), then double-click **`build_windows.bat`**, and hand him
   `dist\Easy-Life-Setup-<version>.exe`.
3. **To cut a release without a Windows PC:** `git tag v0.2.0 && git push origin v0.2.0`
   and let the release workflow do the Windows work in the cloud.

---

## What needs to be installed

| Needed for | What | Notes |
|---|---|---|
| A and B | **Python 3.10+** from python.org | On the first installer screen, **tick "Add python.exe to PATH"**. Do not use the Microsoft Store version. |
| B (the installer) | **Inno Setup 6** (free, ~5 MB) | `winget install -e --id JRSoftware.InnoSetup` or https://jrsoftware.org/isdl.php |
| A and B | Internet on the first run | Downloads the app's libraries into its own folder |
| Phones | **Microsoft Edge WebView2 runtime** | Already present on Windows 10/11. Without it the app opens in your normal browser instead — it still works. |

`build_windows.bat` checks all of these and tells you in plain words what is
missing instead of failing with a stack trace.

---

## A. Install as an app (no build step)

Double-click **`setup_windows.bat`**. It is a wizard, not a script: it prints what
it is doing at every step and asks before it does anything unusual.

1. Checks Python (and explains exactly how to install it if missing).
2. Asks where to install — default `%LOCALAPPDATA%\Programs\Easy Life`
   (your own user folder, **no administrator rights needed**).
3. Copies the application files there.
4. Creates a Python environment for it and downloads what it needs.
5. **Starts the app once and checks it really serves its interface** before
   making shortcuts (so you never get a shortcut that flashes and dies).
6. Creates a **Start Menu** entry and a **desktop shortcut** (with the proper
   icon), and offers to add the firewall rule that lets your iPhone control the
   PC — that is the only step that needs permission, and it asks for it once.
7. Launches Easy Life, then offers to build the standalone installer.

If a ready-made installer already exists in `dist\`, the wizard offers that
first and runs it silently (`/SILENT /SUPPRESSMSGBOXES /NORESTART`) — no clicks,
no Python needed.

- Uninstall: **`uninstall_windows.bat`** (or Apps & Features, if you used the
  installer).
- Quick run without installing anything: **`run_windows.bat`**.

---

## B. Build the app and the installer

Run **`build_windows.bat`** (double-click it). It creates a build-only virtual
environment (`.venv-build`, so it never disturbs a working install) and then:

| Step | What it does |
|---|---|
| 3 | Builds the **portable single file**: `dist\Easy Life.exe` |
| 4 | Builds the **app folder**: `dist\Easy Life\` (this is what the installer ships) |
| 5 | Checks both builds with `scripts\verify_bundle.py` (the whole UI, the icons and the exe must be present) and then **starts the built app once** to prove it really runs — a Easy Life window opens for a few seconds and closes by itself. Any failure here stops the build with the reason |
| 6 | Builds the **installer** with Inno Setup into `dist\Easy-Life-Setup-<version>.exe` |
| 7 | Makes `dist\Easy-Life-<version>-update.zip` (what the in-app updater downloads) and prints its SHA256 |

If Inno Setup is not installed it says so, prints the two ways to get it, and
still leaves you the portable `.exe` — you never end up with an unusable folder.

### The two build targets

`pcrituals.spec` builds two different things, chosen by an environment variable:

```
set PCRITUALS_TARGET=portable    (default)  ->  dist\Easy Life.exe      one file
set PCRITUALS_TARGET=installed              ->  dist\Easy Life\         folder + exe
```

(`build_windows.bat` does both for you.)

The **portable** file is one self-contained `.exe`. It unpacks its payload into
`%TEMP%` on every launch, so it starts slower (a few seconds) — that is normal
for one-file builds.

The **folder** build starts immediately and is what the installer ships. It is
also the only layout the in-app updater can replace in place.

### What the spec must not lose (each one is a bug that shipped)

- `datas = [(str(web_dir), "pcrituals/web")]` — the **whole** `pcrituals/web`
  folder (index.html, app.js, styles.css, tokens.css, liquid-glass.css,
  manifest.json, sw.js, icons). If it is missing, the window opens **blank**.
- `excludes` stays tiny (`test`, `unittest`, `pydoc`). Adding stdlib modules
  here is how a release crashed with `ModuleNotFoundError: No module named
  'locale'` — `subprocess`/`multiprocessing` import `locale`.
- `console=False` plus `desktop.py`'s stream guard (`_ensure_streams`,
  `log_config=None`). A windowed build has `sys.stdout is None`, and uvicorn's
  default logging config then dies with
  `'NoneType' object has no attribute 'isatty'`.
- `hiddenimports` includes the uvicorn submodules, `qrcode`, `webview`, and any
  module the app imports through a *string* (`__import__("uuid")`), because
  PyInstaller cannot follow a string argument.

`tests/test_packaging.py` asserts every one of those things, so a future edit
that breaks one of them fails the test suite instead of the user's PC.

### Verify by hand (2 minutes, on Windows)

```
dist\Easy Life\Easy Life.exe          a window opens with the dashboard
```
If the window is blank, the web folder was not bundled. To get a console with
the error messages, set `console=True` in `pcrituals.spec` and rebuild.

The automated version of that check (used by the build and by CI):

```
.venv-build\Scripts\python.exe scripts\verify_bundle.py "dist\Easy Life" --smoke
```

`--smoke` starts the built app, waits for its local server, and fetches the UI
files over HTTP. A packaged app that crashes on startup — the `locale` crash,
the `isatty` crash — fails this check instead of reaching a user.

---

## The Windows installer (`installer\pc-rituals.iss`)

Compile it (build_windows.bat does this):

```
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=0.1.0 installer\pc-rituals.iss
```

`/DAppVersion` is **required**, on purpose: a hard-coded version in the script
would eventually ship an installer claiming to be an older release. The script
refuses to compile without it, and it also refuses to compile if
`dist\Easy Life\` has not been built yet — with a plain-English message, not an
Inno error code.

### Where it installs, and why not Program Files

```
Program : %LOCALAPPDATA%\Programs\Easy Life
Data    : %LOCALAPPDATA%\Easy Life\data
```

Program files go in the **per-user** folder because the in-app updater
(`pcrituals/update.py`) writes a new version straight into the install folder.
Windows does not allow an ordinary program to write into `C:\Program Files`
without an administrator prompt every single time — so a Program Files install
would break "Update now", which is exactly the feature that avoids the
"download and run another installer" annoyance. Two side benefits: installing,
updating and uninstalling never ask for administrator rights.

### What it creates

- A **Start Menu** entry, always.
- A **desktop shortcut** — optional (a checkbox, remembered on upgrades).
- **Start Easy Life when I log in** — optional, off by default (adds/removes a
  per-user `Run` registry value).
- **Firewall rule for phone control** — optional, off by default. Runs
  `scripts\add_firewall_rule.bat`, which asks Windows for permission itself; the
  install itself stays unprivileged.
- A proper **uninstaller** in Apps & Features, which also removes the firewall
  rule (best effort) and the shortcuts.

### Silent install

```
Easy-Life-Setup-0.1.0.exe /SILENT /SUPPRESSMSGBOXES /NORESTART
Easy-Life-Setup-0.1.0.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
Easy-Life-Setup-0.1.0.exe /SILENT /TASKS="desktopicon,startupicon,firewallrule"
```

Without `/TASKS` you get the defaults: Start Menu yes, desktop shortcut yes,
run-at-login no, firewall rule no. Silent installs are why every message box in
the script is a `SuppressibleMsgBox` — a plain one would sit there waiting for a
click that never comes.

### Your rituals survive uninstalling

This is deliberate and tested. The data folder
`%LOCALAPPDATA%\Easy Life\data` is a **sibling** of the program folder
`%LOCALAPPDATA%\Programs\Easy Life`, never a child of it, so removing the
program cannot reach the rituals, the settings, the pairings or the log. The
uninstaller says so out loud and tells you where the folder is, in case you
really do want it gone.

`tests/test_packaging.py` proves it as far as is possible without a Windows PC:
it reads the data path out of `pcrituals/config.py`, checks the data folder is
not inside the install folder, checks the only thing `[InstallDelete]` clears is
the program's own `_internal\` payload, and checks that every delete in the
script's code stays inside the program folder. The one-time manual confirmation
(when you get to a Windows box): install, create a ritual, uninstall, then open
`%LOCALAPPDATA%\Easy Life\data` — your ritual file is still there, and
reinstalling shows it again.

### Windows will warn about the installer

The installer and the app are **not code-signed** (a certificate costs money).
The first run shows a blue "Windows protected your PC" box: click **More info**
then **Run anyway**. Everything still installs normally. If you ever buy a
certificate, add `SignTool=...` to the `[Setup]` section of the `.iss`.

---

## C. Releases (GitHub Actions)

`.github/workflows/release.yml` builds everything on a real **Windows** runner
and publishes it. Trigger it with a version tag:

```
git tag v0.2.0
git push origin v0.2.0
```

It runs the whole test suite first, then builds the portable app and the app
folder, checks the bundles, **starts the built app** (smoke test), builds the
installer with Inno Setup, and attaches to the GitHub Release:

| Asset | What it is |
|---|---|
| `Easy-Life-Setup-<version>.exe` | The installer to give to people |
| `Easy-Life-<version>-portable.exe` | The single-file portable build |
| `Easy-Life-<version>-update.zip` | Exactly what the in-app "Update now" downloads |
| `update.json` | The update manifest the app reads |

Nothing is published unless every step passes.

---

## Updating an installed copy

### In-app (one click, the preferred way)

Point the app at the manifest URL once — **Settings → Updates**:

```
https://github.com/<your-user>/<your-repo>/releases/latest/download/update.json
```

From then on the app checks that URL on startup and shows an "Update available"
banner with an **Update now** button. The manifest looks like this:

```json
{
  "version": "0.2.0",
  "url": "https://github.com/you/pc-rituals/releases/download/v0.2.0/Easy-Life-0.2.0-update.zip",
  "notes": "Faster starts, fixed the close-app step.",
  "sha256": "…64 hex characters…"
}
```

Never hand-write that file: it is produced by
**`scripts/make_update_manifest.py`** (the release workflow calls it), because
the four field names have to stay exactly what `pcrituals/update.py` reads.
`tests/test_packaging.py` executes the real updater against a generated manifest
to prove the contract holds, including that a tampered download is rejected by
the SHA256 check.

### Manual, if an update ever misbehaves

Run the new `Easy-Life-Setup-<version>.exe` — it upgrades in place and keeps
your rituals. Or, for a source install, re-run `setup_windows.bat`.

### Known limitations of in-app updating (honest list)

- The updater copies the new **files** over the install folder. It cannot remove
  files that no longer exist, and it cannot install new Python *libraries* — the
  folder build bundles its own runtime, so a release that adds a dependency is
  fine for the frozen app but a source install would need `setup_windows.bat`
  again.
- The app cannot replace its own `.exe`/DLLs while it is running. If the "Update
  now" step reports a permission error on a file, close Easy Life and run the
  installer once; everything from then on is a normal in-app update.
- **Gap in the app code (not this area):** `pcrituals/app.py`'s
  `_update_check_result()` passes only `version`, `url` and `notes` on to the
  downloader, so the `sha256` in the manifest is verified when
  `pcrituals/update.py` is used directly (and in the tests here) but is dropped
  on the way through the UI path. The manifest still carries the checksum, so
  fixing `app.py` is a one-line change in the app layer.

---

## Run from source (development)

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pcrituals            # or: launcher.py
```

Open http://127.0.0.1:8765 (or just run `desktop.py` for the app window).

Tests: `run_tests.bat`, or `python -m pytest tests -q` for the backend and
packaging suites. `tests/test_packaging.py` is the off-Windows verification of
everything in this document — 35 checks covering the spec, the manifest
contract, the installer's data-safety and every script BUILD.md names.

---

## What I could not verify

Written by an assistant working on **Linux**, with no Windows machine and no way
to produce a `.exe`. These are the things that are therefore *believed* correct
but not *proven*, and you are the one who has to prove them on Windows:

1. **The .exe builds and runs.** PyInstaller cannot cross-compile; nothing here
   ever ran a packaged app. `build_windows.bat` on Windows plus its
   `verify_bundle.py --smoke` step is what will confirm it.
2. **The Inno Setup script compiles.** Inno Setup is Windows-only, so
   `installer\pc-rituals.iss` has never been compiled. Its structure, versions,
   paths and data-safety are asserted statically by the tests, and the release
   workflow compiles it on a Windows runner — expect to fix a syntax nit on the
   first run, not a design problem.
3. **Installer behaviour end to end:** shortcuts, the "run at login" checkbox,
   the firewall prompt, the uninstaller, the silent switches, and that the
   uninstaller really leaves `%LOCALAPPDATA%\Easy Life\data` alone. See the
   manual check in the installer section above.
4. **The `.bat` scripts under cmd.exe.** They are checked for the usual Windows
   traps (CRLF line endings, quoted paths so folders with spaces work, balanced
   `setlocal`/`endlocal`, every `goto` target existing, Python-missing messages,
   a `pause` so you can read the output) — but cmd.exe never executed them here.
5. **The native window itself** (WebView2) and the phone PWA: Windows-only.
6. **GitHub Actions run #1.** The workflow is written for a `windows-latest`
   runner and its YAML is validated here, but the first real run is yours.
7. **The code-signing warning.** Expected, not tested: unsigned apps always get
   the SmartScreen prompt.

Everything that *could* be verified off Windows is, and is in
`tests/test_packaging.py`: the spec's inclusions and excludes (the `locale`
regression), the bundled UI file list, the update manifest contract executed
against the real updater with a real checksum, the installer's data-safety
reasoning, and the existence of every script named in this document.

---

## Architecture / key files

```
desktop.py               desktop app entry point (native WebView2 window) — packaged entry
launcher.py              simple entry point (browser mode / fallback)
pcrituals/
  api.py                 FastAPI app + routes + serves the web UI
  app.py                 service layer (orchestrates everything)
  engine.py              ritual execution engine (progress/cancel/timeout/retry)
  models.py              pydantic models (Ritual, Action, DeckButton, ...)
  storage.py             SQLite persistence
  security.py            pairing codes + device tokens (hashed)
  platform.py            Windows automation (os.startfile, cmd, taskkill, powershell)
  media.py               media keys + now-playing
  icons.py               auto icon resolution (favicons / brand logos)
  integrations.py        game-store / app detection
  wol.py                 Wake-on-LAN
  voice.py               voice command parsing
  backup.py              backup/restore
  update.py              in-app updater (reads the update manifest)
  web/                   the UI (PWA) — MUST be bundled for the exe
pcrituals.spec           PyInstaller build definition (two targets - see step B)
build_windows.bat        one-double-click build (portable + folder + installer)
setup_windows.bat        one-double-click installer wizard (no build needed)
installer/pc-rituals.iss Inno Setup script for the real installer
scripts/
  verify_bundle.py       checks a built bundle (and smoke-tests it) - runs on Windows
  make_update_manifest.py writes the update manifest the app reads
  add_firewall_rule.bat  self-elevating firewall rule for phone control
  package_zip.py         packages the source tree into a zip for a source install
  seed.py                sample rituals for a fresh install
.github/workflows/release.yml  tag -> Windows build -> GitHub Release + update feed
```

## Login & data location

**Local login.** On first run the app asks you to create a username + password;
after that it asks you to sign in. Passwords are stored only as salted PBKDF2
hashes. Once an account exists, every control endpoint requires a session (or a
paired phone's device token). Sign out from **Settings → Account**.

**One data folder.** The source install and the packaged app share the same data
directory, so they see the same rituals:

```
Windows : %LOCALAPPDATA%\Easy Life\data
other   : ~/.pcrituals/data
```

On first launch the app migrates data from older locations (the old temp folder
or an exe-relative `data\`), so existing rituals carry over. For a truly portable
build (data next to the exe), drop a file named `portable.txt` next to the
executable — note that a portable data folder *does* live inside the program
folder, so keep it out of your uninstall habits.

## Gotchas

- **Build on Windows.** No cross-compiling, ever.
- **Bundle `pcrituals/web/`** or the UI is blank.
- **Never add stdlib modules to `excludes`** in the spec (see the `locale` story).
- **Windowed builds have no stdout**; `desktop.py` handles it. Do not remove the
  guard or pass a uvicorn `log_config`.
- Default port **8765**; the app binds `0.0.0.0` so a phone on the same Wi-Fi can
  reach it, and every control endpoint requires a token.
- Antivirus is the most common cause of a failed build: if PyInstaller or
  `verify_bundle.py` fails oddly, add an exclusion for the project folder,
  delete `build\` and `dist\`, and try again.
- A previous build's leftovers can confuse PyInstaller; `build_windows.bat`
  passes `--clean` for exactly that reason.
- Windows-only behaviour (launching apps, power control, media keys, WOL) can
  only be tested on a Windows machine.
- Known gap in the app (not this area): the media player shows title/artist but
  **not** real album art or playback position — that needs the Spotify Web API.
