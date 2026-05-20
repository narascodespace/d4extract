"""Assembly Parts panel — grouped per-piece submesh visibility controls.

Sits to the right of the piece browser inside the Character Builder
splitter. Each currently-equipped piece becomes a collapsible group;
inside the group, one row per submesh mirrors the per-piece swatch +
checkbox UI that the Model Browser's :class:`SubmeshListWidget` uses,
re-using ``_SubmeshRow`` for visual parity.

Routing is the interesting bit. Skinned pieces have been merged into
one ``MeshData`` whose submesh indices are globally rebased, so a row
toggle for a skinned piece needs to translate the local submesh index
to the merged-stream's global index before reaching the viewport.
Static (overlay) pieces — weapons, props — keep their own actor
namespace under a tag, so a row toggle for an overlay piece needs to
carry both the tag and the local submesh index.

The panel emits two signal pairs (single + bulk) for each routing
case, and the builder page wires them to the viewport's per-submesh
visibility methods.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from d4extract.gui.theme import Colors
from d4extract.gui.widgets.submesh_list import _SubmeshRow

if TYPE_CHECKING:
    from d4extract.formats.app_parser import Submesh
    from d4extract.formats.material_parser import Material


log = logging.getLogger(__name__)


@dataclass
class PieceEntry:
    """One equipped piece's contribution to the assembly.

    ``submesh_offset`` is the cumulative submesh count that came before
    this piece in the merged stream (always 0 for overlay pieces, since
    each overlay has its own actor namespace).
    """

    display_name: str
    sno_path: str
    submeshes: list["Submesh"] = field(default_factory=list)
    target_type: Literal["merged", "overlay"] = "merged"
    submesh_offset: int = 0
    overlay_tag: str | None = None


class _PieceGroup(QWidget):
    """One collapsible piece-section in the panel.

    Header row hosts a tri-state checkbox (toggles every child row), a
    disclosure triangle (collapses the body without affecting
    visibility), and the piece label. Body is a vertical stack of
    ``_SubmeshRow`` widgets keyed on the piece-local submesh index.
    """

    # Local submesh index + new visibility — the panel translates these
    # into either merged (global) or overlay-tagged signals.
    row_visibility_changed = Signal(int, bool)
    bulk_visibility_changed = Signal(bool)

    def __init__(self, entry: PieceEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entry = entry
        self._rows: list[_SubmeshRow] = []
        # Suppress header re-sync while the bulk path is mass-toggling
        # the children — otherwise every silent set still walks the
        # state computation N times.
        self._suppress_header_sync = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())
        outer.addWidget(self._build_body(), 1)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_header(self) -> QWidget:
        header = QWidget(self)
        header.setObjectName("partsGroupHeader")
        row = QHBoxLayout(header)
        row.setContentsMargins(4, 4, 4, 2)
        row.setSpacing(6)

        self._header_check = QCheckBox(header)
        self._header_check.setChecked(True)
        # ``setTristate(False)`` keeps user clicks on a partial-state
        # checkbox from cycling into the partial state again — clicks
        # always resolve to either fully-on or fully-off, while
        # programmatic ``setCheckState(Qt.PartiallyChecked)`` from our
        # own sync path still renders the partial glyph.
        self._header_check.setTristate(False)
        self._header_check.clicked.connect(self._on_header_clicked)
        row.addWidget(self._header_check)

        self._disclosure = QToolButton(header)
        self._disclosure.setObjectName("partsGroupDisclosure")
        self._disclosure.setText("▾")
        self._disclosure.setAutoRaise(True)
        self._disclosure.setCursor(Qt.PointingHandCursor)
        self._disclosure.clicked.connect(self._on_disclosure_clicked)
        row.addWidget(self._disclosure)

        self._title = QLabel(self._entry.display_name, header)
        self._title.setObjectName("partsGroupTitle")
        self._title.setToolTip(self._entry.sno_path)
        row.addWidget(self._title, 1)

        return header

    def _build_body(self) -> QWidget:
        self._body = QWidget(self)
        self._body.setObjectName("partsGroupBody")
        col = QVBoxLayout(self._body)
        col.setContentsMargins(20, 0, 4, 4)
        col.setSpacing(2)

        for local_idx, sm in enumerate(self._entry.submeshes):
            tris = max(0, sm.index_count) // 3
            row = _SubmeshRow(
                submesh_index=local_idx,
                submesh=sm,
                triangle_count=tris,
                parent=self._body,
            )
            row.toggled.connect(self._on_row_toggled)
            col.addWidget(row)
            self._rows.append(row)

        return self._body

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def entry(self) -> PieceEntry:
        return self._entry

    @property
    def sno_path(self) -> str:
        return self._entry.sno_path

    def update_material_names(
        self, materials: list["Material"] | None,
    ) -> None:
        """Re-label rows once the texture worker resolves materials.

        Mirrors :meth:`SubmeshListWidget.update_material_names` — rows
        whose ``material_index`` overshoots the materials list keep
        their fallback label. The first cloth-only update for each row
        also auto-unchecks the row, so the cloth proxy auto-hide
        behaviour from the Model Browser carries over to the builder.
        """
        if not self._rows:
            return
        n = len(materials) if materials else 0
        for row in self._rows:
            mat_idx = row._submesh.material_index
            mat_name: str | None = None
            cloth_only = False
            if materials and 0 <= mat_idx < n:
                mat = materials[mat_idx]
                mat_name = getattr(mat, "name", None) or None
                cloth_only = bool(getattr(mat, "is_cloth_only", False))
            row.update_label(mat_name, is_cloth_only=cloth_only)

    def get_visible_local_indices(self) -> set[int] | None:
        """Local submesh indices currently visible, or ``None`` for all."""
        if not self._rows:
            return None
        visible = {
            i for i, r in enumerate(self._rows) if r._check.isChecked()
        }
        if len(visible) == len(self._rows):
            return None
        return visible

    def set_all_silent(self, visible: bool) -> None:
        """Force every child row's checkbox to ``visible`` without signals."""
        self._suppress_header_sync = True
        try:
            for row in self._rows:
                row.set_checked_silent(visible)
        finally:
            self._suppress_header_sync = False
        self._sync_header_state()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_row_toggled(self, local_idx: int, visible: bool) -> None:
        if not self._suppress_header_sync:
            self._sync_header_state()
        self.row_visibility_changed.emit(local_idx, visible)

    def _on_header_clicked(self, _checked: bool) -> None:
        # ``checked`` from QCheckBox.clicked reflects the post-click
        # state; with tristate disabled it's strictly bool. Apply that
        # to every child silently and emit one bulk signal so the
        # viewport gets a single render instead of N.
        target = self._header_check.isChecked()
        self._suppress_header_sync = True
        try:
            for row in self._rows:
                row.set_checked_silent(target)
        finally:
            self._suppress_header_sync = False
        # Header is already in the target state from the click; no
        # further re-sync needed.
        self.bulk_visibility_changed.emit(target)

    def _on_disclosure_clicked(self) -> None:
        new_visible = not self._body.isVisible()
        self._body.setVisible(new_visible)
        self._disclosure.setText("▾" if new_visible else "▸")

    def _sync_header_state(self) -> None:
        if not self._rows:
            return
        states = [r._check.isChecked() for r in self._rows]
        if all(states):
            new_state = Qt.Checked
        elif any(states):
            new_state = Qt.PartiallyChecked
        else:
            new_state = Qt.Unchecked
        with QSignalBlocker(self._header_check):
            self._header_check.setCheckState(new_state)


