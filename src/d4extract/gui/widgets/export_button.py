"""Bottom-bar export button with an attached options menu."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QMenu,
    QToolButton,
    QWidget,
)

from d4extract.gui.settings import AppSettings


# (option key, display label, options-dict key, default value).
# ``key`` is stable for QSettings storage; ``kwarg`` is the key the
# option lands under in the export options dict — a GltfExporter
# constructor kwarg for most, but consumed by main_window for some
# (``include_animations`` gates whether animations are decoded/embedded
# rather than reaching GltfExporter). Order matches the menu layout.
@dataclass(frozen=True)
class _Option:
    key: str
    label: str
    kwarg: str
    default: bool


EXPORT_OPTIONS: tuple[_Option, ...] = (
    _Option("embed_textures",          "Embed Textures",          "embed_textures",          True),
    _Option("export_skin",             "Include Skeleton",        "export_skin",             True),
    _Option("include_animations",      "Include Animations",      "include_animations",      True),
    _Option("export_normals",          "Include Normals",         "export_normals",          True),
    _Option("export_tangents",         "Include Tangents",        "export_tangents",         True),
    _Option("prune_bones",             "Prune Unused Bones",      "prune_bones",             False),
    _Option("write_materials_sidecar", "Write Materials Sidecar", "write_materials_sidecar", True),
)

# Coordinate transform radio entries: (label, GltfExporter coordinate_transform value).
COORD_TRANSFORMS: tuple[tuple[str, str], ...] = (
    ("Y-Up (glTF standard)", "z_up_to_y_up"),
    ("Z-Up (raw D4)", "none"),
)

# Format choices for the primary action(s).
FMT_GLB = "glb"
FMT_GLTF = "gltf"


class ExportButton(QWidget):
    """Composite export button: primary action + dropdown options menu.

    The primary button triggers an export with the currently saved
    options; the dropdown arrow opens the option menu. ``set_enabled``
    follows whether a model is currently loaded; ``set_busy`` follows
    whether an export is in progress.
    """

    # Emitted when the user triggers an export. Payload is a dict that
    # always contains ``format`` (``"glb"`` or ``"gltf"``) plus every
    # GltfExporter constructor kwarg the user selected. The main window
    # handles the file dialog, the visible-submesh filter, and the
    # worker spawn.
    export_requested = Signal(dict)
    d4data_path_changed = Signal(object)  # Path or None

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._enabled = False
        self._busy = False
        self._option_actions: dict[str, QAction] = {}
        self._coord_action_group: QActionGroup | None = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        row = QHBoxLayout(self)
        # Right margin keeps the button from sitting flush against the
        # status bar's edge — addPermanentWidget pins it to the far
        # right, and QStatusBar's padding doesn't reliably push child
        # widgets inward.
        row.setContentsMargins(0, 0, 10, 0)
        row.setSpacing(0)

        self._primary = QToolButton(self)
        self._primary.setObjectName("exportPrimary")
        self._primary.setText("Export Selected")
        self._primary.setToolTip(
            "Export the currently-selected submeshes as .glb (Ctrl+E)",
        )
        self._primary.setPopupMode(QToolButton.DelayedPopup)
        self._primary.clicked.connect(self._on_primary_clicked)
        row.addWidget(self._primary)

        self._arrow = QToolButton(self)
        self._arrow.setObjectName("exportArrow")
        self._arrow.setText("▾")
        self._arrow.setToolTip("Export options")
        self._arrow.setPopupMode(QToolButton.InstantPopup)
        self._menu = self._build_menu()
        self._arrow.setMenu(self._menu)
        row.addWidget(self._arrow)

        self.set_enabled(False)

    def _build_menu(self) -> QMenu:
        menu = QMenu(self)

        export_glb = menu.addAction("Export as GLB")
        export_glb.triggered.connect(lambda: self._emit_export(FMT_GLB))
        export_gltf = menu.addAction("Export as glTF")
        export_gltf.triggered.connect(lambda: self._emit_export(FMT_GLTF))

        menu.addSeparator()

        for opt in EXPORT_OPTIONS:
            action = QAction(opt.label, menu)
            action.setCheckable(True)
            action.setChecked(self._settings.export_option(opt.key, opt.default))
            action.toggled.connect(self._make_option_handler(opt.key))
            menu.addAction(action)
            self._option_actions[opt.key] = action

        menu.addSeparator()

        coord_menu = menu.addMenu("Coordinate System")
        self._coord_action_group = QActionGroup(self)
        self._coord_action_group.setExclusive(True)
        current_coord = self._settings.coordinate_transform()
        for label, value in COORD_TRANSFORMS:
            action = QAction(label, coord_menu)
            action.setCheckable(True)
            action.setData(value)
            action.setChecked(value == current_coord)
            self._coord_action_group.addAction(action)
            coord_menu.addAction(action)
        self._coord_action_group.triggered.connect(self._on_coord_triggered)

        menu.addSeparator()

        d4data_action = menu.addAction("Set D4Data Path…")
        d4data_action.triggered.connect(self._on_set_d4data)

        return menu

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._update_widget_state()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._primary.setText("Exporting…" if busy else "Export Selected")
        self._update_widget_state()

    def get_export_options(self, *, format: str = FMT_GLB) -> dict:
        """Read every option into a kwargs-shaped dict for the worker.

        Always includes ``format`` (consumed by main_window for the file
        dialog filter and extension); the rest map 1:1 to GltfExporter
        constructor kwargs.
        """
        opts: dict[str, object] = {"format": format}
        for opt in EXPORT_OPTIONS:
            action = self._option_actions[opt.key]
            opts[opt.kwarg] = bool(action.isChecked())
        opts["coordinate_transform"] = self._settings.coordinate_transform()
        # Cloth inclusion is now governed by submesh checkboxes — the
        # GUI defaults cloth proxies to hidden, so anything still visible
        # at export time is something the user explicitly wants. Pass
        # ``include_cloth=True`` so GltfExporter doesn't second-guess
        # the visibility filter and drop cloth submeshes anyway.
        opts["include_cloth"] = True
        return opts

    def open_set_d4data_dialog(self) -> Path | None:
        """Modal directory picker. Returns the picked path, or None if cancelled."""
        start = ""
        existing = self._settings.d4data_path()
        if existing is not None:
            start = str(existing)
        chosen = QFileDialog.getExistingDirectory(
            self, "Select D4Data 'json/' directory", start,
        )
        if not chosen:
            return None
        path = Path(chosen)
        self._settings.set_d4data_path(path)
        self.d4data_path_changed.emit(path)
        return path

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _update_widget_state(self) -> None:
        clickable = self._enabled and not self._busy
        self._primary.setEnabled(clickable)
        self._arrow.setEnabled(clickable)

    def _make_option_handler(self, key: str) -> Callable[[bool], None]:
        # Bind ``key`` so the closure remembers which option it owns.
        def _handler(checked: bool) -> None:
            self._settings.set_export_option(key, checked)
        return _handler

    def _on_coord_triggered(self, action: QAction) -> None:
        value = action.data()
        if isinstance(value, str):
            self._settings.set_coordinate_transform(value)

    def _on_set_d4data(self) -> None:
        self.open_set_d4data_dialog()

    def _on_primary_clicked(self) -> None:
        self._emit_export(FMT_GLB)

    def _emit_export(self, fmt: str) -> None:
        if not self._enabled or self._busy:
            return
        opts = self.get_export_options(format=fmt)
        self.export_requested.emit(opts)
