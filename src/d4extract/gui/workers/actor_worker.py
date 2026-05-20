"""Background worker that builds the actor display-name index.

:class:`ActorIndexBuildWorker` runs once at startup, mirroring
:class:`~d4extract.gui.workers.item_worker.ItemIndexBuildWorker`. On a
warm launch it loads the persisted on-disk index in milliseconds; on a
cold launch it runs the full build over ``CoreTOC.dat.json`` +
``incomingSnoReferences.json`` + every ``Actor_*.stl.json``, then writes
the cache to disk. Emits ``index_ready`` once the index is in
``actor_lookup._INDEX_CACHE``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal

log = logging.getLogger(__name__)


class ActorIndexBuildWorker(QThread):
    """Load the persisted actor name index from disk or build it.

    Signals:
        index_ready — the index is now in ``actor_lookup._INDEX_CACHE``.
        progress(str) — short status message for the status bar.
    """

    index_ready = Signal()
    progress = Signal(str)

    def __init__(self, d4data_path: Path, *, parent=None) -> None:
        super().__init__(parent)
        self._d4data_path = Path(d4data_path)

    def run(self) -> None:  # noqa: D401 — Qt API
        try:
            from d4extract.gui import actor_lookup
        except Exception:
            log.exception("actor_lookup import failed")
            return

        key = self._d4data_path.resolve()

        # Fast path: already in memory.
        with actor_lookup._INDEX_LOCK:
            if key in actor_lookup._INDEX_CACHE:
                self.index_ready.emit()
                return

        # Warm path: valid on-disk cache.
        disk = actor_lookup._load_cache_from_disk(key)
        if disk is not None:
            with actor_lookup._INDEX_LOCK:
                if key not in actor_lookup._INDEX_CACHE:
                    actor_lookup._INDEX_CACHE[key] = disk
            self.index_ready.emit()
            return

        if self.isInterruptionRequested():
            return

        # Cold path: full build.
        self.progress.emit("Building actor name index…")
        tracker = [0]

        def _cb(current: int, total: int) -> None:
            if self.isInterruptionRequested():
                return
            step = max(1, total // 10)
            prev, tracker[0] = tracker[0], current
            if prev // step < current // step or current >= total:
                self.progress.emit(
                    f"Building actor name index… {current:,}/{total:,}"
                )

        built = actor_lookup._build_index(key, progress_cb=_cb)
        if self.isInterruptionRequested():
            return

        actor_lookup._save_cache_to_disk(key, built)
        with actor_lookup._INDEX_LOCK:
            if key not in actor_lookup._INDEX_CACHE:
                actor_lookup._INDEX_CACHE[key] = built
        self.index_ready.emit()
