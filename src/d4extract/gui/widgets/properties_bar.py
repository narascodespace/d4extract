"""Inline properties strip shown along the bottom of the viewport."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
)

if TYPE_CHECKING:
    from d4extract.formats.app_parser import MeshData


class PropertiesBar(QFrame):
    """Compact info strip under the viewport.

    Shows the current model's name and key stats. The widget is always
    present; ``clear()`` resets it to a placeholder. ``update_from`` is
    named to avoid colliding with ``QWidget.update()``.
    """

    PLACEHOLDER = "No model loaded"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("propertiesBar")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Semi-transparent dark band that floats over the viewport.
        self.setStyleSheet(
            "QFrame#propertiesBar {"
            "  background-color: rgba(30, 30, 46, 200);"
            "  border-top: 1px solid #45475a;"
            "}"
            "QFrame#propertiesBar QLabel {"
            "  background: transparent;"
            "  color: #cdd6f4;"
            "  font-size: 11px;"
            "}"
            "QFrame#propertiesBar QLabel#propsName {"
            "  color: #cdd6f4;"
            "  font-weight: 600;"
            "  font-size: 12px;"
            "}"
            "QFrame#propertiesBar QLabel#propsKey {"
            "  color: #6c7086;"
            "}"
            "QFrame#propertiesBar QLabel#propsValue {"
            "  color: #a6adc8;"
            "}"
        )

        self._build_ui()
        self.clear()

    def _build_ui(self) -> None:
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(16)

        self._name_label = QLabel(self.PLACEHOLDER, self)
        self._name_label.setObjectName("propsName")
        self._name_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )

        row.addWidget(self._name_label)

        self._verts_value = self._make_field(row, "Vertices")
        self._tris_value = self._make_field(row, "Triangles")
        self._submesh_value = self._make_field(row, "Submeshes")
        self._bones_value = self._make_field(row, "Bones")
        self._format_value = self._make_field(row, "Format")

        row.addStretch(1)

    def _make_field(self, row: QHBoxLayout, label_text: str) -> QLabel:
        key = QLabel(label_text, self)
        key.setObjectName("propsKey")
        value = QLabel("—", self)
        value.setObjectName("propsValue")
        row.addWidget(key)
        row.addWidget(value)
        return value

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def clear(self) -> None:
        self._name_label.setText(self.PLACEHOLDER)
        for v in (
            self._verts_value, self._tris_value, self._submesh_value,
            self._bones_value, self._format_value,
        ):
            v.setText("—")

    def update_from(self, mesh_data: "MeshData") -> None:
        name = getattr(mesh_data, "name", None) or "Unknown"
        self._name_label.setText(name)

        n_verts = len(mesh_data.positions)
        n_tris = len(mesh_data.indices) // 3
        n_subs = len(mesh_data.submeshes) or getattr(mesh_data, "submesh_count", 0)
        skeleton = getattr(mesh_data, "skeleton", None)
        is_skinned = skeleton is not None and bool(getattr(skeleton, "bones", None))

        self._verts_value.setText(f"{n_verts:,}")
        self._tris_value.setText(f"{n_tris:,}")
        self._submesh_value.setText(f"{n_subs:,}")
        self._bones_value.setText(
            f"{len(skeleton.bones):,}" if is_skinned else "Static"
        )
        self._format_value.setText("Skinned" if is_skinned else "Static")
