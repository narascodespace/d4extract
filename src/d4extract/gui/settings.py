"""Persistent settings wrapper around QSettings."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QSettings

log = logging.getLogger(__name__)


class AppSettings:
    """Typed accessors over ``QSettings("d4extract", "D4Export")``.

    Path-valued keys are validated on read: if the saved path no longer
    exists, the accessor returns ``None`` rather than a stale path.
    """

    KEY_GAME_DIR = "game_dir"
    KEY_LAST_EXPORT_DIR = "last_export_dir"
    KEY_D4DATA_PATH = "d4data_path"
    KEY_COORD_TRANSFORM = "export/coordinate_transform"
    KEY_FAVORITES = "favorites"
    KEY_TACT_KEYS = "tact_keys_path"
    KEY_TACT_BANNER_DISMISSED = "tact_banner_dismissed"
    KEY_D4DATA_BANNER_DISMISSED = "d4data_banner_dismissed"

    # Prefix for all checkable export options. Stored as ``export/<key>``
    # so they're easy to clear or migrate as a group.
    EXPORT_OPTION_PREFIX = "export/option/"

    def __init__(self) -> None:
        self._qs = QSettings("d4extract", "D4Export")

    def _read_path(self, key: str, *, must_be_dir: bool) -> Path | None:
        value = self._qs.value(key)
        if not value:
            return None
        path = Path(str(value))
        if must_be_dir:
            if not path.is_dir():
                return None
        else:
            if not path.exists():
                return None
        return path

    # ------------------------------------------------------------------
    # Game / export directories
    # ------------------------------------------------------------------

    def game_dir(self) -> Path | None:
        return self._read_path(self.KEY_GAME_DIR, must_be_dir=True)

    def set_game_dir(self, path: Path) -> None:
        self._qs.setValue(self.KEY_GAME_DIR, str(Path(path)))
        self._qs.sync()

    def last_export_dir(self) -> Path | None:
        return self._read_path(self.KEY_LAST_EXPORT_DIR, must_be_dir=True)

    def set_last_export_dir(self, path: Path) -> None:
        self._qs.setValue(self.KEY_LAST_EXPORT_DIR, str(Path(path)))
        self._qs.sync()

    def raw_game_dir(self) -> Path | None:
        """Saved game_dir without existence validation (for 'Last opened:')."""
        value = self._qs.value(self.KEY_GAME_DIR)
        if not value:
            return None
        return Path(str(value))

    # ------------------------------------------------------------------
    # D4Data (extracted JSON game data) path
    # ------------------------------------------------------------------

    def d4data_path(self) -> Path | None:
        """Path to the d4data ``json/`` directory, or ``None`` if unset/stale."""
        return self._read_path(self.KEY_D4DATA_PATH, must_be_dir=True)

    def set_d4data_path(self, path: Path) -> None:
        self._qs.setValue(self.KEY_D4DATA_PATH, str(Path(path)))
        self._qs.sync()

    def d4data_banner_dismissed(self) -> bool:
        return bool(
            self._qs.value(
                self.KEY_D4DATA_BANNER_DISMISSED, False, type=bool,
            )
        )

    def set_d4data_banner_dismissed(self, value: bool) -> None:
        self._qs.setValue(self.KEY_D4DATA_BANNER_DISMISSED, bool(value))
        self._qs.sync()

    # ------------------------------------------------------------------
    # TACT keys (user-supplied, never bundled — see d4extract.config)
    # ------------------------------------------------------------------

    def tact_keys_path(self) -> Path | None:
        """Path to the cached TACT keys file, or ``None`` if not loaded.

        Mirrors :func:`d4extract.config.get_tact_keys_path` but goes
        through the same QSettings instance the rest of this class
        uses; both reads return the same value.
        """
        return self._read_path(self.KEY_TACT_KEYS, must_be_dir=False)

    def set_tact_keys_path(self, path: Path) -> None:
        """Persist *path* as the cached TACT keys location.

        Note: this only updates QSettings. To validate, copy, and store
        a user-chosen file in one step, call
        :func:`d4extract.config.set_tact_keys_path` instead — that's
        the canonical entry point used by the GUI menu action.
        """
        self._qs.setValue(self.KEY_TACT_KEYS, str(Path(path)))
        self._qs.sync()

    def clear_tact_keys_path(self) -> None:
        self._qs.remove(self.KEY_TACT_KEYS)
        self._qs.sync()

    def tact_banner_dismissed(self) -> bool:
        return bool(
            self._qs.value(self.KEY_TACT_BANNER_DISMISSED, False, type=bool)
        )

    def set_tact_banner_dismissed(self, value: bool) -> None:
        self._qs.setValue(self.KEY_TACT_BANNER_DISMISSED, bool(value))
        self._qs.sync()

    # ------------------------------------------------------------------
    # Export option toggles
    # ------------------------------------------------------------------

    def export_option(self, key: str, default: bool) -> bool:
        """Read a boolean export option. Falls back to ``default`` if unset."""
        value = self._qs.value(self.EXPORT_OPTION_PREFIX + key)
        if value is None:
            return default
        # QSettings round-trips bools through strings on some backends.
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value)

    def set_export_option(self, key: str, value: bool) -> None:
        self._qs.setValue(self.EXPORT_OPTION_PREFIX + key, bool(value))
        self._qs.sync()

    # ------------------------------------------------------------------
    # Favorites (starred SNO paths)
    # ------------------------------------------------------------------

    def favorites(self) -> set[str]:
        """Return the saved favorite SNO paths.

        Stored as a JSON list under :attr:`KEY_FAVORITES`. Returns the
        empty set on missing/malformed data so a corrupted entry never
        prevents the GUI from coming up — the user can re-star the
        models they care about.
        """
        raw = self._qs.value(self.KEY_FAVORITES)
        if not raw:
            return set()
        try:
            decoded = json.loads(str(raw))
        except (TypeError, ValueError) as exc:
            log.warning("Could not decode favorites payload: %s", exc)
            return set()
        if not isinstance(decoded, list):
            return set()
        # Defensive str() — older caches or partial writes could land
        # non-string entries; coerce and drop empties.
        return {str(p) for p in decoded if p}

    def set_favorites(self, paths: set[str]) -> None:
        """Persist the favorite SNO paths as a JSON list."""
        # Sorted so the on-disk payload is stable and diff-friendly when
        # users (or tooling) inspect the QSettings backing store.
        payload = json.dumps(sorted(paths))
        self._qs.setValue(self.KEY_FAVORITES, payload)
        self._qs.sync()

    def coordinate_transform(self, default: str = "z_up_to_y_up") -> str:
        value = self._qs.value(self.KEY_COORD_TRANSFORM)
        if not value:
            return default
        return str(value)

    def set_coordinate_transform(self, value: str) -> None:
        self._qs.setValue(self.KEY_COORD_TRANSFORM, value)
        self._qs.sync()
