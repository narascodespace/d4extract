"""Background worker that runs the fuzzy scorer in bulk.

The previous implementation drove text filtering through
``QSortFilterProxyModel.filterAcceptsRow``, which crosses the
C++/Python bridge once per source row per keystroke (~67K calls). That
dominated keystroke latency long before the matcher itself did. This
worker pulls the filter into a single Python pass on a background
thread: every keystroke spawns one worker, the worker scores its
candidate set in pure Python, and the top results land back on the GUI
thread via a single signal.

Cancellation: ``requestInterruption`` is checked periodically inside
the score loop so a follow-up keystroke can abort an in-flight pass
quickly. Late results are also gated at the widget level by the
current-query check.
"""

from __future__ import annotations

import heapq
import logging
from typing import Sequence

from PySide6.QtCore import QThread, Signal

from d4extract.gui.fuzzy import multi_token_score

log = logging.getLogger(__name__)

# How often to poll for an interruption request inside the score loop.
# Each fuzzy_score call on a typical SNO path is sub-millisecond, so
# checking every ~1024 candidates keeps cancellation responsive without
# adding measurable overhead.
_INTERRUPT_CHECK_INTERVAL = 1024

# Cap on the number of results returned to the UI. Going past this just
# fills the list view with rows the user will never scroll to, and
# heapq.nlargest scales with this constant rather than the candidate
# count.
_TOP_N = 200


class FilterWorker(QThread):
    """Score ``candidates`` against ``query`` off-thread.

    ``results`` carries ``(query, ranked, survivors)``:

    * ``ranked`` — top ``_TOP_N`` ``(path, score, match_indices)``
      tuples sorted by score descending, shorter path first on ties,
      alphabetical on ties-of-ties. This is what the list view shows.
    * ``survivors`` — every path that matched, in scan order. The
      widget keys its incremental cache on this list so a subsequent
      keystroke can refine against the actual survivor set rather than
      the visually-truncated top 200. Skipping this would silently
      drop paths from the next prefix's candidate pool.

    The lists are typed ``object`` rather than ``list`` to dodge PySide6
    QVariant marshalling (see ``feedback_pyside_signal_marshaling`` in
    auto-memory): list-of-tuples-with-ndarrays-or-int-keys silently
    corrupts under QVariantMap conversion, and ``object`` keeps the raw
    PyObject pointer intact across the thread boundary.
    """

    results = Signal(str, object, object)

    def __init__(
        self,
        query: str,
        candidates: Sequence[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._query = query
        # Snapshot the input — the caller may mutate its list while
        # we're scanning, and the scan order is the only thing that
        # makes the Python loop predictable.
        self._candidates: list[str] = list(candidates)

    @property
    def query(self) -> str:
        return self._query

    def run(self) -> None:  # noqa: D401 — Qt API
        query = self._query
        candidates = self._candidates
        if not query or not candidates:
            self.results.emit(query, [], [])
            return

        # Build the full survivor list with scores. heapq.nlargest then
        # picks the top _TOP_N for display without paying the O(n log n)
        # cost of sorting every match. The unranked survivor *paths* go
        # into the second emit slot for the widget's cache.
        scored: list[tuple[int, int, str, list[int]]] = []
        survivor_paths: list[str] = []
        # Primary key is the score; secondary key is -len(path) so that
        # nlargest treats shorter paths as "larger" for tie-breaking.
        push = scored.append
        push_path = survivor_paths.append

        for i, path in enumerate(candidates):
            if i % _INTERRUPT_CHECK_INTERVAL == 0 and self.isInterruptionRequested():
                return
            res = multi_token_score(query, path)
            if res is None:
                continue
            score, indices = res
            push((score, -len(path), path, indices))
            push_path(path)

        if self.isInterruptionRequested():
            return

        top = heapq.nlargest(_TOP_N, scored)
        # Stable secondary pass: spec is score desc, then shorter path,
        # then alphabetical. The heap key already enforces the first two;
        # adding the path string as the final sort key resolves the
        # remaining ties deterministically.
        top.sort(key=lambda r: (-r[0], -r[1], r[2]))

        ranked = [(path, score, indices) for (score, _neg_len, path, indices) in top]
        if self.isInterruptionRequested():
            return
        self.results.emit(query, ranked, survivor_paths)
