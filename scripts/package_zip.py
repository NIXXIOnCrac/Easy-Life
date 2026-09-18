"""Package the project into a clean, downloadable Windows installer zip.

Excludes the virtualenv, git history, build artifacts, and runtime data so the
zip is small and self-contained. On Windows you extract it and double-click
setup_windows.bat.
"""
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # project root
OUT = ROOT / "dist"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    zip_path = OUT / "Easy-Life-Windows.zip"
    if zip_path.exists():
        zip_path.unlink()

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        base_str = str(ROOT)
        for cur, dirs, files in os.walk(ROOT):
            curp = Path(cur)
            # Prune excluded dirs in-place so os.walk doesn't descend.
            dirs[:] = [d for d in dirs if not _is_excluded_dir(curp / d)]
            for fn in files:
                fpath = curp / fn
                if _is_excluded(curp / fn):
                    continue
                arc = os.path.relpath(str(fpath), base_str).replace(os.sep, "/")
                zf.write(str(fpath), arc)
                count += 1

    size_mb = os.path.getsize(zip_path) / 1_048_576
    print(f"Packed {count} files -> {zip_path}  ({size_mb:.2f} MB)")


def _is_excluded_dir(d: Path) -> bool:
    return d.name in {".venv", "venv", ".git", "dist", "build", "node_modules",
                      "__pycache__", ".pytest_cache", "data", "backups"}


def _is_excluded(f: Path) -> bool:
    if f.name.endswith((".pyc", ".pyo")):
        return True
    if f.name in {".DS_Store", "models_dev_cache.json", ".env"}:
        return True
    return False


if __name__ == "__main__":
    main()