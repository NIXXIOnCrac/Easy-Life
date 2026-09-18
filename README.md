# Easy Life
  DOWNLOAD THE Easy-Life Download.zip file to downlaod then extract it and run setup_windows.bat and in it complete the setup and the build and the app will open and you can search it on your windows search bar to open

> ### The App For Streamers, By Streamers 🎮
> **Built by a streamer, for streamers.** Run your whole PC from your phone — launch apps, control OBS, rescue a dead stream, and never worry you're away from your setup when something goes wrong.

[![Twitch](https://img.shields.io/badge/Twitch-yoki8ems-9146FF?logo=twitch&logoColor=white)](https://twitch.tv/yoki8ems)

**Easy Life** turns your Windows PC into a personal automation hub you can drive from your desktop **or your phone, from anywhere**. You build **Plays** — simple, ordered steps like *open OBS → switch to scene "MAIN 1" → start streaming to Twitch* — and then fire them with one tap.

No coding. No cloud account. No subscription. Your data stays on **your** PC.

---

## 🎬 Why streamers love it

If you stream, Easy Life is the one app that watches your stream *for* you while you're away from the keyboard.

| What it does | How it helps |
|---|---|
| **Streamer Mode** | Turns on an OBS tab in the app so you can control OBS scenes & your stream from your phone |
| **One-tap Stream Rescue** | OBS glitched or your stream dropped mid-broadcast? Press one button and it restarts the stream — from your phone, even from outside your home |
| **Scene switching** | Switch to your "BRB", "Intermission" or "MAIN" scene without running upstairs to the desk |
| **Stream-drop alerts** | Get a **push notification on your phone** the moment your stream drops, so you know to rescue it even when you're not looking |
| **"Describe it" builder** | Type *"open obs then switch to MAIN 1 scene then start stream to twitch"* and Easy Life builds the Play for you |

> Check me out on Twitch: **[twitch.tv/yoki8ems](https://twitch.tv/yoki8ems)**

---

## ✨ What is a "Play"?

A **Play** is a saved list of steps your PC runs, in order. Each step is one action:

| Action | Example |
|---|---|
| Open an app | Open **Spotify** |
| Open a website | Open **youtube.com** |
| Launch a game | Launch **Steam** (or a specific Steam/Epic/Battle.net/Riot game by its store link) |
| Open a file / folder | Open **D:\Clips\best-play** |
| Run a command | Run **git pull** (or any PowerShell/CMD command) |
| Wait | Wait **30 seconds** |
| Close an app | Close **OBS** |
| PC power | **Lock / Sleep / Restart / Shutdown** your PC (asks you to confirm first) |
| OBS scene | Switch OBS to the **BRB** scene |
| OBS stream | **Start / stop streaming** to Twitch |

You can reorder steps by dragging, enable or disable them, set a **timeout** per step, and choose whether a failed step **stops the Play** or **continues to the next one**.

**Examples of real Plays people build:**

- 🎮 **"My setup"** — open Streamlabs, Spotify, your Discord, then wait 20s and launch your game.
- 📺 **"Going live"** — open OBS, switch to "MAIN 1" scene, start streaming to Twitch.
- 🌙 **"Shut down"** — close all your apps, wait 10 seconds, shutdown.
- 🌐 **"Content digest"** — open youtube.com, your analytics dashboard, and TikTok, one after another.

You can also pin your most-used Plays as big touch buttons on your **Deck** — your personal control surface.

---

## ✨ What you can do

### 🖥️ Desktop app (Windows)
- **Build Plays** — a visual builder with drag-to-reorder, plus the magic **"Describe it"** box that writes the steps for you.
- **Dashboard** — PC status at a glance, your Plays, and live progress while something runs.
- **Deck** — a grid of one-tap buttons, good for launching your whole setup.
- **Activity / History** — every run, searchable and filterable, including *why* a Play failed.
- **Settings** — pairing, Streamer Mode, notifications, remote access, backups, voice control, updates and more.

### 📱 Phone app (iPhone PWA)
No app-store install. Pair your phone once with a code or QR, then **Add to Home Screen** to get an app-like icon.

- **Run & stop Plays** from anywhere.
- **Live progress** of whatever the PC is doing.
- **PC power controls** (lock / sleep / restart / shutdown, with confirmation).
- **OBS tab** — the moment Streamer Mode is on, control OBS scenes and stream from your phone.
- **Stream Rescue card** — save a dropped stream.
- **Media control** — play/pause/skip, volume, and now-playing.

### 🌐 Remote access (outside your home)
Place your phone on the **same Wi-Fi** and it just works. Want it to work from **anywhere** (mobile data, a hotel, anywhere)? Easy Life publishes a public address through a **Cloudflare tunnel** — nothing to install on the phone, just open the link:

**Settings → Use your phone from anywhere → Turn on.**

> The address changes each time you turn it on — if your PC restarts, turn remote access back on for a fresh link. The app warns you about this up front.

---

## 🔒 Private by design

- **Local-first.** There is no Easy Life cloud and no account. Your Plays, history, pairings and credentials live in one folder on **your** PC.
- **Everything needs a login.** Once you create a username + password, every action from every device requires a session (or a paired phone's token). Passwords are stored only as salted hashes.
- **Pairing is safe.** Your phone gets a **single-use, expiring code** (5 minutes) to pair; from then on it uses a revocable device token.
- **Destructive actions confirm.** Lock/sleep/restart/shutdown always ask before running.
- **Optional HTTPS** encrypts traffic between your phone and PC (needed for the QR scanner in Safari anyway).
- **What leaves your PC is tiny and knowable:** an app icon lookup here, a Spotify cover there, the update check — each optional and listed in the Security section below.

---

## 🚀 Installation

### Easiest (no build) — Windows
1. Unzip the downloaded folder.
2. Double-click **`setup_windows.bat`**.
3. Let it install (it needs internet on the first run, then nothing). It adds a **Start Menu** entry and a desktop shortcut.
4. Double-click **`run_windows.bat`** to launch, or open **Easy Life** from your Start Menu.

> Your Plays and settings live in `%LOCALAPPDATA%\Easy Life\data`. **Uninstalling Easy Life never deletes that folder** — reinstall and everything comes straight back.

### From source (developers)
```bash
uv venv .venv && . .venv/bin/activate
uv pip install -e .
python -m pcrituals          # desktop at http://127.0.0.1:8765
```

---

## 🎬 Getting live on Twitch (3-minute setup)

1. **Install OBS** and turn on its **WebSocket Server** (Tools → WebSocket Server Settings, note the **password**).
2. In Easy Life: **Settings → Streamer Mode → Turn on**. A new **OBS** tab appears.
3. Paste your OBS password into the Streamer Mode card (set your OBS location if Easy Life can't find it).
4. On your phone (paired, see below): tap the **OBS** tab. You can now see live stream status, switch scenes, and hit **Fix my stream** if it drops.
5. Optional, the auto-saver: **Settings → Notifications → turn on "Notify me when the stream drops"** — you get a push alert on your phone if your stream dies while you're away.

**Honest limit:** "start stream to Twitch" starts whatever OBS is already set to output to. Set your streaming destination inside OBS once, and Easy Life will start it reliably every time.

---

## 📱 Pairing your iPhone

1. On the PC: **Phone Remote → Generate Pairing Code**.
2. On your iPhone, open Safari and go to the address shown (or scan the QR code).
3. Enter the short code.
4. Tap **Add to Home Screen** to get an app-like icon on your home screen.
5. Done — run Plays, check live progress, control OBS and power.

> **Pairing codes expire in 5 minutes.** If a code expires, just generate a new one.

---

## 🛠 Power, Voice & Extras

- **Power controls** — Lock, Sleep, Restart, Shutdown from desktop or phone (with confirmation).
- **Wake-on-LAN** — send a Magic Packet to wake a PC on your network.
- **Voice control** — say *"start my setup"* or *"shut down the PC"* (power commands confirm with you first).
- **Integrations** — Easy Life detects Steam, Epic, Battle.net, Riot, Discord, Spotify, RGB and Wallpaper Engine and can launch games through each store's own link.
- **Backups** — create, list, restore and prune backups so your Plays are never lost.
- **Import / Export** — move Plays between PCs.
- **Optional sync** — sync Plays across your own PCs via a server you run (off by default).
- **In-app updates** — Easy Life checks a hosted manifest and offers an **Update Now** button, so users never re-download an installer.

---

## ⚙️ Environment variables (developers)

| Var | Default | Description |
|-----|---------|-------------|
| `PCRITUALS_HOST` | `0.0.0.0` | Bind host |
| `PCRITUALS_PORT` | `8765` | Port |
| `PCRITUALS_DATA_DIR` | `~/.pcrituals/data` (`%LOCALAPPDATA%\Easy Life\data`) | Where the data lives |
| `PCRITUALS_LOOPBACK` | `false` | `true` = this PC only, no LAN/phone |
| `PCRITUALS_PAIRING_TTL` | `300` | Pairing code lifetime (seconds) |
| `PCRITUALS_UPDATE_URL` | *(empty)* | Update manifest URL |
| `PCRITUALS_TLS` | `false` | `true` also serves HTTPS (`:8443`) |
| `PCRITUALS_TLS_PORT` | `8443` | HTTPS port |

**HTTPS (optional, needed for Safari's QR scanner):**
```bash
python -m pcrituals.cert --enable    # on, generates the self-signed cert
python -m pcrituals.cert --export ~/easylife.crt   # copy to the phone
python -m pcrituals.cert --disable   # off
```

---

## 🔒 Full security model

- **Login** — create an account; every control endpoint then requires a session. Passwords stored only as salted PBKDF2 hashes.
- **Pairing** — single-use, expiring codes; device tokens are **SHA-256 hashed** in storage, so a database leak doesn't expose working keys.
- **Network** — the server binds to the LAN so your phone can reach it, but remote requests must be paired (device token). Set `PCRITUALS_LOOPBACK=true` to serve only this PC.
- **Throttling** — login is throttled per username *and* per client address, so one host can't lock you out or brute-force in.
- **Power safety** — destructive actions require confirmation.
- **Import safety** — imports are validated and **block obviously destructive commands**.
- **Sync is optional & scoped** — the sync server stores Plays only (never anything that can control a PC) and never receives your login password.
- **Not exposed to the internet** — the API is designed to sit behind a private VPN/mesh; don't port-forward it.

### What leaves this PC
| Request | When | Avoid |
|---|---|---|
| Google favicon (deck-button icon) | A deck button is a website with no icon set | Set an emoji/icon in the **Icon** field |
| Deezer/iTunes cover-art lookup | Spotify playing without an album | Connect Spotify in Settings |
| Spotify Web API | You connected Spotify | Don't connect it |
| Update manifest | Only if you set an update URL | Leave it empty |
| Your sync server | Only if you enable Sync | Leave Sync off |

---

## 🧰 For developers

### Architecture
```
pcrituals/
  config.py      config + data-dir layout
  models.py      Pydantic models: Ritual/Play, Action, History
  platform.py    Windows + Generic platform abstraction
  actions.py     dispatch an Action to its real OS operation
  engine.py      sequential engine (progress/cancel/timeout/retry/continue-stop)
  storage.py     SQLite persistence
  security.py    pairing codes, device tokens, server secret
  app.py         service layer + live event hub
  api.py         FastAPI REST + SSE + serves the web UI
  obs.py         OBS remote rescue (WebSocket)
  nlplay.py      "Describe it" natural-language Play builder
  notify.py      Web Push (VAPID) + stream-drop watcher
  tunnel.py      Cloudflare quick-tunnel remote access
  streamwatch.py stream-drop detection
  web/           desktop UI + iPhone PWA (single responsive SPA)
```

**Stack:** Python 3.10+, FastAPI, Uvicorn, SQLite (stdlib), vanilla JS SPA. Windows automation uses `os.startfile`, `cmd /c`, `taskkill` and PowerShell, all behind the `Platform` interface so the engine is fully testable on non-Windows.

### Tests
```bash
python -m pytest -q            # backend suite
node tests/smoke_frontend.js   # a frontend/browser suite
run_tests.bat                  # backend + every frontend suite
```
The suites that matter most: `test_concurrency.py` (shared-SQLite 500s), `test_auth_throttle.py` (lockout), `test_audit_backend.py` (pairing/secret/updater), `frontend_regression_test.js` (renderer errors poisoning state), `phone_e2e_test.js` (full pairing+revoke), `test_packaging.py` (spec includes, update-manifest contract, data-safety).

### Windows packaging
PyInstaller `pcrituals.spec` (two targets: portable single file + installable folder), Inno Setup `installer/pc-rituals.iss` for the real `Easy-Life-Setup-<ver>.exe`. **A `.exe` can only be built on Windows** — the `.github/workflows/release.yml` does it on a free Windows GitHub runner and attaches the installer to a Release:

```
Easy-Life-Setup-<ver>.exe      the double-click installer (give this out)
Easy-Life-<ver>-portable.exe   single-file portable build
Easy-Life-<ver>-update.zip     what "Update now" downloads
update.json                    the update manifest
```

Full build/install/update details: **[`BUILD.md`](BUILD.md)**.

---

## ❓ Troubleshooting

| Problem | Fix |
|---|---|
| "Python was not found" | Reinstall Python and tick **Add to PATH** |
| Windows blocked the app | **More info → Run anyway** (unsigned — a paid cert fixes this) |
| An app won't launch | Use the **full path** to the `.exe` in the action |
| Phone can't connect (same Wi-Fi) | Firewall allow? Re-run `setup_windows.bat` |
| Pairing code rejected | Codes expire in 5 min — generate a new one |
| OBS says "isn't responding" | Enable OBS **Tools → WebSocket Server Settings** and paste the password into Streamer Mode |
| "Fix my stream" can't find OBS | Set **OBS location** to the full path of `obs64.exe` |
| Test notification never arrives | Install the app to your iPhone Home Screen, and use the remote-access (HTTPS) address |
| Link stops working after a reboot | The address changes on restart — turn remote access back on for a fresh link |

---

## 🛣 Roadmap

**Built:** Plays + visual & "Describe it" builders, live progress, history, phone PWA + pairing, Streamer Mode + OBS tab + stream rescue, stream-drop notifications, remote access (Cloudflare), power controls, Wake-on-LAN, voice control, integrations, import/export, backups, in-app updates, optional sync, Windows installer + GitHub releases.

**Next:** richer integration backends (OpenRGB/SignalRGB, Spotify playback control), a system-tray shell, and more OBS insights (bitrate, health) live on the phone.

---

🐛 Found a bug?
Tell me and I'll fix it — include what you were doing, what you expected, and a screenshot if you can.

🐞 Or open a GitHub Issue: New Issue

Made with ❤️ by **yoki8ems** — *The App For Streamers, By Streamers.*  
 [twitch.tv/yoki8ems](https://twitch.tv/yoki8ems)
