# Control your PC from anywhere (remote access)

By default your phone only reaches your PC on the **same Wi-Fi**. This adds
access from anywhere — mobile data, a hotel, a friend's house.

We use **[Tailscale](https://tailscale.com)**. It puts your PC and your phone on
a private, encrypted network of their own. It is free for personal use, takes
about five minutes, and — importantly — **it does not put your PC on the public
internet.** Nothing is exposed, there is no port to forward, and no open door for
someone to find and attack.

> **Why not just "open a port"?** Port forwarding is free and works, but it makes
> your PC's web server directly reachable by anyone on the internet. The app has
> a login and expiring pairing codes, so it isn't wide open — but you'd be
> trusting that to protect a machine that can run programs and shut itself down.
> Tailscale avoids the gamble entirely.

---

## One-time setup

### 1. On your PC

1. Go to <https://tailscale.com/download/windows> and install it.
2. Run it and **sign in** — Google, Microsoft, GitHub, or an email address all
   work. Whatever you choose, remember it: your phone has to use the *same*
   account.
3. Once it says **Connected**, you're done on the PC.

### 2. On your iPhone

1. Install **Tailscale** from the App Store.
2. Open it and **sign in with the same account you just used.**
3. Turn the switch **on**. It will ask permission to add a VPN configuration —
   that's how it works, and it's safe to allow.

### 3. Check Easy Life can see it

Open Easy Life on your PC → **Phone Remote**.

You should now see a green card: **"✓ Remote access is on"** with an address
like `https://100.101.102.103:8443`.

If you instead see **"Control your PC from anywhere"**, Easy Life hasn't spotted
Tailscale yet — usually because it's not connected yet. Connect Tailscale, then
click away to another tab and back to refresh.

### 4. Pair your phone

Click **Generate Pairing Code** and scan the QR with your phone's camera. The
code now points at the Tailscale address, which works at home *and* away.

That's it. Test it by turning your phone's Wi-Fi **off** and using mobile data —
your Plaves should still run.

---

## Notes

**Which address does my phone use?**
A Tailscale address, because it works everywhere. Easy Life deliberately prefers
it over the home Wi-Fi address so you don't have to re-pair whenever you leave
the house. The home address is still shown as a fallback for a phone that hasn't
joined Tailscale yet.

**Does the camera scanner still work?**
Yes, and if you have HTTPS turned on, Easy Life automatically reissues its
certificate to cover the new address the first time it sees Tailscale — so the
scanner keeps working away from home. If your phone shows a certificate warning,
reinstall the certificate (see the main README).

**Battery and speed.**
Tailscale is very light, and when both devices are on the same Wi-Fi it talks to
them directly rather than going out to the internet — so at home it's no slower
than before.

**Is it really private?**
Yes. Your devices talk to each other over an encrypted connection. Nobody else
can reach your PC — not other Tailscale users, not us, not your ISP beyond
seeing encrypted traffic.

**Free?**
The free personal plan covers your own devices (up to 100). You only pay if a
business uses it.

**What if I don't want it any more?**
Turn Tailscale off, or uninstall it. Easy Life falls straight back to the home
Wi-Fi address with no other changes — nothing else is affected.

---

## Reaching it from anywhere with no phone app (Cloudflare Tunnel)

Want a phone to reach Easy Life from **outside** your home with nothing
installed? Easy Life publishes a **public** address through a **Cloudflare
Tunnel**. In **Settings → Use your phone from anywhere** → *Turn on remote
access*.

- Your phone just opens the link in Safari. **Nothing to install on the phone.**
- It needs the free `cloudflared` tool on the PC:
  `winget install --id Cloudflare.cloudflared`. Easy Life will not download and
  run a binary by itself — that's not a decision a desktop app should make
  quietly, so it tells you the command instead.
- The address is **public** and **changes every time you turn it on** — if the
  PC restarts, turn remote access back on to get a fresh link.

**Read this before using it.** The link is reachable by anyone who learns it.
Your login and pairing codes still protect the PC — and a tunnelled request can
no longer be mistaken for the trusted desktop UI (see the trust fix below) — so
keep a strong password.

### The trust fix this depends on

Loopback is trusted implicitly — the desktop window talks to `127.0.0.1` and
shouldn't have to log in. A tunnel breaks that assumption, because `cloudflared`
runs **on the PC**: internet traffic reaches the app with a *loopback* address.

Before that was fixed, such a request was treated as the trusted desktop UI and
got the **entire API — running Plays, power control, shutdown — with no
credential at all.** Publishing the app would have been a remote-shutdown
backdoor for anyone with the URL.

The rule now: **any forwarding header disqualifies a request from counting as
local.** It fails closed in the only direction that matters — a caller cannot
spoof their way *in*, because adding a header can only make a request look more
remote. Rate limits and lockouts key off the real forwarded address instead of
the shared loopback one, so one attacker can't lock out everybody through the
tunnel. Covered by `tests/test_tunnel_trust.py`.

---

## For whoever maintains this

The app never installs or configures Tailscale; it only *detects* it, because
quietly installing a VPN is not something a desktop app should do on its own.

- `pcrituals/net.py` — `tailscale_ips()` finds addresses in `100.64.0.0/10`
  (via interface enumeration, plus the `tailscale ip -4` CLI, which is
  authoritative when enumeration misses the adapter).
- `net.pairing_urls()` puts the tunnel address first; `net.cert_ips()` adds it
  to the certificate SANs.
- `pcrituals/app.py::pairing_base_url()` and `cert.py::san_names()` both read
  those helpers, so the address the QR advertises is always one the certificate
  names.
- `tests/test_remote_access.py` covers the range boundaries, ordering, the
  no-Tailscale fallback, and certificate reissue. Tailscale is faked; no test
  touches a real tunnel.
