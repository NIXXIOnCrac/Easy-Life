# Easy Life — User Guide

A simple guide for getting started. No coding needed.

---

## 1. Install (one time)

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/>
   → on the first screen, **tick "Add python.exe to PATH"** → Install.
2. Unzip the `Easy-Life` folder somewhere (e.g. Documents).
3. Double-click **`setup_windows.bat`**.
   - Click **Yes** when Windows asks for permission (needed for phone access).
   - Press **Enter** to accept the default install folder.
   - It installs everything and opens the app.

After that, open Easy Life any time from the **desktop shortcut** or **Start menu**.

> If Windows shows a "Windows protected your PC" warning, click **More info → Run anyway**.
> That's just because the app isn't code-signed.

---

## 2. First run — create your account

The first time you open it, it asks you to **create a username and password**.

- This is your **local** login — it keeps your rituals private on this PC.
- Your password is **hashed** (scrambled), never stored in plain text.
- **Write it down.** There's no "forgot password" email.

Once you sign in, a short **tour** walks you through the app. You can replay it any
time from **Settings → Show tutorial** at the bottom.

---

## 3. Create your first Ritual

A **Ritual** is a list of things you want your PC to do, in order.

1. Click **Rituals** in the left sidebar.
2. Click **+ New Ritual**.
3. Give it a name (e.g. "Gaming") → **Create**.
4. You're now in the **Builder**. Click the buttons to add actions:

| Button | What it does | Example |
|--------|--------------|---------|
| **+ Launch App** | opens a program | `discord` or the full path to the `.exe` |
| **+ Launch Game** | starts a game | `steam://rungameid/570` |
| **+ Open Website** | opens a page | `https://youtube.com` |
| **+ Open File/Folder** | opens Explorer | `C:\Users\You\Downloads` |
| **+ Run Command** | runs a command | `start lighting.cmd` |
| **+ Delay** | waits | `3` (seconds) |
| **+ Close App** | quits an app | `discord` |
| **+ Power Control** | Lock / Sleep / Restart / Shut down | — |

5. **Drag the ⠿ handle** to reorder steps.
6. Hit **Save**, then **▶ Run**.

**Example — a Gaming ritual:**
```
1. Launch App      → discord
2. Launch App      → spotify
3. Launch Game     → steam://rungameid/570
4. Delay           → 3
5. Launch App      → C:\Program Files\OBS\obs64.exe
```

You can also start a saved Ritual from a single button — see the **Ritual** type
in the next section. A Ritual can't contain another Ritual: the builder has no
"Ritual" step (its steps are the eight in the table above).

---

## 4. The Deck (quick-fire buttons)

**Deck** is your control panel of big buttons.

1. Click **Deck** → **+ Add Button**.
2. Pick a type — **App, Game, Website, File/Folder, Command, Ritual, Media** or
   **Power** (the same eight the app offers).
3. Type what it should open — the **icon fills in automatically** (game logos,
   website favicons, app logos). You can also paste your own emoji or image URL
   in the **Icon** box, and then nothing is downloaded (see *About the internet*
   at the end).
4. Drag buttons to reorder. Click a button to fire it.

**Starting a Ritual from a button:** choose type **Ritual** and paste the
Ritual's ID into **Target** — it is the code in the address bar when you open
that Ritual (`…/#/builder/5ebe9139c722`). Pressing the button runs the whole
Ritual, and the run shows up in **Activity** like any other.

There's also a **music player** at the top — it shows what's playing (with album
art) and lets you play/pause, skip, shuffle and repeat.

---

## 5. Control it from your iPhone

1. On the PC, go to **Phone Remote** → **Generate Pairing Code**.
2. On your iPhone (on the **same Wi-Fi**), open **Safari** and go to the address
   shown on screen (something like `http://192.168.1.42:8765`).
3. Type the 6-character code → **Pair this phone**.
4. In Safari: **Share → Add to Home Screen** — now it's an app on your phone.

From your phone you can run/stop rituals, watch progress, and lock/sleep/restart
the PC.

---

## 6. Settings worth knowing

- **Spotify** — connect for exact album art and real playback controls.
- **Paired Devices** — see/remove phones that can control this PC.
- **Backups** — save a snapshot of all your rituals; restore anytime.
- **Sync across PCs** — optional. Keeps your rituals on a second computer. It is
  **off until you turn it on**, and you run the sync server yourself — nothing is
  sent to any company. Full instructions are in `SYNC.md`.
- **Voice Control** — try phrases like "start gaming" or "lock the pc".
- **Account** — change your password, replay the tutorial, or sign out.

---

## 7. Streaming: control OBS from your phone

If you stream (Twitch, YouTube) and you ever leave the house while live, this is
the part built for you.

### Turn on Streamer Mode

**Settings → Streamer Mode → Show the OBS tab.** A new **OBS** tab appears with:

