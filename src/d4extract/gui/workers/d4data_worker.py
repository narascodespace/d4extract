"""Background worker that downloads and installs the d4data repo.

Wraps :func:`d4extract.setup.download_and_install_d4data` in a
``QThread`` so the network fetch and zip extraction don't block the UI
thread. The progress callback emitted by the headless module is
forwarded as a Qt signal, which marshals correctly to the main
thread — widgets connected to ``progress`` can drive a ``QProgressBar``
directly without any extra queuing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal

from d4extract.setup import (
    D4DataInstallError,
    D4DataSource,
    default_d4data_dir,
    download_and_install_d4data,
)

log = logging.getLogger(__name__)


class D4DataDownloadWorker(QThread):
    """Run :func:`download_and_install_d4data` off the UI thread.

    Signals:

    - ``progress(done, total, stage)`` — forwarded from the headless
      progress callback. ``total`` is ``-1`` when the server didn't
      send a ``Content-Length`` header (Qt's ``Signal(int, ...)``
      can't carry ``None``, so the sentinel is the agreed contract
      between this worker and connected widgets).
    - ``finished(Path)`` — emitted with the resolved install path on
      success.
    - ``failed(str)`` — emitted with a user-facing error message on
      any download/extract/install failure.

    The ``finished`` name shadows the inherited ``QThread.finished``
    signal, but we want a payload-carrying variant — Qt picks the
    redefined signal for connect() calls, and the original (no-arg)
    signal still fires automatically when run() returns. Callers that
    want lifecycle notification can connect to ``finished(Path)`` for
    the payload or ``QThread.finished`` for the bare event.
    """

    progress = Signal(int, int, str)
    finished = Signal(Path)
    failed = Signal(str)

    def __init__(
        self,
        target_dir: Path | None = None,
        source: D4DataSource | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # Resolving the target eagerly means the widget can show
        # "installing to <path>" before the thread starts, and the
        # default path is computed off the UI thread's first call.
        self._target_dir = Path(target_dir) if target_dir else default_d4data_dir()
        self._source = source or D4DataSource()

    @property
    def target_dir(self) -> Path:
        return self._target_dir

    @property
    def source(self) -> D4DataSource:
        return self._source

    def _on_progress(
        self, done: int, total: Optional[int], stage: str,
    ) -> None:
        """Adapter — Qt signals can't carry Python ``None``."""
        self.progress.emit(done, total if total is not None else -1, stage)

    def run(self) -> None:  # noqa: D401 — Qt API
        try:
            installed = download_and_install_d4data(
                target_dir=self._target_dir,
                source=self._source,
                progress=self._on_progress,
            )
        except D4DataInstallError as exc:
            log.warning("d4data install failed: %s", exc)
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # pragma: no cover — defensive
            log.exception("Unexpected error while installing d4data")
            self.failed.emit(f"Unexpected error: {exc}")
            return

        self.finished.emit(Path(installed))
