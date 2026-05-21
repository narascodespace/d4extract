"""Build the installable Blender extension zip for the D4Extract Importer.

Run with any Python 3.11+ (no Blender needed)::

    python make_dist.py

Produces ``dist/d4extract_blender-<version>.zip`` with
``blender_manifest.toml`` at the archive root — the layout Blender's
"Install from Disk..." expects for a 4.2+ extension.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parent
DIST_DIR = ADDON_DIR / "dist"
VERSION = "0.2.0"
ZIP_NAME = f"d4extract_blender-{VERSION}.zip"

# Never descend into these directories.
_EXCLUDE_DIRS = {"dist", "__pycache__", ".pytest_cache"}
# This build script is not part of the shipped extension.
_EXCLUDE_FILES = {"make_dist.py"}
# Allowlist of file types that make up the addon. An allowlist (rather
# than a denylist) keeps stray files — stale build zips, .blend backups,
# logs — out of the extension even if they land in this directory.
_INCLUDE_SUFFIXES = {".py", ".toml", ".md"}


def _iter_addon_files(root: Path):
    """Yield ``(absolute_path, archive_relative_path)`` for the addon."""
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        if any(part in _EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.name in _EXCLUDE_FILES:
            continue
        if path.suffix.lower() not in _INCLUDE_SUFFIXES:
            continue
        yield path, rel


def build() -> Path:
    """Write the extension zip and return its path."""
    if not (ADDON_DIR / "blender_manifest.toml").is_file():
        raise SystemExit("blender_manifest.toml missing — wrong directory?")

    DIST_DIR.mkdir(exist_ok=True)
    out = DIST_DIR / ZIP_NAME
    if out.exists():
        out.unlink()

    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, rel in _iter_addon_files(ADDON_DIR):
            zf.write(path, rel.as_posix())
            count += 1

    size_kb = out.stat().st_size / 1024
    print(f"Built {out}")
    print(f"  {count} files, {size_kb:.1f} KB")
    return out


if __name__ == "__main__":
    try:
        build()
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        raise
