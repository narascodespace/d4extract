"""First-run setup card for installing the d4data community metadata.

Mirrors :class:`d4extract.gui.widgets.setup_card.SetupCard` so the
two-step welcome flow (game directory → d4data) looks consistent.
Offers two paths:

- **Download for me (recommended)** — kicks off
  :class:`D4DataDownloadWorker` against
  :func:`default_d4data_dir`. The card swaps to a progress view
  for the duration of the fetch.
- **I have it already** — opens a folder picker, validates with
  :func:`is_d4data_dir`, and persists the chosen path.

On either success path the card emits ``d4data_ready(Path)`` and the
parent window swaps to the main browser.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from d4extract.gui.settings import AppSettings
from d4extract.gui.workers.d4data_worker import D4DataDownloadWorker
from d4extract.setup import (
    D4DataInstallError,
    default_d4data_dir,
    is_d4data_dir,
    resolve_d4data_json_path,
)

log = logging.getLogger(__name__)


# Progress-view page indices on the internal QStackedLayout.
_PAGE_CHOICE = 0
_PAGE_PROGRESS = 1


class D4DataCard(QWidget):
    """Centered card that resolves the d4data dependency.

    The card lives alongside :class:`SetupCard` in the main window's
    ``QStackedWidget``. Showing it is the parent's responsibility — we
    emit ``d4data_ready`` and let the parent decide what to do next
    (typically: stash the path in ``AppSettings`` and switch pages).

    A single :class:`D4DataDownloadWorker` is kept around per card so
    re-entrant "Download" clicks are guarded — once a download starts,
    the action button is disabled until the worker emits a terminal
    signal.
    """

    d4data_ready = Signal(Path)

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._worker: D4DataDownloadWorker | None = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 40, 40, 40)
        outer.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)

        self._card = QFrame(self)
        self._card.setObjectName("card")
        self._card.setMaximumWidth(600)
        self._card.setMinimumWidth(420)
        self._card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(32, 28, 32, 28)
        card_layout.setSpacing(8)

        title = QLabel("Set up d4data", self._card)
        title.setObjectName("cardTitle")

        subtitle = QLabel(
            "d4data is the community-maintained metadata repo for "
            "Diablo IV. d4extract needs it to resolve model and "
            "material references.",
            self._card,
        )
        subtitle.setObjectName("cardSubtitle")
        subtitle.setWordWrap(True)

        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)

        # Two stacked pages inside the card: the action buttons (choice
        # page) and the in-flight progress view. A stacked layout swaps
        # without resizing the surrounding card.
        self._stack = QStackedLayout()
        self._stack.addWidget(self._build_choice_page())
        self._stack.addWidget(self._build_progress_page())
        card_layout.addLayout(self._stack)

        self._error = QLabel("", self._card)
        self._error.setObjectName("cardError")
        self._error.setWordWrap(True)
        self._error.hide()
        card_layout.addWidget(self._error)

        row.addWidget(self._card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

    def _build_choice_page(self) -> QWidget:
        page = QWidget(self._card)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(8)

        self._target_label = QLabel(
            f"Default install location: {default_d4data_dir()}",
            page,
        )
        self._target_label.setObjectName("cardMuted")
        self._target_label.setWordWrap(True)
        layout.addWidget(self._target_label)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)

        self._download_btn = QPushButton("Download for me (recommended)", page)
        self._download_btn.setObjectName("primary")
        self._download_btn.clicked.connect(self._on_download_clicked)

        self._existing_btn = QPushButton("I have it already", page)
        self._existing_btn.clicked.connect(self._on_existing_clicked)

        action_row.addWidget(self._download_btn)
        action_row.addWidget(self._existing_btn)
        action_row.addStretch(1)

        layout.addLayout(action_row)
        return page

    def _build_progress_page(self) -> QWidget:
        page = QWidget(self._card)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(8)

        self._stage_label = QLabel("Preparing…", page)
        self._stage_label.setObjectName("cardMuted")
        layout.addWidget(self._stage_label)

        self._progress = QProgressBar(page)
        # 0/0 puts the bar into indeterminate (marquee) mode until we
        # get a real Content-Length-backed total. Switched to a regular
        # 0..100 range inside ``_on_progress``.
        self._progress.setRange(0, 0)
        layout.addWidget(self._progress)
        return page

    # ------------------------------------------------------------------
    # Public surface for re-entry from the File menu
    # ------------------------------------------------------------------

    def start_download(self) -> None:
        """Trigger the download path programmatically.

        Called by the File menu's ``Re-download d4data`` action so the
        same code path runs whether the user clicked the card button
        or chose the menu item.
        """
        self._on_download_clicked()

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_download_clicked(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return  # guarded: already downloading

        self._error.hide()
        self._stack.setCurrentIndex(_PAGE_PROGRESS)
        self._stage_label.setText("Starting…")
        self._progress.setRange(0, 0)

        self._worker = D4DataDownloadWorker(
            target_dir=default_d4data_dir(), parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_download_finished)
        self._worker.failed.connect(self._on_download_failed)
        self._worker.finished.connect(self._clear_worker)
        self._worker.failed.connect(self._clear_worker)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.failed.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_existing_clicked(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select existing d4data directory",
            str(Path.home()),
        )
        if not chosen:
            return
        path = Path(chosen)
        if not is_d4data_dir(path):
            QMessageBox.warning(
                self,
                "d4data",
                f"{path}\n\nThis doesn't look like a d4data checkout — "
                "it should be the d4data repo root (contains a 'json/' "
                "directory) or its 'json/' subdirectory.",
            )
            return
        # Normalise to the ``json/`` subpath the rest of the code consumes.
        try:
            normalised = resolve_d4data_json_path(path)
        except D4DataInstallError as exc:
            QMessageBox.warning(self, "d4data", str(exc))
            return
        self._settings.set_d4data_path(normalised)
        self.d4data_ready.emit(normalised)

    def _on_progress(self, done: int, total: int, stage: str) -> None:
        # Headless module emits "downloading" / "extracting"; capitalize
        # for display since the rest of the GUI is sentence-cased.
        self._stage_label.setText(stage.capitalize())
        if total > 0:
            # Switch to a determinate bar once we know the total. We
            # report as a 0..100 percent so the headless module's
            # byte/byte progress stays opaque to the UI.
            pct = max(0, min(100, (done * 100) // total))
            if self._progress.maximum() != 100:
                self._progress.setRange(0, 100)
            self._progress.setValue(pct)
        else:
            # Indeterminate; QProgressBar marquees automatically.
            if self._progress.maximum() != 0:
                self._progress.setRange(0, 0)

    def _on_download_finished(self, installed: Path) -> None:
        self._settings.set_d4data_path(installed)
        self._stack.setCurrentIndex(_PAGE_CHOICE)
        self.d4data_ready.emit(installed)

    def _on_download_failed(self, message: str) -> None:
        log.warning("d4data download failed: %s", message)
        self._stack.setCurrentIndex(_PAGE_CHOICE)
        self._error.setText(f"Download failed: {message}")
        self._error.show()

    def _clear_worker(self, *_args) -> None:
        self._worker = None
