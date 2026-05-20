"""Background worker that builds the equipment display-name index.

:class:`ItemIndexBuildWorker` runs once at startup, mirroring
:class:`~d4extract.gui.workers.anim_worker.AnimIndexBuildWorker`. On a
warm launch it loads the persisted on-disk index in milliseconds; on a
cold launch it runs the full ~8-12 s build over ``CoreTOC.dat.json`` +
``incomingSnoReferences.json`` + the per-item StringList files, then
writes the cache to disk. Emits ``index_ready`` once the index is in
``item_lookup._INDEX_CACHE``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)


class ItemIndexBuildWorker(QThread):
    """Load the persisted equipment name index from disk or build it.

    Signals:
        index_ready — the index is now in ``item_lookup._INDEX_CACHE``.
        progress(str) — short status message for the status bar.
    """

    index_ready = Signal()
    progress = Signal(str)

    def __init__(self, d4data_path: Path, *, parent=None) -> None:
        super().__init__(parent)
        self._d4data_path = Path(d4data_path)

    def run(self) -> None:  # noqa: D401 — Qt API
        try:
            from d4extract.gui import item_lookup
        except Exception:
            log.exception("item_lookup import failed")
            return

        key = self._d4data_path.resolve()

        # Fast path: already in memory.
        with item_lookup._INDEX_LOCK:
            if key in item_lookup._INDEX_CACHE:
                self.index_ready.emit()
                return

        # Warm path: valid on-disk cache.
        disk = item_lookup._load_cache_from_disk(key)
        if disk is not None:
            with item_lookup._INDEX_LOCK:
                if key not in item_lookup._INDEX_CACHE:
                    item_lookup._INDEX_CACHE[key] = disk
            self.index_ready.emit()
            return

        if self.isInterruptionRequested():
            return

        # Cold path: full build.
        self.progress.emit("Building equipment name index…")
        tracker = [0]

        def _cb(current: int, total: int) -> None:
            if self.isInterruptionRequested():
                return
            step = max(1, total // 10)
            prev, tracker[0] = tracker[0], current
            if prev // step < current // step or current >= total:
                self.progress.emit(
                    f"Building equipment name index… {current:,}/{total:,}"
                )

        built = item_lookup._build_index(key, progress_cb=_cb)
        if self.isInterruptionRequested():
            return

        item_lookup._save_cache_to_disk(key, built)
        with item_lookup._INDEX_LOCK:
            if key not in item_lookup._INDEX_CACHE:
                item_lookup._INDEX_CACHE[key] = built
        self.index_ready.emit()
