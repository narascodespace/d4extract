"""Filterable, scrollable list of CASC model SNO paths.

The text filter runs through a fuzzy subsequence scorer rather than a
substring match. See :mod:`d4extract.gui.fuzzy` for the algorithm and
:mod:`d4extract.gui.workers.filter_worker` for the background scan.

Each visible row is a composed string ``"<label>\\x1f<sno_path>"``: the
label is what the user sees (the in-game / actor display name when known,
the ``.app`` stem otherwise) and the SNO path stays in the string so the
fuzzy worker matches either half and so favorites / selection / category
filtering all key off the SNO path, not the rotating label. The delegate
paints only the label half; ``\\x1f`` (unit separator) never occurs in
real CASC paths.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Iterable

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QSortFilterProxyModel,
    QStringListModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QStackedLayout,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from d4extract.gui.settings import AppSettings
from d4extract.gui.theme import Colors
from d4extract.gui.workers.filter_worker import FilterWorker

log = logging.getLogger(__name__)


# (button label, regex matched case-insensitively against the SNO path).
# Order matches the spec; ALL is special-cased and clears the filter.
CATEGORY_PILLS: tuple[tuple[str, str | None], ...] = (
    ("ALL", None),
    ("MON", r"monster_"),
    ("PLR", r"player_"),
    ("NPC", r"npc_"),
    ("ITM", r"item_"),
    ("ENV", r"environment_|world_"),
    ("WPN", r"weapon_"),
)


# Coalesce rapid typing so each keystroke in a 16ms window doesn't
# spawn a worker. 16ms ≈ a 60Hz frame, which is well below the
# perceptible response budget but still long enough to swallow bursts
# from autorepeat / paste.
_FILTER_DEBOUNCE_MS = 16

# Width (px) of the star gutter at the left of every row. The delegate
# paints the star within this strip and shifts the path text right by
# the same amount; the click filter on the viewport routes presses
# inside this band to the favorite-toggle path instead of letting them
# bubble up as model selections.
_STAR_AREA_WIDTH = 20

# Composed-row separator. ``\x1f`` (ASCII unit separator) never occurs
# in CASC SNO paths, so splitting on it always recovers the two halves
# cleanly.
_ROW_SEP = "\x1f"


def _row_label(row: str) -> str:
    """The user-visible label half of a composed list row."""
    return row.split(_ROW_SEP, 1)[0]


def _row_sno(row: str) -> str:
    """The SNO-path half of a composed list row.

    Accepts a bare SNO path too (no separator) — that's what we serve
    while the name resolver is still ``None`` and rows haven't been
    composed yet, so the helper has to be tolerant.
    """
    sep_idx = row.find(_ROW_SEP)
    if sep_idx < 0:
        return row
    return row[sep_idx + 1:]


def _stem_no_ext(sno_path: str) -> str:
    """Filename of a SNO path with any ``.app`` extension stripped."""
    name = sno_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if name.lower().endswith(".app"):
        name = name[:-4]
    return name


class _CategoryProxyModel(QSortFilterProxyModel):
    """Proxy that applies only the category-pill regex.

    Text filtering used to live here too; that path crossed the
    C++/Python bridge once per source row per keystroke and ran ~67K
    calls per character on a real catalog. It now lives in
    :class:`~d4extract.gui.workers.filter_worker.FilterWorker`.

    The regex is matched against the SNO-path half of a composed row,
    never the label — otherwise category pills like ``MON`` would also
    accept rows whose display name happens to contain "monster".
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._category_regex: re.Pattern[str] | None = None

    def set_category_regex(self, pattern: str | None) -> None:
        if pattern is None:
            self._category_regex = None
        else:
            self._category_regex = re.compile(pattern, re.IGNORECASE)
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if self._category_regex is None:
            return True
        idx = self.sourceModel().index(source_row, 0, source_parent)
        value = self.sourceModel().data(idx, Qt.DisplayRole)
        if not isinstance(value, str):
            return False
        # Match on the SNO path only — the label can legitimately
        # contain category-overlapping English words.
        return self._category_regex.search(_row_sno(value)) is not None


