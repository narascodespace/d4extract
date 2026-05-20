"""Filtered piece list for the Character Builder.

Shows the candidate equipment pieces for the currently selected
class+gender+slot. Filtering is two-stage:

1. **Slot filter** — narrow ``_all_entries`` (the full CASC catalog)
   to entries whose filename matches the active slot. Armor slots
   intersect a class+gender prefix (e.g. ``barF_…_HLM.app``);
   weapons (mh/oh) use type prefixes that don't depend on class.
2. **Text filter** — fuzzy-score the slot-filtered candidates
   against the search box, off-thread, via the same
   :class:`~d4extract.gui.workers.filter_worker.FilterWorker` used by
   the Model Browser.

Stage 1 is a fast pure-Python pass that runs on the GUI thread. Stage
2 is debounced and can be cancelled by the next keystroke.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Iterable

from PySide6.QtCore import (
    QModelIndex,
    QSignalBlocker,
    QStringListModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QLineEdit,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from d4extract.gui.theme import Colors
from d4extract.gui.widgets.character_builder.models import EQUIPMENT_SLOTS
from d4extract.gui.workers.filter_worker import FilterWorker

log = logging.getLogger(__name__)


# Per-slot filename suffix patterns (case-insensitive). The "bdy"
# (Chest) slot intentionally accepts both ``_TRS.app`` and ``_BDY.app``
# because game data uses both for chest meshes.
_SLOT_SUFFIX_PATTERNS: dict[str, str] = {
    "hlm": r"_HLM\.app$",
    "bdy": r"_(?:TRS|BDY)\.app$",
    "glv": r"_GLV\.app$",
    "leg": r"_LEG\.app$",
    "bts": r"_BTS\.app$",
}

# Weapon / off-hand filename prefixes. These don't share the
# class+gender prefix shape — weapons are reused across classes, and
# off-hands are class-specific but live under their own naming family
# rather than under ``{classGender}_*``.
_WEAPON_PATTERN = (
    r"^(?:Axe_|Sword_|Dagger_|Mace_|Scythe_|Wand_|Polearm_|Staff_|"
    r"Bow_|Crossbow_|2HAxe_|2HSword_|2HMace_)"
)
_OFFHAND_PATTERN = (
    r"^(?:Shield_|offHandFocus_|offHandsDruid_|offHandsNecro_|"
    r"OffHandsSorc_|OffHandTotem_|Totem_)"
)

# Matches the model list's debounce: 16ms ≈ a 60Hz frame, swallowing
# autorepeat / paste bursts without adding perceptible latency.
_FILTER_DEBOUNCE_MS = 16

# Slot key → user-visible name, mirrored from EQUIPMENT_SLOTS so the
# context label can read "Helm" rather than "hlm".
_SLOT_DISPLAY: dict[str, str] = dict(EQUIPMENT_SLOTS)

# Each list row is a composed string ``"<label>\x1f<sno_path>"``: the
# label is what the user sees and the SNO path is the unique key the
# selection / fuzzy machinery needs. Composing them keeps the model
# strings unique (display names collide — "Buckler" appears twice) and
# lets the fuzzy worker match either the in-game name OR the SNO path
# without any change to the shared FilterWorker. The delegate paints
# only the label; ``\x1f`` (unit separator) never occurs in real data.
_ROW_SEP = "\x1f"


def _basename(path: str) -> str:
    """Return the trailing filename component of a CASC SNO path."""
    return path.rsplit("/", 1)[-1]


def _stem_no_ext(sno_path: str) -> str:
    """Filename of a SNO path with any ``.app`` extension stripped."""
    name = _basename(sno_path)
    if name.lower().endswith(".app"):
        name = name[:-4]
    return name


def _row_label(row: str) -> str:
    """The user-visible label half of a composed list row."""
    return row.split(_ROW_SEP, 1)[0]


class _FuzzyHighlightDelegate(QStyledItemDelegate):
    """Paint piece rows with fuzzy-match indices in the accent colour.

    Stripped-down version of :class:`FuzzyMatchDelegate` from the
    Model Browser — no star gutter, no favorites, just the
    monospace-with-highlights render.

    Each row is a composed ``"<label>\\x1f<sno_path>"`` string (see
    ``_ROW_SEP``); only the label is painted. Match indices come keyed
    by the *whole* composed row, so an index that falls inside the
    hidden SNO-path half (a search that hit the path, not the name)
    simply paints nothing — the row still appears in the filtered list.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._indices_by_path: dict[str, list[int]] = {}
        self._match_color = QColor(Colors.ACCENT)
        self._text_color = QColor(Colors.TEXT)

    def set_match_indices(self, indices_by_path: dict[str, list[int]]) -> None:
        self._indices_by_path = indices_by_path

    def paint(self, painter, option, index) -> None:  # noqa: D401 — Qt API
        row = index.data(Qt.DisplayRole)
        if not isinstance(row, str):
            super().paint(painter, option, index)
            return
        # The painted text is the label half; match indices stay keyed
        # by the full composed row.
        path = _row_label(row)

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""

        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)

        text_rect = style.subElementRect(
            QStyle.SE_ItemViewItemText, opt, widget,
        )

        painter.save()
        painter.setFont(opt.font)
        fm = painter.fontMetrics()

        # Mono font: every glyph has the same advance, so we use 'M'
        # as the canonical width. ``or 1`` guards against a zero-width
        # font during early paints before the QSS lands.
        char_w = fm.horizontalAdvance("M") or 1
        max_chars = max(1, text_rect.width() // char_w)

        skip = 0
        if len(path) > max_chars:
            keep = max(1, max_chars - 1)
            skip = len(path) - keep
            display = "…" + path[skip:]
        else:
            display = path

        indices_set = set(self._indices_by_path.get(row, ()))

        x = text_rect.x()
        baseline = (
            text_rect.y()
            + (text_rect.height() + fm.ascent() - fm.descent()) // 2
        )

        for i, ch in enumerate(display):
            if skip > 0 and i == 0:
                is_match = False
            elif skip > 0:
                is_match = (i - 1 + skip) in indices_set
            else:
                is_match = i in indices_set
            painter.setPen(self._match_color if is_match else self._text_color)
            painter.drawText(x, baseline, ch)
            x += char_w

        painter.restore()


class PieceBrowser(QWidget):
    """Class+gender+slot scoped piece picker."""

    piece_selected = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(True)

        # Catalog + filter state.
        self._all_entries: list[str] = []
        self._entry_set: set[str] = set()
        self._slot_filtered: list[str] = []
        # ``_rows`` is ``_slot_filtered`` projected into composed
        # "<label>\x1f<sno>" strings — what the model and fuzzy worker
        # actually operate on. ``_row_to_sno`` maps a composed row back
        # to its SNO path for selection.
        self._rows: list[str] = []
        self._row_to_sno: dict[str, str] = {}
        # Resolver supplied by the builder: SNO path → display label
        # (the in-game item name, or the SNO stem when unresolved).
        # ``None`` until wired — rows then fall back to the bare stem.
        self._name_resolver: Callable[[str], str] | None = None
        self._class_gender_prefix: str | None = None
        self._current_slot: str | None = None
        self._current_class: str | None = None
        self._current_gender: str | None = None

        # Text filter state.
        self._current_query: str = ""
        self._pending_query: str = ""
        self._filter_worker: FilterWorker | None = None
        # Cache: query → (ranked, survivors). Cleared on every slot
        # change — survivor sets are scored against a specific
        # candidate pool, and the pool changes whenever the slot does.
        self._fuzzy_cache: dict[
            str,
            tuple[list[tuple[str, int, list[int]]], list[str]],
        ] = {}

        self._delegate = _FuzzyHighlightDelegate(self)

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(_FILTER_DEBOUNCE_MS)
        self._debounce_timer.timeout.connect(self._fire_debounced_filter)

        self._build_ui()
        self._update_context_label()
        self._update_status_label()
        self._update_search_enabled()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        # Section header (matches SlotPanel's header style).
        header = QLabel("PIECES", self)
        header.setStyleSheet(
            f"color: {Colors.SUBTEXT}; "
            f"font-size: 11px; font-weight: 700; "
            "letter-spacing: 1px;"
        )
        outer.addWidget(header)

        self._context_label = QLabel("", self)
        self._context_label.setObjectName("pieceContext")
        self._context_label.setWordWrap(True)
        outer.addWidget(self._context_label)

        self._filter_input = QLineEdit(self)
        self._filter_input.setPlaceholderText("Search pieces…")
        self._filter_input.setClearButtonEnabled(True)
        self._filter_input.textChanged.connect(self._on_filter_text_changed)
        outer.addWidget(self._filter_input)

        self._source_model = QStringListModel(self)
        self._list_view = QListView(self)
        self._list_view.setModel(self._source_model)
        self._list_view.setItemDelegate(self._delegate)
        self._list_view.setUniformItemSizes(True)
        self._list_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # Long SNO paths shouldn't trigger a horizontal scrollbar that
        # pops in/out during splitter drags. The delegate also elides
        # left so the filename stays visible.
        self._list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list_view.setTextElideMode(Qt.ElideLeft)
        self._list_view.clicked.connect(self._on_list_clicked)
        outer.addWidget(self._list_view, 1)

        self._status_label = QLabel("", self)
        self._status_label.setObjectName("listStatus")
        outer.addWidget(self._status_label)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def all_entries(self) -> list[str]:
        """The full CASC catalog (read-only view)."""
        return self._all_entries

    def load_entries(self, entries: Iterable[str]) -> None:
        """Receive the full CASC catalog.

        Filtering is recomputed in case the user already picked a
        slot before the catalog arrived (legitimate when ``set_*``
        was called against an empty ``_all_entries`` and the worker
        finally produced data).
        """
        self._all_entries = list(entries)
        self._entry_set = set(self._all_entries)
        self._fuzzy_cache.clear()
        self._recompute_slot_filtered()
        self._reapply_text_filter()

    def set_name_resolver(
        self, resolver: Callable[[str], str] | None,
    ) -> None:
        """Install the SNO-path → display-label resolver.

        ``resolver`` must always return a usable label — the in-game
        item name when known, the SNO stem otherwise. Re-projects the
        current rows so a resolver wired after the catalog loaded takes
        effect immediately.
        """
        self._name_resolver = resolver
        self.refresh_display_names()

    def refresh_display_names(self) -> None:
        """Re-project rows through the resolver and re-render.

        Called when the equipment name index finishes building so rows
        composed against the not-yet-ready resolver pick up real names.
        """
        self._fuzzy_cache.clear()
        self._compose_rows()
        self._reapply_text_filter()

    def set_class_gender_filter(self, prefix: str | None) -> None:
        """Update the class+gender prefix used by armor slot filtering.

        ``None`` clears the prefix; armor-slot filtering will then
        produce an empty list until both class and gender are set
        again. Weapon/off-hand filtering ignores this value.
        """
        if prefix == self._class_gender_prefix:
            return
        self._class_gender_prefix = prefix
        self._fuzzy_cache.clear()
        self._recompute_slot_filtered()
        self._reapply_text_filter()

    def set_slot_filter(self, slot_key: str | None) -> None:
        if slot_key == self._current_slot:
            return
        self._current_slot = slot_key
        self._fuzzy_cache.clear()
        self._recompute_slot_filtered()
        self._reapply_text_filter()

    def clear_filters(self) -> None:
        self._class_gender_prefix = None
        self._current_slot = None
        self._current_class = None
        self._current_gender = None
        self._fuzzy_cache.clear()
        self._slot_filtered = []
        self._rows = []
        self._row_to_sno = {}
        with QSignalBlocker(self._filter_input):
            self._filter_input.clear()
        self._current_query = ""
        self._pending_query = ""
        self._cancel_filter_worker()
        self._source_model.setStringList([])
        self._delegate.set_match_indices({})
        self._update_context_label()
        self._update_status_label()
        self._update_search_enabled()

    def set_class_gender_display(
        self, class_name: str | None, gender: str | None,
    ) -> None:
        """Set the display strings used by the context label.

        The class+gender prefix is set separately via
        ``set_class_gender_filter``; this is purely cosmetic.
        """
        self._current_class = class_name
        self._current_gender = gender
        self._update_context_label()

    def has_entry(self, sno_path: str) -> bool:
        """Return *True* if ``sno_path`` exists in the loaded catalog."""
        return sno_path in self._entry_set

    def shutdown(self) -> None:
        """Halt any in-flight filter worker before the window closes."""
        try:
            self._debounce_timer.stop()
        except RuntimeError:
            pass
        self._cancel_filter_worker(wait=True)

    # ------------------------------------------------------------------
    # Filter pipeline
    # ------------------------------------------------------------------

    def _recompute_slot_filtered(self) -> None:
        self._slot_filtered = self._compute_slot_filtered()
        self._compose_rows()

    def _compose_rows(self) -> None:
        """Project ``_slot_filtered`` (SNO paths) into composed rows.

        Each row is ``"<label>\\x1f<sno_path>"``; ``_row_to_sno`` maps it
        back. The label is the resolver's output (in-game name, or SNO
        stem when the index has no entry / isn't built yet).

        Rows are sorted alphabetically by label (case-insensitive) —
        that's the order the unfiltered list shows. Search results stay
        relevance-ranked by the fuzzy worker, so this sort only governs
        the scan order it ranks against, which is harmless.
        """
        rows: list[str] = []
        mapping: dict[str, str] = {}
        for sno in self._slot_filtered:
            label = ""
            if self._name_resolver is not None:
                try:
                    label = self._name_resolver(sno)
                except Exception:  # noqa: BLE001 — never break the list
                    label = ""
            if not label:
                label = _stem_no_ext(sno)
            row = f"{label}{_ROW_SEP}{sno}"
            rows.append(row)
            mapping[row] = sno
        rows.sort(key=lambda r: _row_label(r).lower())
        self._rows = rows
        self._row_to_sno = mapping

    def _compute_slot_filtered(self) -> list[str]:
        slot = self._current_slot
        if slot is None or not self._all_entries:
            return []

        if slot == "mh":
            rx = re.compile(_WEAPON_PATTERN, re.IGNORECASE)
            return [e for e in self._all_entries if rx.search(_basename(e))]
        if slot == "oh":
            rx = re.compile(_OFFHAND_PATTERN, re.IGNORECASE)
            return [e for e in self._all_entries if rx.search(_basename(e))]

        suffix_pattern = _SLOT_SUFFIX_PATTERNS.get(slot)
        if suffix_pattern is None:
            return []
        prefix = self._class_gender_prefix
        if prefix is None:
            # Armor slots require a class+gender — without one, there's
            # nothing legitimate to show.
            return []

        prefix_lower = prefix.lower() + "_"
        suffix_rx = re.compile(suffix_pattern, re.IGNORECASE)
        out: list[str] = []
        for e in self._all_entries:
            name = _basename(e)
            if not name.lower().startswith(prefix_lower):
                continue
            if suffix_rx.search(name):
                out.append(e)
        return out

    def _reapply_text_filter(self) -> None:
        """Re-render after either the candidate pool or the query changed."""
        self._update_context_label()
        self._update_search_enabled()

        if self._current_slot is None:
            # No slot picked → list view stays empty regardless of query.
            self._delegate.set_match_indices({})
            self._source_model.setStringList([])
            self._update_status_label()
            return

        if not self._current_query:
            self._show_unfiltered()
            return

        # A pool change may have invalidated cached scores — fire a
        # fresh worker pass against the new candidate set.
        self._fire_debounced_filter_for(self._current_query)

    def _show_unfiltered(self) -> None:
        self._delegate.set_match_indices({})
        self._source_model.setStringList(self._rows)
        self._update_status_label()

    # ------------------------------------------------------------------
    # Text input → fuzzy worker
    # ------------------------------------------------------------------

    def _on_filter_text_changed(self, text: str) -> None:
        # Debounce: only the last keystroke in the 16ms window spawns
        # a worker. Same shape as ModelListWidget.
        self._pending_query = text
        self._debounce_timer.start()

    def _fire_debounced_filter(self) -> None:
        self._fire_debounced_filter_for(self._pending_query)

    def _fire_debounced_filter_for(self, query: str) -> None:
        self._current_query = query

        if not query or not query.strip():
            self._cancel_filter_worker()
            self._show_unfiltered()
            return

        if not self._rows:
            # Nothing to filter against. Skip the worker; the list is
            # already empty from the caller's perspective.
            self._cancel_filter_worker()
            self._delegate.set_match_indices({})
            self._source_model.setStringList([])
            self._update_status_label(query=query)
            return

        cached = self._fuzzy_cache.get(query)
        if cached is not None:
            self._cancel_filter_worker()
            ranked, _survivors = cached
            self._apply_fuzzy_results(query, ranked)
            return

        # Refinement: a longer query's matches are a subset of any
        # cached prefix's survivors (subsequence matching is monotonic
        # in query length), so we can scan that smaller list.
        candidates: list[str] | None = None
        for k in range(len(query) - 1, 0, -1):
            cached_prefix = self._fuzzy_cache.get(query[:k])
            if cached_prefix is not None:
                _ranked, survivors = cached_prefix
                candidates = survivors
                break
        if candidates is None:
            candidates = self._rows

        self._spawn_filter_worker(query, candidates)

    def _spawn_filter_worker(
        self, query: str, candidates: list[str],
    ) -> None:
        self._cancel_filter_worker(wait=False)
        worker = FilterWorker(query, candidates, parent=self)
        worker.results.connect(self._on_filter_results)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self._clear_worker_ref)
        self._filter_worker = worker
        worker.start()

    def _cancel_filter_worker(self, *, wait: bool = True) -> None:
        prior = self._filter_worker
        if prior is None:
            return
        try:
            if prior.isRunning():
                prior.requestInterruption()
                # Disconnect the results signal so a late emit can't
                # land after we've moved on.
                try:
                    prior.results.disconnect()
                except (RuntimeError, TypeError):
                    pass
                if wait:
                    prior.quit()
                    prior.wait(2000)
        except RuntimeError:
            pass
        self._filter_worker = None

    def _clear_worker_ref(self, *_args) -> None:
        self._filter_worker = None

    def _on_filter_results(
        self,
        query: str,
        ranked: list[tuple[str, int, list[int]]],
        survivors: list[str],
    ) -> None:
        if query != self._current_query:
            return
        self._fuzzy_cache[query] = (ranked, survivors)
        self._apply_fuzzy_results(query, ranked)

    def _apply_fuzzy_results(
        self,
        query: str,
        results: list[tuple[str, int, list[int]]],
    ) -> None:
        paths = [r[0] for r in results]
        self._delegate.set_match_indices({r[0]: r[2] for r in results})
        self._source_model.setStringList(paths)
        self._update_status_label(query=query)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _on_list_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        value = self._source_model.data(index, Qt.DisplayRole)
        if not isinstance(value, str) or not value:
            return
        # The model row is a composed "<label>\x1f<sno>" string —
        # selection emits the SNO path, never the label.
        sno = self._row_to_sno.get(value)
        if sno is None and _ROW_SEP in value:
            sno = value.split(_ROW_SEP, 1)[1]
        if sno:
            self.piece_selected.emit(sno)

    # ------------------------------------------------------------------
    # Cosmetic state
    # ------------------------------------------------------------------

    def _update_context_label(self) -> None:
        cls = self._current_class
        gen = self._current_gender
        slot = self._current_slot

        if cls is None:
            text = "Select a class and slot"
        elif gen is None:
            text = f"{cls} — Select a gender and slot"
        elif slot is None:
            text = f"{cls} {gen} — Select a slot"
        else:
            slot_name = _SLOT_DISPLAY.get(slot, slot)
            text = f"{cls} {gen} — {slot_name}"

        self._context_label.setText(text)

    def _update_status_label(self, *, query: str | None = None) -> None:
        total = len(self._slot_filtered)
        if self._current_slot is None:
            self._status_label.setText("")
            return
        if total == 0:
            self._status_label.setText("0 pieces")
            return
        if query:
            visible = self._source_model.rowCount()
            self._status_label.setText(f"{visible:,} of {total:,} pieces")
            return
        self._status_label.setText(f"{total:,} pieces")

    def _update_search_enabled(self) -> None:
        # Search is meaningless without a candidate pool. Tying the
        # enable state to ``_slot_filtered`` rather than to the
        # individual selectors covers both the "no slot picked" and
        # "slot picked but nothing matched" cases naturally.
        self._filter_input.setEnabled(bool(self._slot_filtered))
