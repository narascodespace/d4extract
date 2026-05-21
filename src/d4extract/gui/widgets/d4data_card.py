"""First-run setup card for locating the user-supplied d4data checkout.

d4extract no longer downloads d4data for the user (the in-app
downloader was removed after repeated failures with partial downloads
and Windows file-lock contention on the atomic swap). The card is now
a single-button folder picker: it explains the dependency, links to
the upstream repo, and accepts whichever folder the user points it at
as long as :func:`is_d4data_dir` validates it.

On success the path is normalised via :func:`resolve_d4data_json_path`
(so the rest of the codebase sees the ``json/`` subpath regardless of
which level the user picked) and the card emits ``d4data_ready(Path)``
for the main window to consume.
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
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from d4extract.gui.settings import AppSettings
from d4extract.setup import (
    D4DataInstallError,
    is_d4data_dir,
    resolve_d4data_json_path,
)

log = logging.getLogger(__name__)


class D4DataCard(QWidget):
    """Centered card that resolves the d4data dependency.

    The card lives alongside :class:`SetupCard` in the main window's
    ``QStackedWidget``. Showing it is the parent's responsibility — we
    emit ``d4data_ready`` and let the parent decide what to do next
    (typically: stash the path in ``AppSettings`` and switch pages).
    """

    d4data_ready = Signal(Path)

    def __init__(
        self,
        settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
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

        title = QLabel("Locate d4data", self._card)
        title.setObjectName("cardTitle")

        # Rich-text body so the GitHub link is clickable. ``setOpenExternal
        # Links`` hands the click off to the OS browser instead of trying
        # to navigate the label itself.
        body = QLabel(
            "<p>d4extract needs a local copy of <b>d4data</b>, the "
            "community metadata repo for Diablo IV. It's a separate "
            "repository that updates with each game patch.</p>"
            "<p>Get it from <a "
            "href=\"https://github.com/blizzhackers/d4data\">"
            "github.com/blizzhackers/d4data</a> — clone the repository "
            "or download the ZIP archive (~250 MB, 100k+ files).</p>"
            "<p>Then click below and point d4extract at the folder you "
            "cloned or extracted to.</p>",
            self._card,
        )
        body.setObjectName("cardSubtitle")
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(
            Qt.TextBrowserInteraction,
        )

        card_layout.addWidget(title)
        card_layout.addWidget(body)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 12, 0, 0)
        action_row.addStretch(1)
        self._select_btn = QPushButton("Select d4data Folder", self._card)
        self._select_btn.setObjectName("primary")
        self._select_btn.clicked.connect(self._on_select_clicked)
        action_row.addWidget(self._select_btn)
        action_row.addStretch(1)
        card_layout.addLayout(action_row)

        self._error = QLabel("", self._card)
        self._error.setObjectName("cardError")
        self._error.setWordWrap(True)
        self._error.hide()
        card_layout.addWidget(self._error)

        row.addWidget(self._card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_select_clicked(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Select d4data directory",
            str(Path.home()),
        )
        if not chosen:
            return
        path = Path(chosen)
        if not is_d4data_dir(path):
            QMessageBox.warning(
                self,
                "d4data",
                f"{path}\n\nThis folder doesn't look like a d4data "
                "checkout — it should contain a 'json/' subdirectory "
                "(the repo root) or be the 'json/' subdirectory itself.",
            )
            return
        # Normalise to the ``json/`` subpath the rest of the code
        # consumes. ``is_d4data_dir`` already accepted both shapes; this
        # is the conversion to the single internal form.
        try:
            normalised = resolve_d4data_json_path(path)
        except D4DataInstallError as exc:
            QMessageBox.warning(self, "d4data", str(exc))
            return
        self._settings.set_d4data_path(normalised)
        self._error.hide()
        self.d4data_ready.emit(normalised)
