"""Welcome / game-directory setup card shown on first launch."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from d4extract.gui.settings import AppSettings


# Common Windows install locations probed before showing the file dialog.
_AUTO_DETECT_PATHS: tuple[Path, ...] = (
    Path("C:/Program Files (x86)/Steam/steamapps/common/Diablo IV"),
    Path("C:/Program Files/Steam/steamapps/common/Diablo IV"),
    Path("C:/Program Files (x86)/Diablo IV"),
    Path("C:/Program Files/Diablo IV"),
)


def is_valid_d4_dir(path: Path) -> bool:
    """A D4 install has either a ``Data/`` subdir (Steam) or ``.build.info`` (Battle.net)."""
    if not path.is_dir():
        return False
    return (path / "Data").is_dir() or (path / ".build.info").is_file()


def auto_detect_d4_dir() -> Path | None:
    for candidate in _AUTO_DETECT_PATHS:
        if is_valid_d4_dir(candidate):
            return candidate
    return None


class SetupCard(QWidget):
    """Centered welcome card for selecting the Diablo IV install directory."""

    directory_selected = Signal(Path)

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._detected_path: Path | None = None
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Outer layout centers the card horizontally and vertically.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 40, 40, 40)
        outer.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)

        self._card = QFrame(self)
        self._card.setObjectName("card")
        self._card.setProperty("clickable", True)
        self._card.setMaximumWidth(600)
        self._card.setMinimumWidth(420)
        self._card.setCursor(Qt.PointingHandCursor)
        # Forward clicks anywhere on the card to _on_card_clicked.
        self._card.mousePressEvent = self._card_mouse_press  # type: ignore[assignment]

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(32, 28, 32, 28)
        card_layout.setSpacing(8)

        self._title = QLabel("Open Diablo IV Installation", self._card)
        self._title.setObjectName("cardTitle")

        self._subtitle = QLabel(
            "Browse to your local D4 install folder", self._card,
        )
        self._subtitle.setObjectName("cardSubtitle")
        self._subtitle.setWordWrap(True)

        self._last_opened = QLabel("", self._card)
        self._last_opened.setObjectName("cardMuted")
        self._last_opened.setWordWrap(True)
        self._last_opened.hide()

        self._detected = QLabel("", self._card)
        self._detected.setObjectName("cardDetected")
        self._detected.setWordWrap(True)
        self._detected.hide()

        self._error = QLabel("", self._card)
        self._error.setObjectName("cardError")
        self._error.setWordWrap(True)
        self._error.hide()

        # Action row sits below the labels. The "Use this installation"
        # button only appears when auto-detection succeeded.
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 12, 0, 0)
        action_row.setSpacing(8)

        self._use_detected_btn = QPushButton("Use this installation", self._card)
        self._use_detected_btn.setObjectName("primary")
        self._use_detected_btn.clicked.connect(self._on_use_detected_clicked)
        self._use_detected_btn.hide()

        self._browse_btn = QPushButton("Browse…", self._card)
        self._browse_btn.clicked.connect(self._on_browse_clicked)

        action_row.addWidget(self._use_detected_btn)
        action_row.addWidget(self._browse_btn)
        action_row.addStretch(1)

        card_layout.addWidget(self._title)
        card_layout.addWidget(self._subtitle)
        card_layout.addWidget(self._last_opened)
        card_layout.addWidget(self._detected)
        card_layout.addWidget(self._error)
        card_layout.addLayout(action_row)

        row.addWidget(self._card)
        row.addStretch(1)

        outer.addLayout(row)
        outer.addStretch(1)

    # ------------------------------------------------------------------
    # State refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read settings and re-run auto-detection."""
        self._error.hide()

        last = self._settings.raw_game_dir()
        if last is not None:
            self._last_opened.setText(f"Last opened: {last}")
            self._last_opened.show()
        else:
            self._last_opened.hide()

        self._detected_path = auto_detect_d4_dir()
        if self._detected_path is not None:
            self._detected.setText(f"Detected: {self._detected_path}")
            self._detected.show()
            self._use_detected_btn.show()
        else:
            self._detected.hide()
            self._use_detected_btn.hide()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _card_mouse_press(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            # Don't hijack clicks that landed on a child button — Qt routes
            # those here only when the press wasn't consumed by the child,
            # so this branch is just the "blank area" click path.
            self._on_card_clicked()
        # Bubble up so default behavior (focus, etc.) still works.
        QFrame.mousePressEvent(self._card, event)

    def _on_card_clicked(self) -> None:
        if self._detected_path is not None:
            # When auto-detect found something, the most useful default
            # action is to use it. The browse button is still available
            # for users who want to pick a different folder.
            self._on_use_detected_clicked()
        else:
            self._on_browse_clicked()

    def _on_use_detected_clicked(self) -> None:
        if self._detected_path is None:
            return
        self._accept(self._detected_path)

    def _on_browse_clicked(self) -> None:
        start_dir = ""
        last = self._settings.raw_game_dir()
        if last is not None and last.is_dir():
            start_dir = str(last)

        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select Diablo IV installation folder",
            start_dir,
        )
        if not chosen:
            return
        self._accept(Path(chosen))

    def _accept(self, path: Path) -> None:
        if not is_valid_d4_dir(path):
            self._show_error("No Diablo IV data found at this location")
            return
        self._error.hide()
        self.directory_selected.emit(path)

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.show()
