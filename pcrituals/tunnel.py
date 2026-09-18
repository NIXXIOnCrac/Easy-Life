"""Publish the app through a Cloudflare Tunnel — remote access with no app to install.

`cloudflared` runs on this PC and dials OUT to Cloudflare, so:

  * no port is opened on the router and the PC keeps no inbound hole;
  * Cloudflare hands back a public https URL the phone opens in Safari, so there
    is nothing to install on the phone.

Two deliberate constraints:

1. **This module never downloads or installs cloudflared.** Fetching a binary
   from the internet and executing it is not something a desktop app should do
   behind the user's back. When it is missing, the UI tells the user the one
   command to run and this module simply reports "not installed".

2. **A public URL means the whole internet can reach the login page.** The app's
   own auth still applies — that depends on net.client_is_local(), which stops
   a tunnelled request from being mistaken for the trusted desktop UI — but the
   user is told plainly rather than left to assume it is private.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time

# cloudflared prints the assigned URL in a banner. The quick-tunnel hostname is
# always under trycloudflare.com.
URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

INSTALL_HINT = "winget install --id Cloudflare.cloudflared"

_CANDIDATES = (
    r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
    r"C:\Program Files\cloudflared\cloudflared.exe",
    "/usr/local/bin/cloudflared",
    "/usr/bin/cloudflared",
)


class TunnelError(RuntimeError):
    """The tunnel could not be started, with a reason worth showing the user."""


class Tunnel:
    """Owns the cloudflared child process. One per app."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._url = ""
        self._error = ""
        self._started_at = 0.0
        self._lock = threading.Lock()

    # -- discovery ---------------------------------------------------------
    @staticmethod
    def find_binary() -> str | None:
        exe = shutil.which("cloudflared") or shutil.which("cloudflared.exe")
        if exe:
            return exe
        for cand in _CANDIDATES:
            if os.path.exists(cand):
                return cand
        return None

    @property
    def installed(self) -> bool:
        return self.find_binary() is not None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- lifecycle ---------------------------------------------------------
    def start(self, port: int, *, timeout: float = 25.0) -> dict:
        """Start the tunnel and wait for Cloudflare to assign a URL."""
        with self._lock:
            if self.running:
                return self.status()
            exe = self.find_binary()
            if not exe:
                raise TunnelError(
                    "cloudflared isn't installed. Run this once in a terminal, "
                    f"then try again:  {INSTALL_HINT}"
                )

            self._url = ""
            self._error = ""
            # --url points at the local server; cloudflared does the dialling out.
            cmd = [exe, "tunnel", "--url", f"http://127.0.0.1:{int(port)}",
                   "--no-autoupdate"]
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, creationflags=creation,
                )
            except OSError as e:
                raise TunnelError(f"could not start cloudflared: {e}") from e

            self._started_at = time.time()
            reader = threading.Thread(target=self._read_output, daemon=True)
            reader.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._url:
                return self.status()
            if not self.running:
                raise TunnelError(self._error or "cloudflared exited before a URL was assigned")
            time.sleep(0.2)
        self.stop()
        raise TunnelError("Cloudflare did not return a URL in time. Check your internet connection.")

    def _read_output(self) -> None:
        """Watch cloudflared's log for the assigned URL (or a fatal error)."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                found = URL_RE.search(line)
                if found and not self._url:
                    self._url = found.group(0)
                low = line.lower()
                # Keep the last useful failure so start() can report something
                # better than "it exited".
                if "error" in low or "failed" in low:
                    self._error = line.strip()[:300]
        except Exception:
            pass

    def stop(self) -> dict:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
        self._url = ""
        self._started_at = 0.0
        return self.status()

    # -- reporting ---------------------------------------------------------
    def status(self) -> dict:
        return {
            "installed": self.installed,
            "installed_hint": INSTALL_HINT,
            "running": self.running,
            "url": self._url if self.running else "",
            "uptime": int(time.time() - self._started_at) if self.running and self._started_at else 0,
            "error": self._error if not self.running else "",
            # Permanent, and shown in the UI: a public URL is reachable by anyone.
            "warning": ("This publishes Easy Life on a public address. Anyone who "
                        "learns the URL can reach the sign-in page, so keep your "
                        "password strong. The URL changes every time you start it."),
        }


# The app's single tunnel.
tunnel = Tunnel()

# cloudflared is a child process and would otherwise keep the public URL open
# after Easy Life exits. Normal exit is enough: a hard kill leaves nothing that
# Easy Life can act on, and the URL dies with the tunnel anyway.
import atexit  # noqa: E402

atexit.register(lambda: tunnel.stop())
