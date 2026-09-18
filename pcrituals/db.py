"""Thread-safe SQLite access.

Why this exists
---------------
SQLite connection objects are **not** safe to use concurrently from multiple
threads. FastAPI runs synchronous endpoints in a thread pool, and the UI fires
many requests at once (status, rituals, history, devices, deck...), so several
threads ended up using the same connection simultaneously.

The result was non-deterministic breakage:
  - `SELECT COUNT(*)` returning None  -> TypeError -> 500s
  - sessions appearing invalid        -> spurious "login required" 401s
  - data silently not refreshing
  - device revoke/pairing failing intermittently

`LockedConnection` serialises every statement and materialises the rows while
the lock is held, so existing call sites (`conn.execute(...).fetchone()`) stay
correct without change.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional


class _Result:
    """A materialised result set that mimics the cursor API we use."""

    __slots__ = ("_rows", "rowcount", "lastrowid", "description")

    def __init__(self, rows: list, rowcount: int, lastrowid: Optional[int],
                 description: Any = None) -> None:
        self._rows = rows
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.description = description

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class LockedConnection:
    """A sqlite3 connection whose statements are serialised across threads."""

    def __init__(self, path: Path | str, *, timeout: float = 30.0) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=timeout)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL lets readers and a writer coexist; busy_timeout avoids
            # "database is locked" when several connections touch the file.
            try:
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute("PRAGMA busy_timeout=30000;")
                self._conn.execute("PRAGMA synchronous=NORMAL;")
            except sqlite3.Error:
                pass

    # --- cursor-ish API ----------------------------------------------------
    def execute(self, sql: str, params: Iterable = ()) -> _Result:
        with self._lock:
            try:
                cur = self._conn.execute(sql, tuple(params))
                if cur.description is not None:
                    rows = cur.fetchall()
                else:
                    rows = []
                return _Result(rows, cur.rowcount, cur.lastrowid, cur.description)
            except sqlite3.Error:
                # A statement that fails part-way can leave an open write
                # transaction, which then blocks every other connection to the
                # same file (including the one that answers /auth/status) until
                # it times out with "database is locked". Roll back before
                # propagating so a single bad write cannot take out the app.
                self._safe_rollback()
                raise

    def executemany(self, sql: str, seq) -> _Result:
        with self._lock:
            try:
                cur = self._conn.executemany(sql, seq)
                return _Result([], cur.rowcount, cur.lastrowid, cur.description)
            except sqlite3.Error:
                self._safe_rollback()
                raise

    def _safe_rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error:
            pass

    def executescript(self, script: str) -> None:
        with self._lock:
            self._conn.executescript(script)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    @property
    def raw(self):
        return self._conn


def open_db(path: Path | str) -> LockedConnection:
    return LockedConnection(path)
