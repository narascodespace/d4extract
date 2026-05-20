"""Per-submesh visibility list shown in the right panel.

Lists the submeshes of the currently-loaded model, each with a checkbox
that toggles the visibility of its VTK actor in the viewport. The panel
is always present — when no model is loaded it shows a placeholder
message.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from d4extract.formats.hash_names import resolve_hash
from d4extract.gui.theme import Colors

if TYPE_CHECKING:
    from d4extract.formats.app_parser import Submesh
    from d4extract.formats.material_parser import Material


# Pretty labels for the 3-letter dwSlotHash abbreviations recovered in
# d4extract/data/hash_names.json.  Keys match the resolved hash name.
_SLOT_LABELS: dict[str, str] = {
    "hlm": "Helmet",
    "trs": "Torso",
    "leg": "Legs",
    "bts": "Boots",
    "glv": "Gloves",
    "bdy": "Body",
}


_BONE_NAME_PREFIXES: tuple[str, ...] = (
    "Bip0", "Bone_", "bone_", "Joint_", "Jnt_", "jnt_",
    "Hero_", "hero_", "Tail_", "Mecha_",
    "b_", "B_", "L_", "R_", "_l_", "_r_",
    "lt_", "rt_", "left_", "right_", "Left_", "Right_",
)


_MATERIAL_NAME_SUFFIXES: tuple[str, ...] = (
    "_mat", "_Mat", "_MAT",
    "_material", "_Material", "_MATERIAL",
    "_diffuse", "_Diffuse",
)


def _looks_like_bone_name(s: str) -> bool:
    """Reject sub-object 'names' that are actually bone-name shapes.

    The dictionary attack is hash-verified, but its candidate space is
    rigging-vocabulary-heavy, so accidental 32-bit collisions are likely
    to be bone-name-shaped strings rather than real submesh labels.
    """
    return s.startswith(_BONE_NAME_PREFIXES)


def _sanitise_material_name(name: str) -> str:
    """Strip artist-convention boilerplate suffixes from a material name."""
    for suffix in _MATERIAL_NAME_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _submesh_label(
    sm: "Submesh",
    index: int,
    triangle_count: int,
    material_name: str | None = None,
    is_cloth_only: bool = False,
) -> str:
    """Format the row label.

    Priority: material name > resolved sub-object name > slot tag >
    generic "Submesh N".  Bone-shaped sub-object names are treated as
    noise and skipped.  Cloth-only material slots get a "(cloth proxy)"
    tag so users can spot physics-simulation submeshes at a glance.
    Triangle count is always appended.
    """
    head: str
    if material_name:
        head = _sanitise_material_name(material_name)
    elif sm.name and not _looks_like_bone_name(sm.name):
        head = sm.name
    else:
        slot_name = resolve_hash(sm.slot_hash) if sm.slot_hash else None
        slot_pretty = _SLOT_LABELS.get(slot_name, slot_name) if slot_name else None
        head = f"Submesh {index}" + (f" — {slot_pretty}" if slot_pretty else "")
    cloth_tag = " (cloth proxy)" if is_cloth_only else ""
    return f"{head}{cloth_tag}  ({triangle_count:,} tris)"

log = logging.getLogger(__name__)


# Mirror of viewport._SET2_PALETTE — duplicated here so the swatch
# colors stay in sync with the per-submesh actor colors without making
# the widget reach into the viewport module.
_SET2_PALETTE: tuple[str, ...] = (
    "#66c2a5", "#fc8d62", "#8da0cb", "#e78ac3",
    "#a6d854", "#ffd92f", "#e5c494", "#b3b3b3",
)


def _swatch_color(material_index: int) -> str:
    return _SET2_PALETTE[material_index % len(_SET2_PALETTE)]


class _SubmeshRow(QWidget):
    """Single row: checkbox + color swatch + descriptive label."""

    toggled = Signal(int, bool)

    def __init__(
        self,
        submesh_index: int,
        submesh: "Submesh",
        triangle_count: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._submesh_index = submesh_index
        self._submesh = submesh
        self._triangle_count = triangle_count
        self._material_name: str | None = None
        self._is_cloth_only: bool = False
        # Set the first time the row learns it's a cloth proxy. Stops the
        # auto-hide from overriding a manual user toggle on later
        # ``update_label`` calls (e.g. cache refresh after texture worker).
        self._cloth_default_applied: bool = False

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(6)

        self._check = QCheckBox(self)
        self._check.setChecked(True)
        self._check.toggled.connect(self._on_toggled)
        row.addWidget(self._check)

        swatch = QLabel(self)
        swatch.setObjectName("submeshSwatch")
        swatch.setFixedSize(12, 12)
        swatch.setStyleSheet(
            f"background-color: {_swatch_color(submesh.material_index)};"
            f" border: 1px solid {Colors.BORDER}; border-radius: 2px;"
        )
        row.addWidget(swatch)

        self._label = QLabel(self)
        self._label.setObjectName("submeshLabel")
        row.addWidget(self._label, 1)

        self._refresh_label()

    # ------------------------------------------------------------------
    # Label rendering
    # ------------------------------------------------------------------

    def update_label(
        self, material_name: str | None, *, is_cloth_only: bool = False,
    ) -> None:
        """Set the material name + cloth flag and re-render label + tooltip.

        The first time a row learns it's a cloth proxy, the checkbox is
        unchecked so the viewport hides those simulation submeshes by
        default — they're physics cards, almost never what the user
        wants to see.  Subsequent calls don't re-apply the default, so
        the user can show a cloth proxy and have that choice stick.
        """
        self._material_name = material_name or None
        self._is_cloth_only = bool(is_cloth_only)
        self._refresh_label()
        if self._is_cloth_only and not self._cloth_default_applied:
            self._cloth_default_applied = True
            if self._check.isChecked():
                # setChecked(False) fires the toggled signal which
                # propagates through ``visibility_changed`` to the
                # viewport — same path a manual user click would take.
                self._check.setChecked(False)

    def _refresh_label(self) -> None:
        self._label.setText(_submesh_label(
            self._submesh,
            self._submesh_index,
            self._triangle_count,
            self._material_name,
            self._is_cloth_only,
        ))
        self._label.setToolTip(self._build_tooltip())

    def _build_tooltip(self) -> str:
        sm = self._submesh
        lines: list[str] = []
        if self._material_name:
            lines.append(f"material name: {self._material_name}")
        if self._is_cloth_only:
            lines.append("kind: cloth simulation proxy")
        lines += [
            f"index: {self._submesh_index}",
            f"material slot: {sm.material_index}",
            f"triangles: {self._triangle_count:,}",
        ]
        if sm.sub_object_hash:
            resolved = resolve_hash(sm.sub_object_hash)
            tag = f' ("{resolved}")' if resolved else ""
            lines.append(
                f"dwSubObjectHash: 0x{sm.sub_object_hash:08x}{tag}"
            )
        if sm.slot_hash:
            slot_name = resolve_hash(sm.slot_hash)
            slot_pretty = _SLOT_LABELS.get(slot_name, slot_name) if slot_name else None
            tag = f" ({slot_pretty})" if slot_pretty else ""
            lines.append(
                f"dwSlotHash: 0x{sm.slot_hash:08x}{tag}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _on_toggled(self, checked: bool) -> None:
        self.toggled.emit(self._submesh_index, checked)

    def set_checked_silent(self, checked: bool) -> None:
        """Set the checkbox state without emitting :attr:`toggled`."""
        self._check.blockSignals(True)
        try:
            self._check.setChecked(checked)
        finally:
            self._check.blockSignals(False)


class SubmeshListWidget(QFrame):
    """Right-panel list of per-submesh visibility controls.

    Emits :attr:`visibility_changed` whenever a single row's checkbox is
    toggled by the user. The Show All / Hide All button drives the
    viewport directly via the bulk handler attached by the main window —
    so toggling N rows does not produce N separate ``visibility_changed``
    emissions.
    """

    visibility_changed = Signal(int, bool)
    bulk_visibility_changed = Signal(bool)

    PLACEHOLDER = "No model loaded"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("submeshList")
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            "QFrame#submeshList {"
            f"  background-color: {Colors.SURFACE};"
            f"  border-left: 1px solid {Colors.BORDER};"
            "}"
            "QFrame#submeshList QLabel#submeshHeader {"
            f"  color: {Colors.OVERLAY};"
            "  font-size: 11px;"
            "  font-weight: 600;"
            "  padding: 8px 10px 4px 10px;"
            "}"
            "QFrame#submeshList QLabel#submeshPlaceholder {"
            f"  color: {Colors.SUBTEXT};"
            "  font-size: 11px;"
            "  padding: 8px 10px;"
            "}"
            "QFrame#submeshList QLabel#submeshLabel {"
            f"  color: {Colors.TEXT};"
            "  font-size: 11px;"
            "}"
            "QFrame#submeshList QScrollArea {"
            "  background: transparent;"
            "  border: none;"
            "}"
            "QFrame#submeshList QWidget#submeshRowsHost {"
            "  background: transparent;"
            "}"
        )

        self._rows: list[_SubmeshRow] = []
        self._all_visible = True

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QLabel("Submeshes", self)
        header.setObjectName("submeshHeader")
        outer.addWidget(header)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._rows_host = QWidget(scroll)
        self._rows_host.setObjectName("submeshRowsHost")
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(4, 2, 4, 4)
        self._rows_layout.setSpacing(2)

        self._placeholder = QLabel(self.PLACEHOLDER, self._rows_host)
        self._placeholder.setObjectName("submeshPlaceholder")
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
        """Remove all rows and show the placeholder."""
        for row in self._rows:
            self._rows_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        self._all_visible = True
        self._toggle_all.setEnabled(False)
        self._toggle_all.setText("Hide All")
        self._placeholder.show()

    def get_visible_indices(self) -> set[int] | None:
        """Indices of currently-checked submeshes, or ``None`` for "all".

        Returning ``None`` when every row is checked lets callers skip
        the filter entirely on the hot path — same code as before this
        feature existed.
        """
        if not self._rows:
            return None
        visible = {r._submesh_index for r in self._rows if r._check.isChecked()}
        if len(visible) == len(self._rows):
            return None
        return visible

    def populate(self, submeshes: list["Submesh"]) -> None:
        """Rebuild the row list from ``submeshes``."""
        self.clear()
        if not submeshes:
            return

        self._placeholder.hide()
        # Insert rows above the trailing stretch (index = count - 1).
        insert_at = self._rows_layout.count() - 1
        for sm_idx, sm in enumerate(submeshes):
            tris = max(0, sm.index_count) // 3
            row = _SubmeshRow(
                submesh_index=sm_idx,
                submesh=sm,
                triangle_count=tris,
                parent=self._rows_host,
            )
            row.toggled.connect(self._on_row_toggled)
            self._rows_layout.insertWidget(insert_at, row)
            insert_at += 1
            self._rows.append(row)

        self._toggle_all.setEnabled(True)
        self._all_visible = True
        self._toggle_all.setText("Hide All")

    def update_material_names(self, materials: list["Material"] | None) -> None:
        """Re-label each row with its resolved material name.

        Called once the texture worker (or a cached MeshData) has
        attached ``mesh_data.materials``.  Rows whose ``material_index``
        is out of range — partial resolution leaves a shorter list — get
        their fallback label instead.  Safe to call multiple times.
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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_row_toggled(self, sm_idx: int, visible: bool) -> None:
        self.visibility_changed.emit(sm_idx, visible)
        # Keep the bulk button label in sync with the rows. If the user
        # toggles individual rows down to "everything hidden" the next
        # bulk press should read "Show All".
        any_visible = any(r._check.isChecked() for r in self._rows)
        self._all_visible = any_visible
        self._toggle_all.setText("Hide All" if any_visible else "Show All")

    def _on_toggle_all(self) -> None:
        target_visible = not self._all_visible
        for row in self._rows:
            row.set_checked_silent(target_visible)
        self._all_visible = target_visible
        self._toggle_all.setText("Hide All" if target_visible else "Show All")
        self.bulk_visibility_changed.emit(target_visible)
