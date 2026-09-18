"""Write the update manifest that the in-app updater downloads.

WHY this is a script instead of three lines inside the release workflow: the
field names have to match EXACTLY what `pcrituals/update.py` reads when it
checks for a new version (`version`, `url`, `notes`, `sha256`). If the workflow
ever wrote `sha` instead of `sha256`, the app would silently stop verifying
downloads and the mistake would only surface as a broken update for the user.
Keeping the manifest shape in one testable place - tests/test_packaging.py
asserts these keys against the real updater code - makes that impossible.

The generated file is what the user pastes into Settings -> Updates. The release
workflow also publishes it as a release asset called `update.json`, so this
stable URL always points at the newest release:

    https://github.com/<owner>/<repo>/releases/latest/download/update.json

Usage (from the project root):

    python scripts/make_update_manifest.py \
        --version 0.2.0 \
        --url "https://github.com/me/pc-rituals/releases/download/v0.2.0/Easy-Life-0.2.0-update.zip" \
        --sha256-file "dist/Easy-Life-0.2.0-update.zip" \
        --notes "Faster starts, fixed the close-app step." \
        --out dist/update.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Optional, Sequence

# The exact keys pcrituals/update.py's UpdateInfo/check() consume. Keep the
# order stable so the published file diffs cleanly between releases.
MANIFEST_FIELDS = ("version", "url", "notes", "sha256")

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_NOTES_CHARS = 300


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """Hex sha256 of a file, streamed so a 150 MB build does not sit in RAM."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_notes(text: str) -> str:
    """One short, plain line for the in-app 'update available' banner."""
    flat = " ".join((text or "").split())
    if len(flat) > MAX_NOTES_CHARS:
        flat = flat[: MAX_NOTES_CHARS - 1].rstrip() + "\u2026"
    return flat


def build_manifest(version: str, url: str, notes: str = "",
                   sha256: Optional[str] = None) -> dict:
    """Return the manifest dict the app expects, with every field present.

    All four keys are always emitted: the updater treats a missing `sha256` as
    'do not verify this download', so an accidental omission would quietly
    downgrade security rather than fail loudly.
    """
    version = (version or "").strip().lstrip("vV")
    url = (url or "").strip()
    if not version:
        raise ValueError("version is required (e.g. 0.2.0)")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"url must be an http(s) link, got: {url!r}")
    if sha256 is not None and not _SHA256_RE.match(sha256.strip()):
        raise ValueError("sha256 must be 64 hex characters")
    return {
        "version": version,
        "url": url,
        "notes": clean_notes(notes),
        "sha256": sha256.strip().lower() if sha256 else None,
    }


def write_manifest(path: str | Path, manifest: dict) -> Path:
    """Write the manifest as UTF-8 JSON with a trailing newline."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    missing = [k for k in MANIFEST_FIELDS if k not in manifest]
    if missing:
        raise ValueError(f"manifest is missing required field(s): {missing}")
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Write the Easy Life update manifest.")
    parser.add_argument("--version", required=True, help="version being released, e.g. 0.2.0")
    parser.add_argument("--url", required=True, help="download URL of the update .zip")
    parser.add_argument("--sha256", default=None, help="sha256 hex digest of the zip")
    parser.add_argument("--sha256-file", default=None,
                        help="compute the sha256 from this file instead (recommended)")
    parser.add_argument("--notes", default="", help="short note shown in the app")
    parser.add_argument("--notes-file", default=None,
                        help="read the note text from this file (e.g. RELEASE_NOTES.md)")
    parser.add_argument("--out", required=True, help="where to write the manifest JSON")
    args = parser.parse_args(argv)

    if args.sha256_file and args.sha256:
        print("[manifest] give either --sha256 or --sha256-file, not both", file=sys.stderr)
        return 2

    digest = args.sha256
    if args.sha256_file:
        archive = Path(args.sha256_file)
        if not archive.exists():
            print(f"[manifest] no such file to hash: {archive}", file=sys.stderr)
            return 2
        digest = sha256_file(archive)
        print(f"[manifest] sha256({archive.name}) = {digest}")

    notes = args.notes
    if args.notes_file:
        notes = Path(args.notes_file).read_text(encoding="utf-8", errors="replace")

    try:
        manifest = build_manifest(args.version, args.url, notes, digest)
        out = write_manifest(args.out, manifest)
    except ValueError as exc:
        print(f"[manifest] {exc}", file=sys.stderr)
        return 2

    print(f"[manifest] wrote {out}")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
