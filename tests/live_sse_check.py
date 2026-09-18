"""Live-event (SSE) verification.

'It's not renewing data' is the user-visible symptom of the event stream not
reaching the UI. This talks to a running server, subscribes to /api/events,
triggers a real ritual run and asserts progress events actually arrive.

Run against a live server:
    PCRITUALS_SSE_URL=http://127.0.0.1:8765 .venv/bin/python tests/live_sse_check.py
Exits non-zero on failure.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("PCRITUALS_SSE_URL", "http://127.0.0.1:8765").rstrip("/")


def _req(method: str, path: str, body=None, token: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{BASE}/api{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read() or b"null")


def main() -> int:
    # Sign in (or create the account if this is a fresh install).
    status, st = _req("GET", "/auth/status")
    if st.get("needs_setup"):
        _, res = _req("POST", "/auth/setup", {"username": "sseuser", "password": "secret123"})
        user, pw = "sseuser", "secret123"
    else:
        user, pw = os.environ.get("PCRITUALS_USER", "sseuser"), os.environ.get("PCRITUALS_PASS", "secret123")
        try:
            _, res = _req("POST", "/auth/login", {"username": user, "password": pw})
        except urllib.error.HTTPError as e:
            print(f"FAIL: could not sign in ({e.code})")
            return 1
    token = res["token"]
    print("signed in as", res.get("username"))

    events: list[dict] = []
    ready = threading.Event()
    stop = threading.Event()

    def listen() -> None:
        url = f"{BASE}/api/events?token={token}"
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                ctype = r.headers.get("Content-Type", "")
                if "text/event-stream" not in ctype:
                    print("FAIL: wrong content-type:", ctype)
                    return
                ready.set()
                buf = ""
                while not stop.is_set():
                    chunk = r.read(1)
                    if not chunk:
                        break
                    buf += chunk.decode("utf-8", "replace")
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        for line in frame.splitlines():
                            if line.startswith("data:"):
                                raw = line[5:].strip()
                                if raw:
                                    events.append(json.loads(raw))
        except Exception as e:  # noqa: BLE001
            if not ready.is_set():
                print("FAIL: could not open the event stream:", e)

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    if not ready.wait(10):
        print("FAIL: event stream never opened")
        return 1
    print("PASS: event stream open")

    # A ritual that takes long enough to emit progress.
    _, ritual = _req("POST", "/rituals", {
        "name": "SSE check",
        "actions": [
            {"type": "delay", "params": {"seconds": 1}, "label": "one"},
            {"type": "delay", "params": {"seconds": 1}, "label": "two"},
        ],
    }, token)
    rid = ritual["id"]
    _req("POST", f"/rituals/{rid}/run", {}, token)

    # Wait for progress events to show up.
    deadline = time.time() + 20
    while time.time() < deadline:
        if any(e.get("type") == "finished" for e in events):
            break
        time.sleep(0.3)

    kinds = [e.get("type") for e in events]
    print("events received:", kinds[:12], "..." if len(kinds) > 12 else "")
    ok = True
    for expected in ("started", "finished"):
        if expected not in kinds:
            print(f"FAIL: never received a '{expected}' event")
            ok = False
    steps = [k for k in kinds if k in ("step_start", "step_done")]
    if not steps:
        print("FAIL: no per-step progress events")
        ok = False
    else:
        print(f"PASS: {len(steps)} progress events")

    stop.set()
    _req("DELETE", f"/rituals/{rid}", None, token)
    print("\nSSE CHECK", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
