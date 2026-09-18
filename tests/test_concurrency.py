"""Concurrency regression test.

The app fires many requests at once (the UI refreshes ~10 endpoints in
parallel). Before the thread-safe DB layer, sharing one SQLite connection
across FastAPI's thread pool corrupted queries — producing random 500s and
spurious "login required" 401s. This test hammers the API concurrently and
asserts nothing fails.
"""
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _client(tmp):
    os.environ["PCRITUALS_DATA_DIR"] = str(tmp)
    from fastapi.testclient import TestClient
    from pcrituals.api import create_app
    return TestClient(create_app())


def test_lock_connection_materialises_rows(tmp_path):
    from pcrituals.db import open_db
    c = open_db(tmp_path / "t.db")
    c.execute("CREATE TABLE x (n INTEGER)")
    assert c.execute("SELECT COUNT(*) c FROM x").fetchone()["c"] == 0
    c.execute("INSERT INTO x (n) VALUES (1)")
    c.commit()
    assert c.execute("SELECT COUNT(*) c FROM x").fetchone()["c"] == 1
    assert c.execute("SELECT COUNT(*) c FROM x").fetchall()[0]["c"] == 1
    c.close()


def test_concurrent_requests_never_fail(tmp_path):
    c = _client(tmp_path / "conc")
    # Create the account, then hit the API hard from many threads.
    tok = c.post("/api/auth/setup", json={"username": "alex", "password": "secret123"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}

    paths = ["/api/status", "/api/rituals", "/api/history?limit=50", "/api/devices",
             "/api/pair/state", "/api/backups", "/api/deck", "/api/auth/status",
             "/api/settings", "/api/integrations", "/api/media/now", "/api/spotify/status"]

    def hit(i):
        p = paths[i % len(paths)]
        r = c.get(p, headers=h)
        return (p, r.status_code)

    results = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for out in ex.map(hit, range(200)):
            results.append(out)

    bad = [(p, s) for p, s in results if s != 200]
    assert not bad, f"concurrent requests failed: {bad[:10]}"


def test_concurrent_login_and_reads(tmp_path):
    """Repeated logins while reading — the exact pattern that broke before."""
    c = _client(tmp_path / "conc2")
    c.post("/api/auth/setup", json={"username": "alex", "password": "secret123"})

    def work(i):
        if i % 5 == 0:
            r = c.post("/api/auth/login", json={"username": "alex", "password": "secret123"})
            return r.status_code
        tok = c.post("/api/auth/login", json={"username": "alex", "password": "secret123"}).json()["token"]
        return c.get("/api/rituals", headers={"Authorization": f"Bearer {tok}"}).status_code

    with ThreadPoolExecutor(max_workers=12) as ex:
        codes = list(ex.map(work, range(120)))
    assert all(s == 200 for s in codes), f"failures: {[s for s in codes if s != 200][:10]}"


def test_has_users_is_stable_under_load(tmp_path):
    """has_users() returned None-derived errors under concurrency — the exact
    500 seen in production. It must be solid now."""
    c = _client(tmp_path / "conc3")
    c.post("/api/auth/setup", json={"username": "alex", "password": "secret123"})

    def check(_):
        return c.get("/api/auth/status").status_code

    with ThreadPoolExecutor(max_workers=20) as ex:
        codes = list(ex.map(check, range(300)))
    assert set(codes) == {200}, f"auth/status codes: {set(codes)}"