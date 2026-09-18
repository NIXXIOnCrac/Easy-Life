"""Backup manager: snapshots the data directory (SQLite DB + secrets) to a
backups folder. Manual backups plus optional scheduled/rotation.
"""
from __future__ import annotations

import re
import shutil
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

from pcrituals.config import Config


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


class BackupManager:
    """Creates and restores full data snapshots (DB file + server secret)."""

    def __init__(self, config: Config):
        self.config = config
        config.ensure_dirs()
        self.backup_dir = config.data_dir / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _backup_path(self, backup_name: str) -> Path:
        """Resolve a caller-supplied backup folder name inside the backup dir.

        The API passes the name straight from the URL, so "../.." or an absolute
        path used to make restore()/delete_backup() read or delete arbitrary
        directories. Only a plain folder name directly under backups/ is allowed.
        """
        raw = (backup_name or "").strip()
        if not raw or raw in (".", "..") or ".." in Path(raw).parts:
            raise ValueError(f"invalid backup name: {backup_name!r}")
        if "/" in raw or "\\" in raw or Path(raw).is_absolute():
            raise ValueError(f"invalid backup name: {backup_name!r}")
        candidate = (self.backup_dir / raw).resolve()
        base = self.backup_dir.resolve()
        if candidate.parent != base:
            raise ValueError(f"invalid backup name: {backup_name!r}")
        return candidate

    def _snapshot_files(self) -> list[Path]:
        """Files that define the app's persistent state."""
        files = []
        db = self.config.db_file
        if db.exists():
            files.append(db)
        # Also catch the WAL file if present (might hold committed data not yet
        # checkpointed) and the server secret.
        for suffix in ("-wal", "-shm"):
            w = Path(str(db) + suffix)
            if w.exists():
                files.append(w)
        if self.config.secret_file.exists():
            files.append(self.config.secret_file)
        if self.config.auth_token_file.exists():
            files.append(self.config.auth_token_file)
        return files

    @staticmethod
    def _safe_label(label: str) -> str:
        """Return a label that can only name a folder *inside* backups/.

        create_backup() builds `backup_dir / f"{label}-{stamp}"`, and pathlib
        treats a rooted right-hand operand as absolute — so a label of "/tmp/x"
        or "../../x" wrote a copy of the database (with its users and sessions)
        outside the data directory, where list_backups() could not see it:
        unrestorable and undeletable. An authenticated caller, including a paired
        phone, chose that path.
        """
        cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", str(label or "").strip())
        cleaned = cleaned.replace("..", "_").strip("._ -")
        cleaned = cleaned[:64]
        return cleaned or "manual"

    def create_backup(self, label: str = "manual") -> dict:
        """Snapshot current state into backups/. Returns metadata."""
        label = self._safe_label(label)
        stamp = _now_iso()
        dest = self.backup_dir / f"{label}-{stamp}"
        dest.mkdir(parents=True, exist_ok=True)

        files = self._snapshot_files()
        manifest = {
            "label": label, "created": stamp,
            "files": [f.name for f in files],
            "count": len(files),
            "zlib_crc32": sum(zlib.crc32(f.read_bytes()) & 0xFFFFFFFF for f in files),
        }
        for f in files:
            dest.joinpath(f.name).write_bytes(f.read_bytes())
        dest.joinpath("manifest.json").write_text(
            __import__("json").dumps(manifest, indent=2))
        return {"backup": str(dest), "label": label, "files": len(files), "created": stamp}

    def list_backups(self) -> list[dict]:
        """Return metadata for all stored backups, newest first."""
        out = []
        for d in sorted(self.backup_dir.iterdir()):
            if not d.is_dir():
                continue
            man = d / "manifest.json"
            if man.exists():
                try:
                    data = __import__("json").loads(man.read_text())
                    out.append({"path": str(d), "name": d.name, **data})
                except Exception:
                    out.append({"path": str(d), "name": d.name})
        out.sort(key=lambda b: b.get("created", ""), reverse=True)
        return out

    def restore(self, backup_name: str) -> dict:
        """Restore a backup by its folder name. Overwrites current state."""
        src = self._backup_path(backup_name)
        if not src.is_dir() or not (src / "manifest.json").exists():
            raise ValueError(f"backup not found: {backup_name}")

        restored = []
        for f in src.iterdir():
            if f.name == "manifest.json" or not f.is_file():
                continue
            # Never write anywhere but the data dir, whatever the backup holds.
            if ".." in Path(f.name).parts or "/" in f.name or "\\" in f.name:
                raise ValueError(f"unsafe file in backup: {f.name}")
            # Map -wal/-shm back to the live DB path.
            if f.name == self.config.db_file.name + "-wal":
                target = Path(str(self.config.db_file) + "-wal")
            elif f.name == self.config.db_file.name + "-shm":
                target = Path(str(self.config.db_file) + "-shm")
            else:
                target = Path(self.config.db_file).parent / f.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.read_bytes())
            restored.append(target.name)
        return {"restored": len(restored), "backup": backup_name}

    def delete_backup(self, backup_name: str) -> bool:
        try:
            src = self._backup_path(backup_name)
        except ValueError:
            # A bogus name simply means "no such backup" to callers (the API
            # maps False to a 404); it must not surface as a 500.
            return False
        if not src.is_dir():
            return False
        shutil.rmtree(src)
        return True

    def prune(self, keep: int = 10) -> int:
        """Keep the newest `keep` backups, delete the rest. Returns deleted count."""
        backups = self.list_backups()
        # A negative slice index would delete the wrong end of the list.
        keep = max(0, int(keep))
        to_delete = backups[keep:]
        deleted = 0
        for b in to_delete:
            try:
                shutil.rmtree(Path(b["path"]))
                deleted += 1
            except Exception:
                pass
        return deleted