"""SQLite persistence for Rituals and execution history."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from pcrituals.config import Config
from pcrituals.models import HistoryEntry, Ritual, RitualListItem, RitualStatus


def _dt(d: datetime) -> str:
    return d.isoformat() if d else ""


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


class Store:
    """SQLite-backed store for rituals and history."""

    def __init__(self, config: Config) -> None:
        config.ensure_dirs()
        from pcrituals.db import open_db
        self._conn = open_db(config.db_file)
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rituals (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY,
                ritual_id TEXT NOT NULL,
                data TEXT NOT NULL,
                started_at TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_history_started ON history(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_history_ritual ON history(ritual_id);
            CREATE TABLE IF NOT EXISTS deck (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self._conn.commit()

    # ---- rituals -----------------------------------------------------------
    def save_ritual(self, ritual: Ritual) -> Ritual:
        ritual.set_updated()
        # Preserve existing position if this ritual already exists.
        existing = self._conn.execute(
            "SELECT position FROM rituals WHERE id=?", (ritual.id,)).fetchone()
        position = existing["position"] if existing else self._next_position()
        self._conn.execute(
            "INSERT OR REPLACE INTO rituals (id, data, updated_at, position) VALUES (?,?,?,?)",
            (ritual.id, ritual.model_dump_json(), _dt(ritual.updated_at), position),
        )
        self._conn.commit()
        return ritual

    def _next_position(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(position), -1) p FROM rituals").fetchone()
        return row["p"] + 1

    def get_ritual(self, ritual_id: str) -> Optional[Ritual]:
        row = self._conn.execute(
            "SELECT data FROM rituals WHERE id=?", (ritual_id,)).fetchone()
        return Ritual.model_validate_json(row["data"]) if row else None

    def list_rituals(self) -> list[Ritual]:
        rows = self._conn.execute(
            "SELECT data FROM rituals ORDER BY position ASC, updated_at DESC").fetchall()
        return [Ritual.model_validate_json(r["data"]) for r in rows]

    def list_summaries(self) -> list[RitualListItem]:
        rituals = self.list_rituals()
        items = []
        for r in rituals:
            items.append(RitualListItem(
                id=r.id, name=r.name, description=r.description,
                status=RitualStatus.DRAFT, action_count=len(r.actions),
                tags=r.tags, updated_at=r.updated_at,
            ))
        return items

    def reorder_rituals(self, ordered_ids: list[str]) -> None:
        """Persist a manual ordering of rituals."""
        for i, rid in enumerate(ordered_ids):
            self._conn.execute(
                "UPDATE rituals SET position=? WHERE id=?", (i, rid))
        self._conn.commit()

    def delete_ritual(self, ritual_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM rituals WHERE id=?", (ritual_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def count_rituals(self) -> int:
        return self._conn.execute("SELECT COUNT(*) c FROM rituals").fetchone()["c"]

    # ---- history -----------------------------------------------------------
    def append_history(self, entry: HistoryEntry) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO history (id, ritual_id, data, started_at, status) "
            "VALUES (?,?,?,?,?)",
            (entry.id, entry.ritual_id, entry.model_dump_json(),
             _dt(entry.started_at), entry.status.value),
        )
        self._conn.commit()

    def list_history(self, limit: int = 100, ritual_id: Optional[str] = None) -> list[HistoryEntry]:
        sql = "SELECT data FROM history"
        params: list = []
        if ritual_id:
            sql += " WHERE ritual_id=?"
            params.append(ritual_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [HistoryEntry.model_validate_json(r["data"]) for r in rows]

    def history_summary(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM history").fetchone()
        successes = self._conn.execute(
            "SELECT COUNT(*) c FROM history WHERE status IN ('completed','stopped')").fetchone()["c"]
        return {"total_runs": row["c"], "successes": row["c"] - successes,
                "total": row["c"]}

    def clear_history(self) -> None:
        self._conn.execute("DELETE FROM history")
        self._conn.commit()

    # ---- deck (Stream Deck buttons) ----------------------------------------
    def save_deck_button(self, btn: "DeckButton") -> "DeckButton":
        from pcrituals.models import DeckButton as _DB
        existing = self._conn.execute(
            "SELECT position FROM deck WHERE id=?", (btn.id,)).fetchone()
        if existing:
            position = existing["position"]
        else:
            row = self._conn.execute("SELECT COALESCE(MAX(position), -1) p FROM deck").fetchone()
            position = row["p"] + 1
        self._conn.execute(
            "INSERT OR REPLACE INTO deck (id, data, position) VALUES (?,?,?)",
            (btn.id, btn.model_dump_json(), position))
        self._conn.commit()
        return btn

    def list_deck(self) -> list:
        from pcrituals.models import DeckButton
        rows = self._conn.execute(
            "SELECT data FROM deck ORDER BY position ASC").fetchall()
        return [DeckButton.model_validate_json(r["data"]) for r in rows]

    def get_deck_button(self, bid: str):
        from pcrituals.models import DeckButton
        row = self._conn.execute("SELECT data FROM deck WHERE id=?", (bid,)).fetchone()
        return DeckButton.model_validate_json(row["data"]) if row else None

    def delete_deck_button(self, bid: str) -> bool:
        cur = self._conn.execute("DELETE FROM deck WHERE id=?", (bid,))
        self._conn.commit()
        return cur.rowcount > 0

    def reorder_deck(self, ordered_ids: list[str]) -> None:
        for i, bid in enumerate(ordered_ids):
            self._conn.execute("UPDATE deck SET position=? WHERE id=?", (i, bid))
        self._conn.commit()