- live status (and how long you have been live),
- a **Fix my stream** button — if OBS is still running it just starts the
  stream; if OBS has crashed it relaunches it, waits for it to come back, then
  starts streaming,
- a **Stop streaming** button,
- your scenes, one tap each.

Everything works from the phone too, so you can rescue a stream from anywhere.

### Connect Easy Life to OBS (one time)

In OBS: **Tools → WebSocket Server Settings** → tick **Enable WebSocket
server** → copy the password. In Easy Life: **Settings → Streamer Mode** →
paste it into **OBS password** → **Save OBS settings**. It should say
"✓ Connected to OBS."

### Describe a Play in plain English

In the **Builder** there is a **Describe it** box. Type what you want and press
**Build it**:

> open obs then switch to MAIN 1 scene then start stream, name it Stream Rescue

It creates the steps and names the Play. You can still edit everything
afterwards, and there is an **Undo** if you change your mind.

It understands: opening apps and websites, OBS scenes ("switch to MAIN 1
scene"), starting/stopping your stream, waits ("wait 30 seconds"), locking or
restarting the PC, and running commands. If it does not understand part of what
you typed it **says so** rather than quietly leaving a step out.

One honest limit: "start stream **to twitch**" starts whatever OBS is already
set to. Easy Life cannot change the destination.

### Get told when the stream drops

**Settings → Notifications → Notify me when the stream drops.** Your PC watches
OBS itself and pushes a notification to your phone, so you find out without
having to check. Use **Send a test notification** to prove it works. This needs
the app installed to your iPhone Home Screen (Share → Add to Home Screen).

### Being reachable from anywhere

**Settings → Use your phone from anywhere.** Easy Life publishes this PC on a
public address through a **Cloudflare Tunnel**, so your phone can reach it from
anywhere — nothing to install on the phone, it just opens the link in Safari.

To set it up:

1. Install `cloudflared` once: `winget install --id Cloudflare.cloudflared`
2. Easy Life → Settings → **Use your phone from anywhere** → **Turn on**

The address is public, so anyone who learns it reaches the sign-in page. Keep a
strong password. Note the address **changes each time you turn it on** — if your
PC restarts, turn remote access back on to get a fresh link.

---

## Common problems

| Problem | Fix |
|---------|-----|
| "Python was not found" | Reinstall Python and tick **Add to PATH** |
| Windows blocked the app | **More info → Run anyway** |
| An app won't launch | Use the **full path** to the `.exe` in the action |
| Phone can't connect | Same Wi-Fi? Firewall allowed? Re-run `setup_windows.bat` |
| Pairing code rejected | Codes expire in 5 minutes — generate a new one |
| Nothing shows on the phone | Spotify only reports what's playing on an active device |
| OBS says "isn't responding" | In OBS, enable **Tools → WebSocket Server Settings** and paste the password into Easy Life's Streamer Mode card |
| "Fix my stream" can't find OBS | Set **OBS location** in the Streamer Mode card to the full path of `obs64.exe` |
| Test notification never arrives | Install the app to your iPhone Home Screen first (Share → Add to Home Screen), and use the Funnel (HTTPS) address |
| Phone link stops working after a reboot | The address changes on restart — turn remote access back on in Settings → **Use your phone from anywhere** to get a fresh link |

---

## Where your data lives

Everything stays on this PC:

```
%LOCALAPPDATA%\Easy Life\data
```

(Rituals, history, login, settings.)

### About the internet

There is no Easy Life account and no Easy Life server — nothing is sent to us,
and your rituals, history and password never leave the folder above. The app is
not completely offline, though: a few optional features look things up on the
web. These are all of them, and how to skip each one.

| What | When it happens | How to avoid it |
|------|-----------------|-----------------|
| **Deck button icons** | A button has no icon of its own, so a logo or favicon is loaded from Google's public favicon service (`www.google.com`) — Google sees which website or brand that button points at | Type your own emoji or image URL in the button's **Icon** box when you add it |
| **Album art** | Spotify is playing but you have not connected Spotify in Settings, so the artist and song title are looked up on Deezer (then iTunes) to find the cover | Connect Spotify in Settings, or don't use the music player |
| **Spotify** | Only if you connected Spotify in Settings: the app talks to your own Spotify account to show the exact track and to control playback | Don't connect it — the media keys and the window-title display still work |
| **Update check** | Only if you typed an update URL in **Settings → Updates**: one check for a new version when the app starts, and a download only when you click **Update Now** | Leave the update URL empty, which is the default |
| **Sync across PCs** | Only after you switch Sync on: your rituals, deck buttons and settings go to the sync server **you** run | Leave Sync off, which is the default |

Voice control, integration detection, Wake-on-LAN and the phone remote all work
on your own PC and network and send nothing outside it.