class FuzzyMatchDelegate(QStyledItemDelegate):
    """Paint matched characters in the accent color, with a star gutter.

    The leftmost ``_STAR_AREA_WIDTH`` pixels of every row are the
    favorite-toggle gutter:

    * Starred row → solid yellow ★.
    * Hovered, unstarred row → dim ★ in the border colour, hinting at
      "click here to favorite".
    * Otherwise → empty (the path text just appears slightly indented).

    The path itself is drawn after the gutter with the same per-char
    highlighting logic as before; only the starting x-coordinate
    shifts. Match indices are pushed in by the widget when filter
    results land.

    Each row is a composed ``"<label>\\x1f<sno_path>"`` string; only the
    label is painted. Match indices come keyed by the *whole* composed
    row, so an index that falls inside the hidden SNO-path half simply
    paints nothing — the row still appears in the filtered list.
    """

    # Single-character glyphs for the star. U+2605 / U+2606 ship in
    # virtually every monospace font on Windows / macOS / Linux, so we
    # don't need to ship a font asset just to draw them.
    _STAR_FILLED = "★"
    _STAR_OUTLINE = "☆"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._indices_by_path: dict[str, list[int]] = {}
        self._favorites: set[str] = set()
        self._match_color = QColor(Colors.ACCENT)
        self._text_color = QColor(Colors.TEXT)
        self._star_active_color = QColor(Colors.FAVORITE)
        self._star_hint_color = QColor(Colors.BORDER)

    def set_match_indices(self, indices_by_path: dict[str, list[int]]) -> None:
        self._indices_by_path = indices_by_path

    def set_favorites(self, favorites: set[str]) -> None:
        # Stored by reference: callers pass the widget's live set so
        # toggles take effect on the next paint without us round-tripping
        # through a setter on every change. Make a copy if we ever start
        # mutating it from inside the delegate.
        self._favorites = favorites

    def paint(self, painter, option, index) -> None:  # noqa: D401 — Qt API
        row = index.data(Qt.DisplayRole)
        if not isinstance(row, str):
            super().paint(painter, option, index)
            return
        # The painted text is the label half; match indices stay keyed
        # by the full composed row. Favorites are SNO-path keyed.
        label = _row_label(row)
        sno = _row_sno(row)

        # Compose the standard option (selection / hover / palette) but
        # blank out the text — we'll draw it ourselves with per-char
        # coloring after the style has painted the background.
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

        # ---- Star gutter ---------------------------------------------
        is_favorite = sno in self._favorites
        is_hover = bool(opt.state & QStyle.State_MouseOver)

        star_glyph: str | None = None
        star_color: QColor | None = None
        if is_favorite:
            star_glyph = self._STAR_FILLED
            star_color = self._star_active_color
        elif is_hover:
            star_glyph = self._STAR_OUTLINE
            star_color = self._star_hint_color

        if star_glyph is not None:
            painter.setPen(star_color)
            star_w = fm.horizontalAdvance(star_glyph)
            star_x = text_rect.x() + max(0, (_STAR_AREA_WIDTH - star_w) // 2)
            star_baseline = (
                text_rect.y()
                + (text_rect.height() + fm.ascent() - fm.descent()) // 2
            )
            painter.drawText(star_x, star_baseline, star_glyph)

        # ---- Path text (offset by the gutter width) ------------------
        # Mono font: every glyph has the same advance, so we use 'M' as
        # the canonical width. Anything else (e.g. ' ') would skew the
        # layout when the QSS-supplied font reports proportional widths
        # for whitespace.
        char_w = fm.horizontalAdvance("M") or 1
        avail_w = text_rect.width() - _STAR_AREA_WIDTH
        max_chars = max(1, avail_w // char_w)

        # Elide LEFT so the most informative tail of the label remains
        # visible. The list view itself sets ElideLeft, but that path
        # runs the default delegate's text painter which we are
        # bypassing here.
        skip = 0
        if len(label) > max_chars:
            keep = max(1, max_chars - 1)  # leave 1 char for the ellipsis
            skip = len(label) - keep
            display = "…" + label[skip:]
        else:
            display = label

        indices_set = set(self._indices_by_path.get(row, ()))

        x = text_rect.x() + _STAR_AREA_WIDTH
        # Vertically center the baseline within the row rect.
        baseline = (
            text_rect.y()
            + (text_rect.height() + fm.ascent() - fm.descent()) // 2
        )

        for i, ch in enumerate(display):
            if skip > 0 and i == 0:
                # The ellipsis itself is never a "match".
                is_match = False
            elif skip > 0:
                is_match = (i - 1 + skip) in indices_set
            else:
                is_match = i in indices_set

            painter.setPen(self._match_color if is_match else self._text_color)
            painter.drawText(x, baseline, ch)
            x += char_w

        painter.restore()


class ModelListWidget(QWidget):
    """Scrollable, filterable model browser for CASC SNO paths."""

    model_selected = Signal(str)

    def __init__(
        self,
        settings: AppSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._source_model = QStringListModel(self)
        self._proxy = _CategoryProxyModel(self)
        self._proxy.setSourceModel(self._source_model)

        # Favorites: a session-local set kept in sync with the
        # persistent QSettings store. The delegate gets a reference so
        # paint sees toggles without an extra setter call. Stored as
        # SNO paths (never composed rows) so the on-disk schema doesn't
        # need a migration and so toggles survive label changes.
        self._favorites: set[str] = (
            settings.favorites() if settings is not None else set()
        )

        # Full catalog as loaded — never mutated; the proxy and the
        # source model snapshot subsets of this for display. Stored as
        # raw SNO paths; composed rows are derived from them on demand.
        self._all_entries: list[str] = []
        # Pre-filtered candidate slice (raw SNO paths). Recomputed when
        # the active pill (category or favs) changes and whenever the
        # catalog reloads.
        self._category_filtered: list[str] = []
        self._category_pattern: str | None = None
        # FAVS pill state. Mutually exclusive with the category pills
        # via the shared QButtonGroup, but we still track it as a
        # separate flag because the candidate-set rule for favs is
        # set-membership rather than regex.
        self._favs_active: bool = False

        # SNO path → display label resolver, installed by main_window
        # after the equipment / actor name indices land. ``None`` until
        # wired — rows then fall back to the bare ``.app`` stem.
        self._name_resolver: Callable[[str], str] | None = None

        # Incremental fuzzy cache: query string → (ranked, survivors).
        #   ranked    — top-200 (composed_row, score, indices) for direct
        #               redraw on a backspace-to-cached-prefix.
        #   survivors — every matching composed row (un-truncated). Used
        #               as the candidate set for the next keystroke when
        #               the new query extends a cached one.
        # Capping the cache at the 200 displayed rows would silently
        # drop survivors that fell off the visible list but might still
        # match the longer query, so the cache holds the full set.
        self._fuzzy_cache: dict[
            str,
            tuple[list[tuple[str, int, list[int]]], list[str]],
        ] = {}
        # The last applied query, used to ignore late-arriving worker
        # results from a previous keystroke.
        self._current_query: str = ""
        self._filter_worker: FilterWorker | None = None
        self._delegate = FuzzyMatchDelegate(self)
        # Hand the delegate a live reference to our favorites set so a
        # toggle is reflected on the next paint without a second setter.
        self._delegate.set_favorites(self._favorites)

        # textChanged fires per keystroke; the timer collapses bursts
        # into a single worker spawn after the user pauses for 16ms.
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(_FILTER_DEBOUNCE_MS)
        self._debounce_timer.timeout.connect(self._fire_debounced_filter)
        self._pending_query: str = ""

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        # List + loading-overlay live in the same slot; the stack lets us
        # swap between them without disturbing the surrounding layout.
        self._list_stack = QStackedLayout()
        self._list_stack.setStackingMode(QStackedLayout.StackOne)

        self._list_view = QListView(self)
        self._list_view.setModel(self._proxy)
        self._list_view.setItemDelegate(self._delegate)
        self._list_view.setUniformItemSizes(True)
        self._list_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self._list_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # Mouse tracking propagates State_MouseOver to the delegate
        # without requiring a click — that's what makes the dim ★
        # appear on hover for unstarred rows.
        self._list_view.setMouseTracking(True)
        # Always-off + left-elide keeps long SNO paths from triggering a
        # horizontal scrollbar that pops in/out and fights the splitter
        # during drag. The custom delegate also elides left for the
        # match-highlighted render path.
        self._list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list_view.setTextElideMode(Qt.ElideLeft)
        self._list_view.clicked.connect(self._on_list_clicked)
        # Intercept presses inside the star gutter on the viewport so
        # they toggle favorites instead of bubbling up as a row
        # selection. The viewport is what actually receives mouse
        # events; the QListView wraps it but doesn't see raw button
        # presses on its own.
        self._list_view.viewport().installEventFilter(self)

        self._overlay = QLabel("Scanning CASC archive…", self)
        self._overlay.setObjectName("listOverlay")
        self._overlay.setAlignment(Qt.AlignCenter)

        list_container = QWidget(self)
        list_container.setLayout(self._list_stack)
        self._list_stack.addWidget(self._list_view)   # index 0
        self._list_stack.addWidget(self._overlay)     # index 1
        self._list_stack.setCurrentWidget(self._overlay)
        outer.addWidget(list_container, 1)

        # Status line — total or filtered counts.
        self._status_label = QLabel("", self)
        self._status_label.setObjectName("listStatus")
        outer.addWidget(self._status_label)

        # Quick filter pills. ALL is the default; FAVS sits between ALL
        # and the category pills because it filters by a different
        # axis (set membership, not regex) and we want it discoverable
        # without burying it past every category abbreviation.
        pill_row = QHBoxLayout()
        pill_row.setSpacing(4)
        self._pill_group = QButtonGroup(self)
        self._pill_group.setExclusive(True)
        self._pill_buttons: list[QPushButton] = []

        # ALL pill (first entry of CATEGORY_PILLS).
        all_label, all_pattern = CATEGORY_PILLS[0]
        all_btn = QPushButton(all_label, self)
        all_btn.setObjectName("pill")
        all_btn.setCheckable(True)
        all_btn.setProperty("pattern", all_pattern)
        self._pill_group.addButton(all_btn)
        pill_row.addWidget(all_btn)
        self._pill_buttons.append(all_btn)

        # FAVS pill — different object name so the checked state can
        # use the favorite-yellow palette without affecting other
        # pills. ``pattern`` stays None; the favs filter doesn't go
        # through the regex path.
        self._favs_pill = QPushButton("★ FAVS", self)
        self._favs_pill.setObjectName("favPill")
        self._favs_pill.setCheckable(True)
        self._favs_pill.setProperty("pattern", None)
        self._pill_group.addButton(self._favs_pill)
        pill_row.addWidget(self._favs_pill)
        self._pill_buttons.append(self._favs_pill)

        # Remaining category pills (skip the ALL we already added).
        for label, pattern in CATEGORY_PILLS[1:]:
            btn = QPushButton(label, self)
            btn.setObjectName("pill")
            btn.setCheckable(True)
            btn.setProperty("pattern", pattern)
            self._pill_group.addButton(btn)
            pill_row.addWidget(btn)
            self._pill_buttons.append(btn)

        pill_row.addStretch(1)
        all_btn.setChecked(True)  # ALL is the default
        # buttonClicked only fires for the newly-checked button — what
        # we want; buttonToggled would also fire for the deselected one.
        self._pill_group.buttonClicked.connect(self._on_pill_clicked)
        outer.addLayout(pill_row)

        # Filter text input lives at the very bottom.
        self._filter_input = QLineEdit(self)
        self._filter_input.setPlaceholderText("Filter models…")
        self._filter_input.setClearButtonEnabled(True)
        self._filter_input.textChanged.connect(self._on_filter_text_changed)
        outer.addWidget(self._filter_input)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_entries(self, entries: Iterable[str]) -> None:
        """Populate the list with SNO paths and switch off the loading state."""
        paths = list(entries)
        self._all_entries = paths
        self._fuzzy_cache.clear()
        self._recompute_category_filter()
        self._reapply_current_filter()
        self._list_stack.setCurrentWidget(self._list_view)
        self._update_status_label()

    def set_name_resolver(
        self, resolver: Callable[[str], str] | None,
    ) -> None:
        """Install the SNO-path → display-label resolver.

        ``resolver`` must always return a usable label — the in-game
        item / actor name when known, the SNO stem otherwise. Re-projects
        the current rows so a resolver wired after the catalog loaded
        takes effect immediately.
        """
        self._name_resolver = resolver
        self.refresh_display_names()

    def refresh_display_names(self) -> None:
        """Re-project rows through the resolver and re-render.

        Called when the equipment / actor name index finishes building
        so rows that fell back to the SNO stem pick up real names. The
        fuzzy cache is cleared because its survivors are composed rows
        scored against the *prior* labels — applying them against the
        new rows would silently drop matches.
        """
        self._fuzzy_cache.clear()
        # _reapply_current_filter re-projects whichever subset is
        # currently visible (search results or the unfiltered category
        # pool) through the new resolver via the standard show paths.
        self._reapply_current_filter()

    def show_loading(self, message: str = "Scanning CASC archive…") -> None:
        self._overlay.setText(message)
        self._list_stack.setCurrentWidget(self._overlay)
        self._status_label.setText("")

    def show_error(self, message: str) -> None:
        # Reuse the overlay slot to surface a load failure inline.
        self._overlay.setText(message)
        self._list_stack.setCurrentWidget(self._overlay)
        self._status_label.setText("")

    def focus_filter(self) -> None:
        self._filter_input.setFocus(Qt.ShortcutFocusReason)
        self._filter_input.selectAll()

    def shutdown(self) -> None:
        """Halt any in-flight filter worker before the window closes.

        QThread's C++ side outlives ``deleteLater`` until ``run`` exits,
        so without this the app shutdown can stall on a long fuzzy scan
        or — worse — race the interpreter teardown holding the GIL.
        """
        # Stop the debounce timer first so a queued fire doesn't spawn
        # a new worker after we've cancelled the current one.
        try:
            self._debounce_timer.stop()
        except RuntimeError:
            pass
        self._cancel_filter_worker(wait=True)

    def total_count(self) -> int:
        return len(self._all_entries)

    def visible_count(self) -> int:
        return self._proxy.rowCount()

    # ------------------------------------------------------------------
    # Row composition
    # ------------------------------------------------------------------

    def _compose_row(self, sno_path: str) -> str:
        """Project one SNO path into its composed ``"<label>\\x1f<sno>"`` form."""
        label = ""
        if self._name_resolver is not None:
            try:
                label = self._name_resolver(sno_path)
            except Exception:  # noqa: BLE001 — never break the list
                label = ""
        if not label:
            label = _stem_no_ext(sno_path)
        return f"{label}{_ROW_SEP}{sno_path}"

    def _compose_rows(self, paths: Iterable[str]) -> list[str]:
        """Compose rows and sort by label (case-insensitive).

        Mirrors :class:`PieceBrowser`'s unfiltered ordering — the list
        reads alphabetically by the user-visible name so a glance scan
        is predictable. Search results are independent: the fuzzy
        worker re-ranks by score, so this sort only governs the
        unfiltered view and the worker's scan order, which is harmless.
        """
        rows = [self._compose_row(p) for p in paths]
        rows.sort(key=lambda r: _row_label(r).lower())
        return rows

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_filter_text_changed(self, text: str) -> None:
        # Debounce: reset the 16ms window. Only the last keystroke in
        # the window reaches _fire_debounced_filter and spawns a worker.
        self._pending_query = text
        self._debounce_timer.start()

    def _on_pill_clicked(self, button: QPushButton) -> None:
        # FAVS is in the same exclusive group as the regex pills, so
        # picking one automatically deselects the other; we just have
        # to translate the click into the right mode flags. Clearing
        # the fuzzy cache here is critical — survivors are scored
        # against a specific candidate pool, and that pool just changed.
        if button is self._favs_pill:
            self._favs_active = True
            self._category_pattern = None
        else:
            self._favs_active = False
            pattern = button.property("pattern")
            self._category_pattern = pattern if pattern else None
        self._fuzzy_cache.clear()
        self._recompute_category_filter()
        self._reapply_current_filter()

    def _on_list_clicked(self, proxy_index: QModelIndex) -> None:
        if not proxy_index.isValid():
            return
        value = self._proxy.data(proxy_index, Qt.DisplayRole)
        if not isinstance(value, str) or not value:
            return
        # The row is a composed "<label>\x1f<sno>" string — emit the SNO
        # path half so downstream mesh loaders / texture caches receive
        # the same identity they've always seen.
        sno = _row_sno(value)
        if sno:
            self.model_selected.emit(sno)

    # ------------------------------------------------------------------
    # Favorites
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 — Qt API
        # Only the list view's viewport is filtered (see _build_ui), but
        # be defensive about other senders sneaking through — Qt
        # occasionally re-parents widgets in ways that broaden the
        # filter scope mid-lifetime.
        if obj is self._list_view.viewport() and event.type() == QEvent.MouseButtonPress:
            if event.button() == Qt.LeftButton:
                pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                # The text rect starts a few px in from the row's left
                # edge for the QSS padding; treat the entire 0..gutter
                # band as the star hit area so the user doesn't have to
                # aim precisely at the glyph.
                if pos.x() < _STAR_AREA_WIDTH:
                    proxy_index = self._list_view.indexAt(pos)
                    if proxy_index.isValid():
                        row = self._proxy.data(proxy_index, Qt.DisplayRole)
                        if isinstance(row, str) and row:
                            # Favorites are keyed by SNO path — the
                            # delegate's star-check, the persisted
                            # QSettings store, and the FAVS pill all
                            # look up SNO paths, never composed rows.
                            self._toggle_favorite(_row_sno(row))
                            # Consume the press so it doesn't also
                            # register as a row click and trigger model
                            # loading.
                            return True
        return super().eventFilter(obj, event)

    def _toggle_favorite(self, sno_path: str) -> None:
        if sno_path in self._favorites:
            self._favorites.discard(sno_path)
        else:
            self._favorites.add(sno_path)
        if self._settings is not None:
            self._settings.set_favorites(self._favorites)

        # The delegate holds a live reference to self._favorites so the
        # next paint sees the change; we just need to nudge Qt to redraw
        # the affected rows.
        self._list_view.viewport().update()

        # FAVS mode's candidate set just changed under us. Recompute and
        # re-apply so the row appears/disappears immediately rather than
        # waiting for the next pill click.
        if self._favs_active:
            self._fuzzy_cache.clear()
            self._recompute_category_filter()
            self._reapply_current_filter()
        else:
            # Status line in non-FAVS modes doesn't reflect favorites,
            # but we still want to keep the count consistent if it ever
            # does — cheap to refresh.
            self._update_status_label(
                query=self._current_query or None,
            )

    # ------------------------------------------------------------------
    # Filter pipeline
    # ------------------------------------------------------------------

    def _recompute_category_filter(self) -> None:
        if self._favs_active:
            # Membership-filter — preserve catalog order so the favs
            # list reads predictably between sessions instead of
            # bouncing around with set iteration order.
            self._category_filtered = [
                e for e in self._all_entries if e in self._favorites
            ]
        elif self._category_pattern is None:
            self._category_filtered = list(self._all_entries)
        else:
            rx = re.compile(self._category_pattern, re.IGNORECASE)
            self._category_filtered = [
                e for e in self._all_entries if rx.search(e)
            ]
        # The proxy's regex is used only by the no-query category
        # branch in _show_unfiltered to filter the full source. In
        # FAVS mode we hand it a pre-filtered source instead, so the
        # regex stays None there.
        self._proxy.set_category_regex(
            None if self._favs_active else self._category_pattern,
        )

    def _reapply_current_filter(self) -> None:
        """Re-run the active filter against the current candidate set."""
        if self._current_query:
            # The cached entry (if any) was scored against the prior
            # candidate set; with category having changed under us, we
            # have to re-scan. _fire_debounced_filter handles cache
            # lookup correctly here because the cache was just cleared.
            self._fire_debounced_filter_for(self._current_query)
        else:
            self._show_unfiltered()

    def _fire_debounced_filter(self) -> None:
        # Reads the latest pending value rather than capturing — the
        # last keystroke in the window wins.
        query = self._pending_query
        self._fire_debounced_filter_for(query)

    def _fire_debounced_filter_for(self, query: str) -> None:
        self._current_query = query

        # Treat whitespace-only input the same as empty: multi_token_score
        # would happily return (0, []) for every candidate and we'd waste
        # a worker pass painting the entire list with score 0.
        if not query or not query.strip():
            self._cancel_filter_worker()
            self._show_unfiltered()
            return

        # Direct cache hit: render synchronously with no worker. This is
        # what makes backspacing back into a previously-typed prefix
        # feel instant.
        cached = self._fuzzy_cache.get(query)
        if cached is not None:
            self._cancel_filter_worker()
            ranked, _survivors = cached
            self._apply_fuzzy_results(query, ranked)
            return

        # Refinement: longest cached prefix's survivors are still a
        # superset of the new query's survivors (subsequence matching
        # is monotonic in query length), so we can scan that smaller
        # list instead of every category-filtered entry.
        candidates: list[str] | None = None
        for k in range(len(query) - 1, 0, -1):
            prefix = query[:k]
            cached_prefix = self._fuzzy_cache.get(prefix)
            if cached_prefix is not None:
                _ranked, survivors = cached_prefix
                candidates = survivors
                break

        if candidates is None:
            # The worker scans composed rows so it can match against
            # either the label or the SNO path half. Compose on demand
            # so an unfiltered ALL+empty-query path doesn't pay the
            # composition cost.
            candidates = self._compose_rows(self._category_filtered)

        self._spawn_filter_worker(query, candidates)

    def _spawn_filter_worker(
        self, query: str, candidates: list[str],
    ) -> None:
        # Cancel without waiting; the previous worker will see the
        # interrupt request, exit run(), and deleteLater itself. We
        # also drop its results signal so a slow finish can't overwrite
        # the new query's display.
        self._cancel_filter_worker(wait=False)

        worker = FilterWorker(query, candidates, parent=self)
        worker.results.connect(self._on_filter_results)
        # Worker self-destructs on thread finish — Python parent ref
        # isn't enough since QThread keeps its C++ side alive until
        # finished fires.
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
                # Disconnect the results path so a late emit can't
                # overwrite the active list — the worker itself also
                # checks isInterruptionRequested before emitting, but
                # the GIL gap between those two events is exactly the
                # bug class this guards against.
                try:
                    prior.results.disconnect()
                except (RuntimeError, TypeError):
                    pass
                if wait:
                    prior.quit()
                    prior.wait(2000)
        except RuntimeError:
            # C++ object already gone — nothing to cancel.
            pass
        self._filter_worker = None

    def _clear_worker_ref(self, *_args) -> None:
        # finished may also fire after we already nulled the ref out
        # in _cancel_filter_worker; that's fine — this is idempotent.
        self._filter_worker = None

    def _on_filter_results(
        self,
        query: str,
        ranked: list[tuple[str, int, list[int]]],
        survivors: list[str],
    ) -> None:
        # Stale-result guard: another keystroke landed before this
        # worker finished, and we've already moved on. The fuzzy cache
        # is also bypassed here because the cached entry would be for a
        # query the user has already abandoned.
        if query != self._current_query:
            return
        self._fuzzy_cache[query] = (ranked, survivors)
        self._apply_fuzzy_results(query, ranked)

    def _apply_fuzzy_results(
        self,
        query: str,
        results: list[tuple[str, int, list[int]]],
    ) -> None:
        rows = [r[0] for r in results]
        self._delegate.set_match_indices({r[0]: r[2] for r in results})
        self._source_model.setStringList(rows)
        # Worker results were already filtered by category/favs, so the
        # proxy regex would be a no-op AND would re-do an unnecessary
        # per-row scan. Drop it for the score-ranked list.
        self._proxy.set_category_regex(None)
        # The favs-empty branch in _show_unfiltered may have parked the
        # stack on the overlay (e.g. user typed in an empty FAVS list).
        # Make sure typing a query swings back to the list view even if
        # the worker returned zero rows — they'll see "0 of 0" rather
        # than the empty-favs hint, which is more informative.
        self._list_stack.setCurrentWidget(self._list_view)
        self._update_status_label(query=query)

    def _show_unfiltered(self) -> None:
        """Restore the no-query view (still subject to the active pill)."""
        self._delegate.set_match_indices({})

        if self._favs_active and not self._favorites:
            # Empty favorites is a real state to surface, not a bug —
            # without this the user sees an empty list and no hint
            # about how to populate it.
            self._overlay.setText(
                "No favorites yet. Click ★ on any model to add it.",
            )
            self._list_stack.setCurrentWidget(self._overlay)
            self._source_model.setStringList([])
            self._proxy.set_category_regex(None)
            self._update_status_label()
            return

        if self._favs_active:
            # Source already pre-filtered by membership; proxy stays
            # identity. Going through the regex path would require us
            # to invent a regex that matches every favorite path, which
            # the FAVS axis exists specifically to avoid.
            self._source_model.setStringList(
                self._compose_rows(self._category_filtered),
            )
            self._proxy.set_category_regex(None)
        else:
            # Category-by-regex path: hand the proxy the full source
            # (composed) and let it drop rows whose SNO half doesn't
            # match the active pill.
            self._source_model.setStringList(
                self._compose_rows(self._all_entries),
            )
            self._proxy.set_category_regex(self._category_pattern)

        self._list_stack.setCurrentWidget(self._list_view)
        self._update_status_label()

    # ------------------------------------------------------------------
    # Status line
    # ------------------------------------------------------------------

    def _update_status_label(self, *, query: str | None = None) -> None:
        total = self.total_count()
        if total == 0:
            self._status_label.setText("0 models found.")
            return
        visible = self.visible_count()

        # FAVS mode reports counts against the favorites pool, not the
        # full catalog — "12 of 67,000" would be technically correct
        # but uselessly hides the favorites count behind the catalog.
        if self._favs_active:
            fav_total = len(self._favorites)
            if fav_total == 0:
                self._status_label.setText("No favorites yet.")
            elif query:
                self._status_label.setText(
                    f"{visible:,} of {fav_total:,} favorites."
                )
            else:
                self._status_label.setText(f"{fav_total:,} favorites.")
            return

        # When a fuzzy query is active and we hit the display cap, the
        # status line should hint at it so the user knows there may be
        # more matches behind the top-200 threshold.
        if query:
            if visible >= 200:
                self._status_label.setText(
                    f"Top {visible:,} of {total:,} (refine to narrow)."
                )
            else:
                self._status_label.setText(
                    f"{visible:,} of {total:,} models."
                )
            return
        if visible == total:
            self._status_label.setText(f"{total:,} models found.")
        else:
            self._status_label.setText(
                f"{visible:,} of {total:,} models."
            )
