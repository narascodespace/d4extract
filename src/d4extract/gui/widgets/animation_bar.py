"""Inline transport / scrubber bar for the animation viewport.

Sits between the 3D viewport and the existing :class:`PropertiesBar`.
Hidden by default — the main window calls :meth:`show` only after a
skinned model has loaded and animation discovery has returned at least
one match.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
)

if TYPE_CHECKING:
    from d4extract.gui.anim_lookup import AnimationInfo

log = logging.getLogger(__name__)


REST_POSE_LABEL = "Rest Pose"


class AnimationBar(QFrame):
    """Compact animation transport — dropdown, perm selector, scrubber."""

    # ``str`` carries either the animation name or :data:`REST_POSE_LABEL`.
    animation_selected = Signal(str)
    permutation_changed = Signal(int)
    frame_changed = Signal(int)
    play_toggled = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("animationBar")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(
            "QFrame#animationBar {"
            "  background-color: rgba(30, 30, 46, 200);"
            "  border-top: 1px solid #45475a;"
            "}"
            "QFrame#animationBar QLabel {"
            "  background: transparent;"
            "  color: #cdd6f4;"
            "  font-size: 11px;"
            "}"
            "QFrame#animationBar QLabel#animKey {"
            "  color: #6c7086;"
            "}"
            "QFrame#animationBar QComboBox {"
            "  background-color: #1e1e2e;"
            "  color: #cdd6f4;"
            "  border: 1px solid #45475a;"
            "  border-radius: 3px;"
            "  padding: 2px 6px;"
            "  min-width: 220px;"
            "}"
            "QFrame#animationBar QSpinBox {"
            "  background-color: #1e1e2e;"
            "  color: #cdd6f4;"
            "  border: 1px solid #45475a;"
            "  border-radius: 3px;"
            "  padding: 2px 4px;"
            "  min-width: 56px;"
            "}"
            "QFrame#animationBar QPushButton {"
            "  background-color: #313244;"
            "  color: #cdd6f4;"
            "  border: 1px solid #45475a;"
            "  border-radius: 3px;"
            "  padding: 2px 10px;"
            "  min-width: 28px;"
            "}"
            "QFrame#animationBar QPushButton:hover {"
            "  background-color: #45475a;"
            "}"
            "QFrame#animationBar QPushButton:checked {"
            "  background-color: #89b4fa;"
            "  color: #1e1e2e;"
            "  font-weight: 600;"
            "}"
            "QFrame#animationBar QSlider::groove:horizontal {"
            "  height: 4px;"
            "  background: #45475a;"
            "  border-radius: 2px;"
            "}"
            "QFrame#animationBar QSlider::handle:horizontal {"
            "  width: 12px;"
            "  height: 12px;"
            "  background: #89b4fa;"
            "  border-radius: 6px;"
            "  margin: -4px 0;"
            "}"
            "QFrame#animationBar QSlider::sub-page:horizontal {"
            "  background: #89b4fa;"
            "  border-radius: 2px;"
            "}"
        )

        # State.
        self._animations: list = []
        self._frame_count = 0
        self._frame_rate = 30.0

        # Playback timer — fires on the main thread (Qt requirement) so
        # ``show_frame`` calls land on the viewport's home thread too.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self._build_ui()
        self.set_inactive("No animation loaded")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(10)

        # Animation dropdown.
        self._anim_combo = QComboBox(self)
        self._anim_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon,
        )
        self._anim_combo.currentIndexChanged.connect(self._on_anim_changed)
        row.addWidget(self._anim_combo)

        # Permutation spinbox (hidden when permutation_count <= 1).
        self._perm_label = QLabel("Perm", self)
        self._perm_label.setObjectName("animKey")
        self._perm_spin = QSpinBox(self)
        self._perm_spin.setRange(0, 0)
        self._perm_spin.valueChanged.connect(self._on_perm_changed)
        self._perm_spin.setVisible(False)
        self._perm_label.setVisible(False)
        row.addWidget(self._perm_label)
        row.addWidget(self._perm_spin)

        # Transport buttons.
        self._step_back_btn = self._make_button("|◀", "Previous frame")
        self._step_back_btn.clicked.connect(self._on_step_back)
        self._play_btn = self._make_button("▶", "Play / Pause", checkable=True)
        self._play_btn.toggled.connect(self._on_play_toggled)
        self._step_fwd_btn = self._make_button("▶|", "Next frame")
        self._step_fwd_btn.clicked.connect(self._on_step_forward)
        row.addWidget(self._step_back_btn)
        row.addWidget(self._play_btn)
        row.addWidget(self._step_fwd_btn)

        # Slider.
        self._slider = QSlider(Qt.Horizontal, self)
        self._slider.setMinimum(0)
        self._slider.setMaximum(0)
        self._slider.setSingleStep(1)
        self._slider.setPageStep(5)
        self._slider.valueChanged.connect(self._on_slider_changed)
        self._slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self._slider, 1)

        # Frame counter + fps.
        self._frame_label = QLabel("Frame 0 / 0", self)
        row.addWidget(self._frame_label)
        self._fps_label = QLabel("—", self)
        self._fps_label.setObjectName("animKey")
        row.addWidget(self._fps_label)

    def _make_button(
        self, text: str, tooltip: str, *, checkable: bool = False,
    ) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setToolTip(tooltip)
        btn.setCheckable(checkable)
        btn.setFixedWidth(36)
        return btn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_animations(self, animations: list) -> None:
        """Populate the dropdown with ``[AnimationInfo, ...]``.

        Inserts the rest-pose entry at position 0 so the user can always
        return to a static pose.  Selecting an animation re-emits
        :attr:`animation_selected` exactly once.
        """
        self._animations = list(animations or [])
        self._anim_combo.blockSignals(True)
        self._anim_combo.clear()
        self._anim_combo.addItem(REST_POSE_LABEL)
        for info in self._animations:
            label = info.name
            if info.permutation_count > 1:
                label = f"{info.name}  ×{info.permutation_count}"
            self._anim_combo.addItem(label, info)
        self._anim_combo.setCurrentIndex(0)
        self._anim_combo.blockSignals(False)
        if not self._animations:
            self.set_inactive("No animations target this appearance")
        else:
            self.set_inactive(f"Select an animation ({len(self._animations)} available)")

    def set_active(
        self, *, frame_count: int, frame_rate: float, permutation_count: int,
    ) -> None:
        """Configure the bar after an animation has finished decoding."""
        self._frame_count = max(1, int(frame_count))
        self._frame_rate = float(frame_rate) if frame_rate > 0 else 30.0

        self._slider.blockSignals(True)
        self._slider.setMaximum(self._frame_count - 1)
        self._slider.setValue(0)
        self._slider.setEnabled(True)
        self._slider.blockSignals(False)

        self._step_back_btn.setEnabled(True)
        self._step_fwd_btn.setEnabled(True)
        self._play_btn.setEnabled(True)

        self._perm_spin.blockSignals(True)
        self._perm_spin.setRange(0, max(0, permutation_count - 1))
        self._perm_spin.setValue(0)
        self._perm_spin.blockSignals(False)
        many = permutation_count > 1
        self._perm_spin.setVisible(many)
        self._perm_label.setVisible(many)

        self._update_labels(0)
        # Stop any prior playback — the new animation may have a
        # different fps, so we restart from frame 0 in paused state.
        self._stop_timer()

    def set_inactive(self, hint: str = "") -> None:
        """Disable the transport without clearing the dropdown.

        Used when the dropdown is on the rest-pose entry, the discovery
        result was empty, or we're between models.  Keeps the dropdown
        interactive so the user can still pick a different animation.
        """
        self._frame_count = 0
        self._stop_timer()
        self._slider.blockSignals(True)
        self._slider.setMaximum(0)
        self._slider.setValue(0)
        self._slider.setEnabled(False)
        self._slider.blockSignals(False)
        self._step_back_btn.setEnabled(False)
        self._step_fwd_btn.setEnabled(False)
        self._play_btn.setChecked(False)
        self._play_btn.setEnabled(False)
        self._perm_spin.setVisible(False)
        self._perm_label.setVisible(False)
        self._frame_label.setText("Frame 0 / 0")
        self._fps_label.setText(hint or "—")

    def reset(self) -> None:
        """Drop discovered animations and return to the no-data state."""
        self._animations = []
        self._anim_combo.blockSignals(True)
        self._anim_combo.clear()
        self._anim_combo.addItem(REST_POSE_LABEL)
        self._anim_combo.setCurrentIndex(0)
        self._anim_combo.blockSignals(False)
        self.set_inactive("No animation loaded")

    def current_animation(self) -> "AnimationInfo | None":
        """Return the currently selected ``AnimationInfo``, or ``None``."""
        idx = self._anim_combo.currentIndex()
        if idx <= 0:
            return None
        return self._anim_combo.itemData(idx)

    def current_permutation(self) -> int:
        return self._perm_spin.value()

    def current_frame(self) -> int:
        return self._slider.value()

    def is_playing(self) -> bool:
        """Return whether the playback timer is currently running."""
        return self._timer.isActive()

    def set_playing(self, playing: bool) -> None:
        """Start or stop playback as if the play button were clicked.

        Routes through the checkable play button so the glyph, timer,
        and :attr:`play_toggled` signal all stay consistent with a
        manual toggle. A no-op when there is no active animation to
        play, or when the bar is already in the requested state.
        """
        playing = bool(playing)
        if playing and self._frame_count <= 0:
            return
        self._play_btn.setChecked(playing)

    def set_frame(self, frame: int) -> None:
        """Move the scrubber to ``frame`` without starting playback.

        Clamps into ``[0, frame_count - 1]``. Setting the slider emits
        :attr:`frame_changed` via ``valueChanged`` so consumers re-skin
        to the new frame exactly as a manual scrub would.
        """
        if self._frame_count <= 0:
            return
        frame = max(0, min(int(frame), self._frame_count - 1))
        self._slider.setValue(frame)

    def set_loading_message(self, message: str) -> None:
        """Show a transient message in the fps slot during background work."""
        self._fps_label.setText(message)

    # ------------------------------------------------------------------
    # Slot handlers
    # ------------------------------------------------------------------

    def _on_anim_changed(self, idx: int) -> None:
        # Pause whenever the user changes selection — playback for an
        # animation that's no longer current would be confusing.
        self._stop_timer()
        if idx <= 0:
            self.set_inactive("Rest pose")
            self.animation_selected.emit(REST_POSE_LABEL)
            return
        info = self._anim_combo.itemData(idx)
        if info is None:
            return
        # Show a "Loading…" hint until the worker reports back via
        # set_active.  set_inactive handles slider / button disabling.
        self.set_inactive("Loading…")
        self.animation_selected.emit(info.name)

    def _on_perm_changed(self, value: int) -> None:
        self._stop_timer()
        self.permutation_changed.emit(int(value))

    def _on_play_toggled(self, checked: bool) -> None:
        # Update the glyph and emit, then start/stop the timer.  The
        # signal goes out before the timer call so consumers see a
        # consistent paused/playing state on the first tick.
        self._play_btn.setText("‖" if checked else "▶")
        self.play_toggled.emit(bool(checked))
        if checked and self._frame_count > 0:
            interval = max(8, int(round(1000.0 / self._frame_rate)))
            self._timer.start(interval)
        else:
            self._timer.stop()

    def _stop_timer(self) -> None:
        if self._play_btn.isChecked():
            self._play_btn.blockSignals(True)
            self._play_btn.setChecked(False)
            self._play_btn.setText("▶")
            self._play_btn.blockSignals(False)
        self._timer.stop()

    def _on_step_back(self) -> None:
        self._stop_timer()
        if self._frame_count <= 0:
            return
        cur = self._slider.value()
        new = (cur - 1) % self._frame_count
        self._set_frame(new)

    def _on_step_forward(self) -> None:
        self._stop_timer()
        if self._frame_count <= 0:
            return
        cur = self._slider.value()
        new = (cur + 1) % self._frame_count
        self._set_frame(new)

    def _on_slider_changed(self, value: int) -> None:
        self._update_labels(value)
        self.frame_changed.emit(int(value))

    def _tick(self) -> None:
        if self._frame_count <= 0:
            self._timer.stop()
            return
        cur = self._slider.value()
        new = (cur + 1) % self._frame_count
        self._set_frame(new)

    def _set_frame(self, value: int) -> None:
        # ``setValue`` triggers ``valueChanged`` which emits frame_changed,
        # so we don't need to emit again here.
        self._slider.setValue(int(value))

    def _update_labels(self, frame: int) -> None:
        if self._frame_count <= 0:
            self._frame_label.setText("Frame 0 / 0")
            return
        self._frame_label.setText(
            f"Frame {frame + 1} / {self._frame_count}"
        )
        self._fps_label.setText(f"{self._frame_rate:g}fps")
