"""Easy Life — desktop app entry point.

Runs the local server in a background thread and opens it in a NATIVE desktop
window (Microsoft Edge WebView2 via pywebview) instead of a browser tab.
No address bar, no Chrome — a real app window.

Falls back to the default browser automatically if pywebview/WebView2 is not
available, so the app always starts one way or another.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

from typing import Any

APP_TITLE = "Easy Life"
WINDOW_W, WINDOW_H = 1280, 840


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def _wait_for_server(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(host, port):
            return True
        time.sleep(0.15)
    return False


def _ensure_streams() -> None:
    """In a windowed (console-less) PyInstaller build, sys.stdout/stderr are
    None, which crashes uvicorn's logging (it calls .isatty()). Point them at a
    log file next to the app so nothing breaks and we still get diagnostics."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = None
    try:
        log_dir = Path(os.environ.get("PCRITUALS_DATA_DIR", str(Path.cwd())))
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "pcrituals.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _prepare_data_dir() -> None:
    """Data location.

    By default the app uses a single stable per-user folder (see
    config.default_data_dir) so the source build and the packaged .exe see the
    SAME rituals. Portable mode (data next to the exe) is opt-in via a
    `portable.txt` file next to the executable or PCRITUALS_PORTABLE=1.
    """
    root = (Path(sys.executable).parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent)
    os.environ.setdefault("PCRITUALS_INSTALL_DIR", str(root))

    portable = (root / "portable.txt").exists() or os.environ.get("PCRITUALS_PORTABLE") == "1"
    if portable:
        os.environ.setdefault("PCRITUALS_DATA_DIR", str(root / "data"))


def _quit_hook(app: Any, server: Any) -> None:
    """Let the UI stop the app on purpose.

    Once the window closes we deliberately keep serving (see main), so there has
    to be an explicit way out that isn't Task Manager. The endpoint behind this
    is loopback-only — a paired phone must not be able to shut the PC's app
    down.
    """
    def request_quit() -> bool:
        server.should_exit = True
        return True
    app.state.pcrituals_request_quit = request_quit


def _start_server() -> "tuple[Any, int]":
    """Start uvicorn in a background thread. Returns (server, port).

    If the port is already listening, another Easy Life is running: return
    (None, port) and let the caller just show the window against it. Starting a
    second copy from the Start Menu must open the window, not crash with
    "address already in use".
    """
    from pcrituals.config import load_config
    from pcrituals.api import create_app, start_https_listener
    import uvicorn

    cfg = load_config()
    probe_host = "127.0.0.1" if cfg.bind_loopback_only else cfg.host
    if _port_open("127.0.0.1", cfg.port):
        return None, cfg.port

    app = create_app()
    # The desktop window always talks to loopback; the LAN bind (0.0.0.0) is
    # still used so a paired phone can reach the same server.
    host = "127.0.0.1" if cfg.bind_loopback_only else cfg.host

    config = uvicorn.Config(app, host=host, port=cfg.port,
                            log_level="warning",
                            # Don't let uvicorn reconfigure logging — its default
                            # config calls sys.stdout.isatty(), which is None in a
                            # windowed (console=False) frozen build and crashes.
                            log_config=None)
    server = uvicorn.Server(config)
    _quit_hook(app, server)
    t = threading.Thread(target=server.run, name="pcrituals-server", daemon=True)
    t.start()
    if cfg.tunnel_autostart:
        # Start the tunnel in the background so a slow provider never delays
        # the window appearing. Best-effort: a failure just logs.
        def _autostart_tunnel() -> None:
            try:
                from pcrituals import funnel as ts_mod
                from pcrituals import tunnel as cf_mod
                mod = ts_mod if getattr(cfg, "tunnel_provider", "cloudflare") == "tailscale" else cf_mod
                if not mod.tunnel.running:
                    mod.tunnel.start(cfg.port)
            except Exception as e:  # noqa: BLE001 - autostart must never crash the app
                print(f"[pcrituals] tunnel autostart failed: {e}")
        threading.Thread(target=_autostart_tunnel, name="tunnel-autostart", daemon=True).start()
    if cfg.tls_enabled:
        # The window keeps using loopback http; this is the phone's secure
        # origin, which its camera scanner needs. The packaged build reaches
        # HTTPS through here, not through api.run().
        start_https_listener(cfg, app, log_config=None)

    # Watch OBS and push when the stream drops. Self-gates on streamer_mode +
    # notify_enabled, so starting it unconditionally is harmless.
    try:
        from pcrituals.notify import Notifier
        from pcrituals.streamwatch import StreamWatcher
        StreamWatcher(cfg, Notifier(cfg)).start()
    except Exception as e:  # noqa: BLE001 - must never block the app from opening
        print(f"[pcrituals] stream watcher failed to start: {e}")

    return server, cfg.port


def _stay_alive(server: Any) -> None:
    """Block until something asks the app to stop.

    The server *is* the app: a paired phone depends on it staying up. The server
    thread is a daemon, so returning here would kill it the instant the window
    closed — which silently broke remote control. Closing the window now just
    puts the app in the background; Settings has an explicit Quit.

    `server is None` means another Easy Life already owns the port and we were
    only a window onto it, so there is nothing to keep alive.
    """
    if server is None:
        return
    try:
        while not server.should_exit:
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True


def main() -> None:
    # --background is what the Windows "run at login" entry passes: serve only,
    # show nothing. Opening the app normally later just opens a window onto it.
    background = "--background" in sys.argv[1:]

    # Data dir first: the stream guard below writes its log file next to the
    # data, and in portable/frozen mode PCRITUALS_DATA_DIR is only set by
    # _prepare_data_dir(). Running them the other way sent the log to the
    # process's working directory (e.g. C:\Windows\System32, or a read-only
    # Program Files folder) exactly when the guard was needed most.
    _prepare_data_dir()
    _ensure_streams()
    server, port = _start_server()

    url = f"http://127.0.0.1:{port}"
    if not _wait_for_server("127.0.0.1", port):
        print(f"[Easy Life] Server did not start on {url}", file=sys.stderr)

    if background:
        _stay_alive(server)
        return

    # --- Preferred: native desktop window (WebView2) ---
    try:
        import webview  # pywebview
        window = webview.create_window(
            APP_TITLE, url,
            width=WINDOW_W, height=WINDOW_H,
            min_size=(880, 600),
            background_color="#07070c",
        )
        # gui='edgechromium' forces the WebView2 backend on Windows.
        try:
            webview.start(gui="edgechromium", debug=False)
        except TypeError:
            webview.start()  # older pywebview signature
        _stay_alive(server)
        return
    except Exception as e:  # noqa: BLE001 - any webview failure -> browser fallback
        print(f"[Easy Life] Native window unavailable ({e}); opening browser.")

    # --- Fallback: default browser ---
    import webbrowser
    webbrowser.open(url)
    _stay_alive(server)


if __name__ == "__main__":
    main()