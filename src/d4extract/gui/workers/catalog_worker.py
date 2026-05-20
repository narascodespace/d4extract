"""Background worker that scans the CASC archive for the model catalog."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThread, Signal

log = logging.getLogger(__name__)


# Glob handed to RustyDemonCLI.list_files. Phase 2 only browses model
# meta entries; later phases may broaden this.
DEFAULT_PATH_FILTER = "base/meta/Appearance/*"


def _cache_root() -> Path:
    """User cache dir for d4extract (created on first write)."""
    base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
    return Path(base) / "d4extract"


def _cache_file() -> Path:
    return _cache_root() / "catalog.json"


def _build_info_mtime(game_dir: Path) -> float | None:
    bi = game_dir / ".build.info"
    try:
        return bi.stat().st_mtime if bi.is_file() else None
    except OSError:
        return None


def _cache_key(game_dir: Path) -> dict[str, object]:
    """Identity of a cached catalog. Mismatch invalidates the cache."""
    return {
        "game_dir": str(game_dir),
        "build_info_mtime": _build_info_mtime(game_dir),
        "path_filter": DEFAULT_PATH_FILTER,
    }


def load_cached_catalog(game_dir: Path) -> list[str] | None:
    """Return a cached SNO path list, or None on miss / stale / corrupt."""
    f = _cache_file()
    if not f.is_file():
        return None
    try:
        payload = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read catalog cache %s: %s", f, exc)
        return None

    key = payload.get("key")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None
    if key != _cache_key(game_dir):
        return None
    # Be defensive about non-string entries — old caches or partial writes.
    return [str(e) for e in entries if isinstance(e, str)]


def save_cached_catalog(game_dir: Path, entries: list[str]) -> None:
    """Persist the catalog with the current cache key."""
    f = _cache_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": _cache_key(game_dir),
            "entries": entries,
        }
        f.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write catalog cache %s: %s", f, exc)


class CatalogWorker(QThread):
    """Scans the CASC archive for model SNO paths off the UI thread.

    Emits ``finished(list[str])`` with the SNO paths on success, or
    ``error(str)`` on failure. The signal payload is plain strings so
    the model can populate a ``QStringListModel`` directly.
    """

    finished = Signal(list)
    error = Signal(str)

    def __init__(self, game_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self._game_dir = Path(game_dir)

    def run(self) -> None:  # noqa: D401 — Qt API
        cached = load_cached_catalog(self._game_dir)
        if cached is not None:
            log.debug("Catalog cache hit: %d entries", len(cached))
            self.finished.emit(cached)
            return

        try:
            # Imported lazily so a missing rustydemon binary doesn't crash
            # the GUI before the user has even seen the main window.
            from d4extract.casc.rustydemon import (
                CASCExtractionError,
                RustyDemonCLI,
            )
        except Exception as exc:  # pragma: no cover — import-time only
            self.error.emit(f"Failed to import RustyDemonCLI: {exc}")
            return

        try:
            rd = RustyDemonCLI(self._game_dir)
            entries = rd.list_files(DEFAULT_PATH_FILTER)
        except CASCExtractionError as exc:
            self.error.emit(str(exc))
            return
        except Exception as exc:
            self.error.emit(f"Unexpected error scanning CASC: {exc}")
            return

        paths = [e.path for e in entries]
        save_cached_catalog(self._game_dir, paths)
        log.debug("Catalog cache miss: scanned %d entries", len(paths))
        self.finished.emit(paths)
