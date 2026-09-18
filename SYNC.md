# Sync across PCs (optional)

Easy Life runs entirely on your own computer by default. **Nothing is sent
anywhere and no account is created** unless you turn sync on.

Sync lets you keep the same rituals on two PCs (say a desktop and a laptop) by
running your own small sync server. It is not a cloud service and there is no
company in the middle — the server is a program you start yourself.

---

## Why it works this way

Your app's whole point is that it can control your PC. Putting that control on
the public internet would be a serious risk, so Easy Life deliberately does not
do it. Sync is designed to be reachable **only on your own network**:

* The sync server holds **rituals and settings only** — never anything that can
  control a PC. It does not know how to run a ritual.
* It listens on your LAN. To sync across the internet you would put it behind a
  VPN/mesh network (Tailscale, WireGuard) rather than opening a port.
* Your **login password for the app never goes to the sync server.** The sync
  server has its own separate username and password.

---

## 1. Start the sync server

On the PC that will hold the shared copy (or any always-on machine):

```bat
.venv\Scripts\python.exe -m pcrituals.sync_server
```

You will see it listening on port **8788**. From another machine on the same
Wi-Fi, the address is `http://<that-pc-ip>:8788` — the same IP you use for the
phone remote.

Optional settings (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `PCRITUALS_SYNC_PORT` | `8788` | Port to listen on |
| `PCRITUALS_SYNC_HOST` | `0.0.0.0` | Interface to bind |
| `PCRITUALS_SYNC_DB` | app data dir | Where the server keeps its database |

## 2. Connect this PC

Open Easy Life → **Settings** → **Sync across PCs**.

1. Server address: `http://192.168.1.20:8788` (use your server's real IP).
2. Pick a username and password (at least **8 characters** — the server is
   stricter than the local app).
3. Click **Create account** the first time. After that, the same details with
   **Connect** on your other PC.

## 3. Move your rituals

* **⬆ Push this PC** — uploads this PC's rituals to the server.
* **⬇ Pull (merge)** — downloads them and *keeps* what is already here.
* **⬇ Pull (replace)** — wipes this PC's rituals and replaces them with the
  server's copy. You are asked to confirm.

Typical first run: **Push** from the machine that already has your rituals, then
**Pull (merge)** on the new one.

### "The sync server has newer data"

If another computer pushed after your last sync, Easy Life refuses to
overwrite it silently. You get a choice: overwrite the server with this PC's
copy, or cancel and pull first. It never discards work without asking.

---

## What is deliberately NOT synced

Anything that is specific to one machine, or that is a credential:

* The PC's MAC address and Wake-on-LAN settings
* Paired devices / phone access
* App login sessions and passwords
* Your Spotify credentials
* The sync password itself, and the sync token

A vault is uploaded to a server and could be downloaded onto another PC, so
credentials must never travel inside it. This is enforced in
`pcrituals/vault.py` and covered by tests.

## Stopping

* **Disconnect** (in Settings) forgets the link on this PC. Your rituals stay
  here and the server copy is left alone.
* To delete the remote copy too, call `DELETE /sync/account` on the sync server
  with your sync password (this also removes the account).
* Deleting the sync server's database file removes everything it holds.

---

## Where the sync token is stored

`sync.json` in the app's data directory, with owner-only permissions (`0600`).
It is not stored in the settings file, and it is never sent to the browser.

## Developer notes

* `pcrituals/sync_server.py` — the standalone server (accounts, tokens, vaults).
* `pcrituals/sync.py` — the HTTP client (standard library only).
* `pcrituals/vault.py` — what a snapshot contains, and how one is applied.
* `pcrituals/sync_link.py` — the app-side glue holding the local token.

Tests: `tests/test_sync_server.py`, `test_vault.py`, `test_sync_api.py`
(the last one starts a real server and does a full round trip between two
simulated installs).
