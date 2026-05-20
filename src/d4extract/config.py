"""User-level configuration shared by the GUI and CLI.

Currently the only state managed here is the path to the
user-supplied TACT key file used to decrypt encrypted CASC content.
The functions here are the single source of truth for that path —
both the GUI menu actions and the CLI ``--tact-keys`` fallback
resolve it through this module so behavior stays consistent.

Design notes:

- QSettings is the persistent store (so the GUI and CLI see the same
  value) but importing PySide6 has a noticeable startup cost. CLI-only
  workflows that never load a key file should not pay that cost, so
  the QSettings import is deferred inside the function bodies that
  need it.
- TACT keys cannot be redistributed (DMCA risk), so the workflow is:
  the user picks a key file from disk; we copy it into the per-user
  app data directory; we store the path of the *copy* in QSettings.
  The original may be moved or deleted afterwards without affecting
  subsequent extractions.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import platformdirs

log = logging.getLogger(__name__)

_APP_NAME = "d4extract"
_APP_AUTHOR = "D4Export"
_QSETTINGS_KEY = "tact_keys_path"
_CACHED_FILE_NAME = "tact_keys.txt"

# wowdev TACT key format: 16 hex chars (u64 keyid), separator (space or
# semicolon), 32 hex chars (16-byte Salsa20 key). Matches the parser at
# rustydemon-lib/src/key_service.rs::load_keys_from_str.
_LINE = re.compile(r"^([0-9A-Fa-f]{16})[;\s]+([0-9A-Fa-f]{32})\s*$")


def _user_data_dir() -> Path:
    """Return the per-user data directory, creating it if needed."""
    path = Path(platformdirs.user_data_dir(_APP_NAME, _APP_AUTHOR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cached_path() -> Path:
    """Where the copied TACT keys file lives on disk."""
    return _user_data_dir() / _CACHED_FILE_NAME


def count_valid_keys(path: Path) -> int:
    """Return the number of well-formed (KEYID, KEY) pairs in *path*.

    Mirrors the lenient parsing in rustydemon-lib's
    ``load_keys_from_str``: blank lines and ``#`` comments are skipped,
    accepted separators are space or semicolon, and any line that
    doesn't match the strict 16+32 hex shape is silently ignored. The
    count is used both as a validation gate (a zero count rejects the
    file as malformed) and for the status indicator in the GUI.
    """
    count = 0
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("count_valid_keys: cannot open %s: %s", path, exc)
        return 0
    with fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if _LINE.match(stripped):
                count += 1
    return count


def get_tact_keys_path() -> Path | None:
    """Return the cached TACT keys file path, or *None* if not loaded.

    Returns *None* both when no entry has been persisted and when the
    persisted entry points at a file that no longer exists (e.g. the
    user wiped their app data directory). Callers should treat the
    *None* case as "no keys available" — encrypted CASC content will
    silently be skipped during extraction.
    """
    # Lazy import: keep PySide6 out of CLI startup when no keys are
    # configured. This function is called by every CLI invocation via
    # ``RustyDemonCLI.find_tact_keys``, so the import cost matters.
    from PySide6.QtCore import QSettings

    qs = QSettings(_APP_NAME, "D4Export")
    raw = qs.value(_QSETTINGS_KEY)
    if not raw:
        return None
    path = Path(str(raw))
    if not path.is_file():
        log.debug("Cached TACT keys path no longer exists: %s", path)
        return None
    return path


def set_tact_keys_path(src: Path) -> Path:
    """Validate *src*, copy it into the user data dir, and persist the copy.

    The source file is parsed with :func:`count_valid_keys`; a file
    with zero valid lines is rejected so the caller can surface a
    clear error to the user rather than silently storing a file the
    extractor will treat as keyless. On success the file is copied to
    a stable per-user location (the original is left untouched), the
    cached path is written to QSettings, and that path is returned.
    """
    src = Path(src)
    if not src.is_file():
        raise ValueError(f"not a file: {src}")
    n = count_valid_keys(src)
    if n == 0:
        raise ValueError(
            "no valid TACT key lines found (expected KEYID KEY or "
            "KEYID;KEY with 16+32 hex chars per line)"
        )

    dst = _cached_path()
    # shutil.copy2 preserves mtime; that gives the user a way to tell
    # at a glance how fresh the file is when troubleshooting.
    shutil.copy2(src, dst)
    log.info("TACT keys: cached %d keys to %s (from %s)", n, dst, src)

    from PySide6.QtCore import QSettings

    qs = QSettings(_APP_NAME, "D4Export")
    qs.setValue(_QSETTINGS_KEY, str(dst))
    qs.sync()
    return dst


def clear_tact_keys() -> None:
    """Forget the loaded TACT keys: delete the cached copy and clear QSettings.

    Idempotent — calling this when no keys are loaded is a no-op. After
    this returns, ``get_tact_keys_path()`` will return *None*.
    """
    from PySide6.QtCore import QSettings

    qs = QSettings(_APP_NAME, "D4Export")
    qs.remove(_QSETTINGS_KEY)
    qs.sync()

    dst = _cached_path()
    try:
        dst.unlink()
        log.info("TACT keys: cleared cached file %s", dst)
    except FileNotFoundError:
        pass
    except OSError as exc:
        # Logged but not raised — QSettings has already been cleared,
        # so callers see the expected "not loaded" state regardless.
        log.warning("Could not delete cached TACT keys file %s: %s", dst, exc)