class AssemblyPartsPanel(QFrame):
    """Right-panel grouping of every assembled piece's submesh toggles.

    Bulk signals are emitted as one event per affected target so the
    viewport renders once per bulk action instead of once per row.
    """

    merged_visibility_changed = Signal(int, bool)  # global submesh idx
    overlay_visibility_changed = Signal(str, int, bool)  # tag, local idx
    merged_bulk_visibility_changed = Signal(bool)
    overlay_bulk_visibility_changed = Signal(str, bool)  # tag

    PLACEHOLDER = "Equip pieces to see assembly parts"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("assemblyPartsPanel")
        self.setFrameShape(QFrame.NoFrame)
        # The QSS mirrors SubmeshListWidget's so the two panels read as
        # the same family of control. The header / title styling is a
        # touch heavier (bold, slightly larger) so groups visually
        # separate from each other and from the surrounding chrome.
        self.setStyleSheet(
            "QFrame#assemblyPartsPanel {"
            f"  background-color: {Colors.SURFACE};"
            f"  border-left: 1px solid {Colors.BORDER};"
            "}"
            "QFrame#assemblyPartsPanel QLabel#partsHeader {"
            f"  color: {Colors.OVERLAY};"
            "  font-size: 11px;"
            "  font-weight: 600;"
            "  padding: 8px 10px 4px 10px;"
            "}"
            "QFrame#assemblyPartsPanel QLabel#partsPlaceholder {"
            f"  color: {Colors.SUBTEXT};"
            "  font-size: 11px;"
            "  padding: 8px 10px;"
            "}"
            "QFrame#assemblyPartsPanel QLabel#partsGroupTitle {"
            f"  color: {Colors.TEXT};"
            "  font-size: 12px;"
            "  font-weight: 600;"
            "}"
            "QFrame#assemblyPartsPanel QLabel#submeshLabel {"
            f"  color: {Colors.TEXT};"
            "  font-size: 11px;"
            "}"
            "QFrame#assemblyPartsPanel QToolButton#partsGroupDisclosure {"
            f"  color: {Colors.SUBTEXT};"
            "  border: none;"
            "  padding: 0 2px;"
            "  font-size: 11px;"
            "}"
            "QFrame#assemblyPartsPanel QWidget#partsGroupHeader {"
            f"  background-color: {Colors.HOVER};"
            f"  border: 1px solid {Colors.BORDER};"
            "  border-radius: 4px;"
            "}"
            "QFrame#assemblyPartsPanel QWidget#partsGroupBody {"
            "  background: transparent;"
            "}"
            "QFrame#assemblyPartsPanel QScrollArea {"
            "  background: transparent;"
            "  border: none;"
            "}"
            "QFrame#assemblyPartsPanel QWidget#partsRowsHost {"
            "  background: transparent;"
            "}"
        )

        self._groups: list[_PieceGroup] = []
        self._all_visible = True

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QLabel("Assembly Parts", self)
        header.setObjectName("partsHeader")
        outer.addWidget(header)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._rows_host = QWidget(scroll)
        self._rows_host.setObjectName("partsRowsHost")
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(4, 2, 4, 4)
        self._rows_layout.setSpacing(6)

        self._placeholder = QLabel(self.PLACEHOLDER, self._rows_host)
        self._placeholder.setObjectName("partsPlaceholder")
        self._placeholder.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._rows_layout.addWidget(self._placeholder)
        self._rows_layout.addStretch(1)

        scroll.setWidget(self._rows_host)
        outer.addWidget(scroll, 1)

        self._toggle_all = QPushButton("Hide All", self)
        self._toggle_all.setObjectName("pill")
        self._toggle_all.setCursor(Qt.PointingHandCursor)
        self._toggle_all.setEnabled(False)
        self._toggle_all.clicked.connect(self._on_toggle_all)
        button_row = QHBoxLayout()
        button_row.setContentsMargins(8, 6, 8, 8)
        button_row.addStretch(1)
        button_row.addWidget(self._toggle_all)
        button_row.addStretch(1)
        outer.addLayout(button_row)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Remove every group and restore the empty placeholder."""
        for group in self._groups:
            self._rows_layout.removeWidget(group)
            group.setParent(None)
            group.deleteLater()
        self._groups = []
        self._all_visible = True
        self._toggle_all.setEnabled(False)
        self._toggle_all.setText("Hide All")
        self._placeholder.show()

    def populate(self, pieces: list[PieceEntry]) -> None:
        """Rebuild every group from scratch based on ``pieces``.

        The order of ``pieces`` determines the visual order. Pieces
        with no submeshes are skipped — they'd render as empty groups
        which is just chrome noise.
        """
        self.clear()
        usable = [p for p in pieces if p.submeshes]
        if not usable:
            return

        self._placeholder.hide()
        # Insert groups above the trailing stretch.
        insert_at = self._rows_layout.count() - 1
        for entry in usable:
            group = _PieceGroup(entry, parent=self._rows_host)
            group.row_visibility_changed.connect(
                lambda local_idx, visible, g=group:
                self._on_group_row_changed(g, local_idx, visible),
            )
            group.bulk_visibility_changed.connect(
                lambda visible, g=group:
                self._on_group_bulk_changed(g, visible),
            )
            self._rows_layout.insertWidget(insert_at, group)
            insert_at += 1
            self._groups.append(group)

        self._toggle_all.setEnabled(True)
        self._all_visible = True
        self._toggle_all.setText("Hide All")

    def update_material_names(
        self, sno_path: str, materials: list["Material"] | None,
    ) -> None:
        """Re-label rows in every group whose SNO path matches.

        Multiple groups can share an SNO path in theory (e.g. shared
        payload aliasing across slots); update them all so the labels
        and cloth-proxy auto-hide stay in sync.
        """
        for group in self._groups:
            if group.sno_path == sno_path:
                group.update_material_names(materials)

    def get_visibility_map(self) -> dict[str, set[int] | None]:
        """Per-piece visible local-submesh indices (None = all visible).

        Keyed by SNO path. Used by the export phase to filter out
        hidden submeshes per piece. Multiple groups sharing a path
        merge into the intersection of visible indices — a submesh is
        visible only if every group that includes it has it on.
        """
        out: dict[str, set[int] | None] = {}
        for group in self._groups:
            visible = group.get_visible_local_indices()
            existing = out.get(group.sno_path, "missing")
            if existing == "missing":
                out[group.sno_path] = visible
                continue
            # Intersection semantics: None ("all") yields whatever the
            # other side has; otherwise take set-intersection.
            if visible is None:
                continue
            if existing is None:
                out[group.sno_path] = visible
            else:
                out[group.sno_path] = existing & visible
        return out

    # ------------------------------------------------------------------
    # Group → panel signal translation
    # ------------------------------------------------------------------

    def _on_group_row_changed(
        self, group: _PieceGroup, local_idx: int, visible: bool,
    ) -> None:
        entry = group.entry
        if entry.target_type == "merged":
            global_idx = entry.submesh_offset + local_idx
            self.merged_visibility_changed.emit(global_idx, visible)
        else:
            tag = entry.overlay_tag or ""
            if tag:
                self.overlay_visibility_changed.emit(tag, local_idx, visible)
        self._refresh_bulk_button()

    def _on_group_bulk_changed(
        self, group: _PieceGroup, visible: bool,
    ) -> None:
        entry = group.entry
        if entry.target_type == "merged":
            # The bulk header on a merged piece only spans its own
            # submeshes — fan out per-row so the viewport hides exactly
            # this piece's slice without touching siblings.
            for local_idx in range(len(entry.submeshes)):
                global_idx = entry.submesh_offset + local_idx
                self.merged_visibility_changed.emit(global_idx, visible)
        else:
            tag = entry.overlay_tag or ""
            if tag:
                self.overlay_bulk_visibility_changed.emit(tag, visible)
        self._refresh_bulk_button()

    # ------------------------------------------------------------------
    # Panel-level Show All / Hide All
    # ------------------------------------------------------------------

    def _on_toggle_all(self) -> None:
        target = not self._all_visible
        # Coalesce: silence every group while we flip them, then emit
        # one merged_bulk + one overlay_bulk per overlay tag so the
        # viewport renders once per affected actor namespace.
        any_merged = False
        overlay_tags: list[str] = []
        for group in self._groups:
            group.set_all_silent(target)
            entry = group.entry
            if entry.target_type == "merged":
                any_merged = True
            elif entry.overlay_tag:
                overlay_tags.append(entry.overlay_tag)

        if any_merged:
            self.merged_bulk_visibility_changed.emit(target)
        for tag in overlay_tags:
            self.overlay_bulk_visibility_changed.emit(tag, target)

        self._all_visible = target
        self._toggle_all.setText("Hide All" if target else "Show All")

    def _refresh_bulk_button(self) -> None:
        any_visible = any(
            r._check.isChecked()
            for g in self._groups
            for r in g._rows
        )
        self._all_visible = any_visible
        self._toggle_all.setText("Hide All" if any_visible else "Show All")
