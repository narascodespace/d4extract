"""Data model for browsing the CASC file tree."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CASCEntry:
    """A single file or directory entry in the CASC archive."""

    path: str
    size: int
    is_directory: bool = False

    @property
    def name(self) -> str:
        """Filename without directory path."""
        return self.path.rsplit("/", 1)[-1] if "/" in self.path else self.path

    @property
    def extension(self) -> str:
        """File extension including the dot, or empty string."""
        name = self.name
        dot = name.rfind(".")
        return name[dot:] if dot != -1 else ""


@dataclass
class CASCArchive:
    """Browsable snapshot of a CASC archive's file listing."""

    game_dir: Path
    product: str  # "fenris" for D4
    entries: list[CASCEntry] = field(default_factory=list)

    def filter(self, glob: str) -> list[CASCEntry]:
        """Return entries whose path matches a glob pattern.

        Args:
            glob: A glob/fnmatch pattern (e.g. ``base/meta/Appearance/*.app``).

        Returns:
            Matching entries, preserving order.
        """
        return [e for e in self.entries if fnmatch.fnmatch(e.path, glob)]

    def appearances(self) -> list[CASCEntry]:
        """Shortcut: return all .app files under Appearance directories."""
        return [
            e
            for e in self.entries
            if "/Appearance/" in e.path and e.path.endswith(".app")
        ]

    def meta_appearances(self) -> list[CASCEntry]:
        """Return .app files under base/meta/Appearance/."""
        return [
            e
            for e in self.entries
            if e.path.startswith("base/meta/Appearance/") and e.path.endswith(".app")
        ]

    def payload_appearances(self) -> list[CASCEntry]:
        """Return .app files under base/payload/Appearance/."""
        return [
            e
            for e in self.entries
            if e.path.startswith("base/payload/Appearance/")
            and e.path.endswith(".app")
        ]
