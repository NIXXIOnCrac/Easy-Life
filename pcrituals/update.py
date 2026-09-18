"""In-app updater.

Checks a manifest URL for a newer version, and (on Windows) downloads and
applies the update over the install directory. The manifest is a small JSON
hosted anywhere reachable (e.g. GitHub Releases / Pages):

    {
      "version": "0.2.0",
      "url": "https://example.com/Easy-Life-0.2.0.zip",
      "notes": "What changed",
      "sha256": "optional hex digest"
    }

The check is safe to run on every app start — it's a single GET. Apply is
guarded: it refuses to run on non-Windows, refuses a downgrade, and verifies
the downloaded archive checksum when a sha256 is provided.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pcrituals import __version__ as CURRENT_VERSION


def _safe_component(text: str) -> str:
    """Reduce a manifest-supplied value to a safe path component.

    The version string lands in the download filename; a manifest containing
    "../../evil" would otherwise write outside the temp directory.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip()).strip("._-")
    return cleaned[:64] or "update"


def parse_version(v: str) -> tuple:
    """Turn '0.2.0-beta' -> (0, 2, 0, 'beta'). Safe for junk input."""
    v = v.strip().lstrip("vV")
    parts = v.split("-", 1)
    nums = []
    for seg in parts[0].split("."):
        try:
            nums.append(int(seg))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    prerelease = parts[1] if len(parts) > 1 else ""
    return (nums[0], nums[1], nums[2], prerelease)


def is_newer(candidate: str, current: str) -> bool:
    """True if candidate version > current version (stable beats prerelease)."""
    c = parse_version(candidate)
    cur = parse_version(current)
    if c[:3] != cur[:3]:
        return c[:3] > cur[:3]
    # Same numbers: non-prerelease > prerelease; else lexicographic.
    if bool(c[3]) != bool(cur[3]):
        return not c[3]  # candidate stable, current prerelease
    return c[3] > cur[3]


@dataclass
class UpdateInfo:
    version: str
    url: str = ""
    notes: str = ""
    sha256: Optional[str] = None
    downloaded_to: Optional[Path] = None


class UpdateError(RuntimeError):
    pass


class Updater:
    """Fetch + apply updates from a manifest URL."""

    def __init__(self, manifest_url: str | None, install_dir: Path | None = None,
                 current_version: str = CURRENT_VERSION) -> None:
        self.manifest_url = manifest_url or ""
        # Where to extract updates. Priority:
        #   1. explicit arg
        #   2. PCRITUALS_INSTALL_DIR env (set by the launcher)
        #   3. folder containing the running executable / script
        if install_dir is None:
            env_dir = os.environ.get("PCRITUALS_INSTALL_DIR")
            if env_dir:
                install_dir = Path(env_dir)
            elif getattr(sys, "frozen", False):
                install_dir = Path(sys.executable).resolve().parent
            else:
                # Source run: use the project root, NOT the venv's python folder.
                install_dir = Path(__file__).resolve().parent.parent
        self.install_dir = Path(install_dir)
        self.current_version = current_version

    def check(self, timeout: float = 6.0) -> UpdateInfo | None:
        """Return UpdateInfo if a newer version exists, else None. Never raises
        on network problems — returns None so a failed check is silent."""
        if not self.manifest_url:
            return None
        try:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(self.manifest_url, headers={"User-Agent": f"pcrituals/{self.current_version}"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return None

        # A manifest that is not a JSON object (null, a list, a bare number) has
        # no version field; this used to escape as AttributeError out of check(),
        # which the API turned into a 500 even though check() "never raises".
        if not isinstance(data, dict):
            return None
        version = str(data.get("version", "")).strip()
        if not version:
            return None
        if not is_newer(version, self.current_version):
            return None
        return UpdateInfo(
            version=version,
            url=str(data.get("url", "")).strip(),
            notes=str(data.get("notes", "")).strip(),
            sha256=(data.get("sha256") or None),
        )

    def download(self, info: UpdateInfo, timeout: float = 120.0) -> Path:
        """Download the update archive to a temp file. Returns its path."""
        if not info.url:
            raise UpdateError("update has no download URL")
        tmpdir = Path(tempfile.gettempdir()) / "pcrituals-update"
        tmpdir.mkdir(parents=True, exist_ok=True)
        dest = tmpdir / f"pcrituals-{_safe_component(info.version)}.zip"
        # Belt and braces: the destination must stay inside the temp dir.
        if dest.resolve().parent != tmpdir.resolve():
            raise UpdateError("invalid update version in manifest")
        ctx = ssl.create_default_context()
        try:
            req = urllib.request.Request(info.url, headers={"User-Agent": f"pcrituals-updater/{self.current_version}"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
        except Exception as e:
            raise UpdateError(f"download failed: {e}") from e

        if info.sha256:
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            if digest.lower() != info.sha256.lower():
                dest.unlink(missing_ok=True)
                raise UpdateError("downloaded archive failed checksum verification")
        info.downloaded_to = dest
        return dest

    def apply(self, info: UpdateInfo) -> bool:
        """Apply a downloaded update over the install dir. Windows-only. Returns
        True on success."""
        if sys.platform != "win32":
            raise UpdateError("updates can only be applied on Windows")
        if not info.downloaded_to or not info.downloaded_to.exists():
            raise UpdateError("update not downloaded yet")
        if not is_newer(info.version, self.current_version):
            raise UpdateError("refusing to install a downgrade")

        target = self.install_dir
        target.mkdir(parents=True, exist_ok=True)
        extract_to = target.parent / f".pcrituals-update-{_safe_component(info.version)}"
        if extract_to.exists():
            shutil.rmtree(extract_to)
        try:
            with zipfile.ZipFile(info.downloaded_to) as zf:
                # Avoid path traversal: reject absolute / parent entries.
                for n in zf.namelist():
                    if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                        raise UpdateError(f"unsafe path in update archive: {n}")
                zf.extractall(extract_to)
        except zipfile.BadZipFile as e:
            raise UpdateError(f"bad update archive: {e}") from e

        # Merge new files over the install dir (won't remove unrelated data/).
        for root, _, files in os.walk(extract_to):
            rel = Path(root).relative_to(extract_to)
            for fn in files:
                src = Path(root) / fn
                dst = target / rel / fn
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        shutil.rmtree(extract_to, ignore_errors=True)
        return True


def build_update_url(owner: str, repo: str, tag: str = "latest") -> str:
    """Convenience: point the updater at a GitHub Releases 'latest' JSON."""
    return f"https://api.github.com/repos/{owner}/{repo}/releases/{tag}"