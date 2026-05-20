"""Background workers for animation discovery and load.

Three workers are defined here:

- :class:`AnimIndexBuildWorker` runs once at startup.  It either loads the
  persisted on-disk index (warm launch, ~milliseconds) or runs a full 45K-file
  parallel scan and writes the result to disk (cold launch, ~5-10 s with a
  thread pool).  Emits ``index_ready`` when the full index is in
  ``_INDEX_CACHE``.

- :class:`AnimDiscoveryWorker` is started when the user selects a model.  If
  the full index is already in ``_INDEX_CACHE`` it returns complete results
  instantly; otherwise it falls back to a cheap prefix-glob (Layer 2) and
  emits ``is_complete=False`` so the GUI can show a "fast matches" notice and
  silently refresh when ``AnimIndexBuildWorker`` finishes.

- :class:`AnimLoadWorker` extracts the .ani payload from CASC, parses the
  AnimPayloadData header, and decodes the requested permutation.  The parsed/
  decoded ``DecodedAnimation`` is then handed back to the main thread.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThread, Signal

log = logging.getLogger(__name__)


def _anim_cache_root() -> Path:
    """Per-user dir for cached extracted .ani payloads."""
    base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
    return Path(base) / "d4extract" / "anim"


class AnimIndexBuildWorker(QThread):
    """Load the persisted animation index from disk or build it from scratch.

    On a warm launch (cache file present and fresh) this completes in
    milliseconds.  On a cold launch it runs the full 45K-file parallel
    scan via ``ThreadPoolExecutor`` then writes the cache to disk.

    Signals:
        index_ready — full index is now in ``anim_lookup._INDEX_CACHE``.
        progress(str) — short status message for the status bar.
    """

    index_ready = Signal()
    progress = Signal(str)

    def __init__(self, d4data_path: Path, *, parent=None) -> None:
        super().__init__(parent)
        self._d4data_path = Path(d4data_path)

    def run(self) -> None:  # noqa: D401 — Qt API
        try:
            from d4extract.gui import anim_lookup
        except Exception as exc:
            log.exception("anim_lookup import failed")
            return

        key = self._d4data_path.resolve()

        # Fast path: already in memory.
        with anim_lookup._INDEX_LOCK:
            if key in anim_lookup._INDEX_CACHE:
                self.index_ready.emit()
                return

        # Warm path: valid on-disk cache.
        disk = anim_lookup._load_cache_from_disk(key)
        if disk is not None:
            with anim_lookup._INDEX_LOCK:
                if key not in anim_lookup._INDEX_CACHE:
                    anim_lookup._INDEX_CACHE[key] = disk
            self.index_ready.emit()
            return

        if self.isInterruptionRequested():
            return

        # Cold path: full parallel build.
        self.progress.emit("Building animation index…")
        tracker = [0]

        def _cb(current: int, total: int) -> None:
            if self.isInterruptionRequested():
                return
            step = max(1, total // 10)
            prev, tracker[0] = tracker[0], current
            if prev // step < current // step or current >= total:
                self.progress.emit(
                    f"Building animation index… {current:,}/{total:,}"
                )

        built = anim_lookup._build_index(key, progress_cb=_cb)
        if self.isInterruptionRequested():
            return

        anim_lookup._save_cache_to_disk(key, built)
        with anim_lookup._INDEX_LOCK:
            if key not in anim_lookup._INDEX_CACHE:
                anim_lookup._INDEX_CACHE[key] = built
        self.index_ready.emit()


class AnimDiscoveryWorker(QThread):
    """Retrieve animations for one appearance from the index.

    If the full index is already in ``_INDEX_CACHE`` (warm launch or
    the background build has finished), results are complete and
    returned immediately.  Otherwise the worker falls back to a fast
    prefix-glob and emits ``is_complete=False`` so the GUI can show a
    "fast matches" notice and silently refresh when the full build
    completes.

    Signals:
        finished(str, list, bool) — appearance name, AnimationInfo list,
            is_complete (False = prefix-glob; True = full index).
        progress(str)             — short status message.
        error(str)                — failure; ``finished`` does not fire.
    """

    finished = Signal(str, list, bool)
    progress = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        appearance_name: str,
        d4data_path: Path,
        *,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.appearance_name = appearance_name
        self._d4data_path = Path(d4data_path)

    def run(self) -> None:  # noqa: D401 — Qt API
        try:
            from d4extract.gui import anim_lookup
        except Exception as exc:
            log.exception("anim_lookup import failed")
            self.error.emit(f"anim_lookup import failed: {exc}")
            return

        key = self._d4data_path.resolve()

        with anim_lookup._INDEX_LOCK:
            full_index = anim_lookup._INDEX_CACHE.get(key)

        if full_index is not None:
            infos = list(full_index.get(self.appearance_name, ()))
            self.finished.emit(self.appearance_name, infos, True)
            return

        if self.isInterruptionRequested():
            return

        self.progress.emit("Scanning animations…")
        try:
            infos = anim_lookup.prefix_glob_animations(
                self.appearance_name, self._d4data_path
            )
        except Exception as exc:
            log.exception("AnimDiscoveryWorker prefix-glob failed")
            self.error.emit(f"{type(exc).__name__}: {exc}")
            return

        if self.isInterruptionRequested():
            return
        self.finished.emit(self.appearance_name, infos, False)


class AnimLoadWorker(QThread):
    """Extract + parse + decode one animation permutation.

    Signals:
        finished(object, int) — DecodedAnimation, permutation_index.
            Typed as ``object`` because Qt's signal annotation can't
            forward-reference a dataclass cleanly.
        progress(str) — status messages.
        error(str)    — failure (no ``finished`` follow-up).
    """

    finished = Signal(object, int)
    progress = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        game_dir: Path,
        anim_info: object,           # AnimationInfo
        permutation_index: int,
        d4data_path: Path,
        rest_pose_map: dict[int, tuple],
        *,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._game_dir = Path(game_dir)
        self._anim_info = anim_info
        self._permutation_index = permutation_index
        self._d4data_path = Path(d4data_path)
        self._rest_pose_map = rest_pose_map
        # Public so callers can correlate the result with the request
        # they queued (matches the LoadModelWorker pattern).
        self.anim_name = getattr(anim_info, "name", "")

    def run(self) -> None:  # noqa: D401 — Qt API
        try:
            from d4extract.casc.rustydemon import (
                CASCExtractionError,
                RustyDemonCLI,
            )
            from d4extract.formats.anim_parser import (
                AnimFormatError,
                decode_permutation,
                parse_anim,
            )
        except Exception as exc:
            log.exception("AnimLoadWorker import failed")
            self.error.emit(f"Import failure: {exc}")
            return

        try:
            cache_root = _anim_cache_root() / self.anim_name
            cache_root.mkdir(parents=True, exist_ok=True)
            output_dir = cache_root
        except OSError:
            output_dir = Path(tempfile.mkdtemp(prefix="d4extract_anim_"))

        self.progress.emit(f"Extracting {self.anim_name} from CASC…")
        try:
            rd = RustyDemonCLI(self._game_dir)
            meta_path, payload_path, _is_shared = rd.extract_anim_pair(
                self.anim_name, output_dir, d4data_path=self._d4data_path,
            )
        except CASCExtractionError as exc:
            self.error.emit(str(exc))
            return
        except Exception as exc:
            log.exception("CASC extraction failed for animation")
            self.error.emit(f"{type(exc).__name__}: {exc}")
            return

        if self.isInterruptionRequested():
            return

        # The CASC extract gives us a binary .ani payload; the parser
        # also needs the d4data .ani.json meta — that path comes from
        # the AnimationInfo (it was the source of the index entry).
        meta_json = getattr(self._anim_info, "ani_meta_path", None)
        if meta_json is None or not Path(meta_json).is_file():
            self.error.emit(
                f"Animation meta JSON missing: {meta_json}"
            )
            return

        self.progress.emit("Parsing animation…")
        try:
            perm = parse_anim(
                Path(meta_json), payload_path,
                permutation_index=self._permutation_index,
            )
        except AnimFormatError as exc:
            self.error.emit(f"Animation parse error: {exc}")
            return
        except Exception as exc:
            log.exception("parse_anim failed")
            self.error.emit(f"{type(exc).__name__}: {exc}")
            return

        if self.isInterruptionRequested():
            return

        self.progress.emit("Decoding curves…")
        try:
            decoded = decode_permutation(perm, rest_pose=self._rest_pose_map)
        except AnimFormatError as exc:
            self.error.emit(f"Decode error: {exc}")
            return
        except Exception as exc:
            log.exception("decode_permutation failed")
            self.error.emit(f"{type(exc).__name__}: {exc}")
            return

        if self.isInterruptionRequested():
            return
        self.finished.emit(decoded, self._permutation_index)
