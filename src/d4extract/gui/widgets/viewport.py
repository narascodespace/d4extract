"""3D viewport widget — embeds a PyVistaQt BackgroundPlotter."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from d4extract.formats.app_parser import MeshData, Submesh

log = logging.getLogger(__name__)


# Background color for the viewport — slightly darker than the surface
# panels so the model has good contrast against it.
VIEWPORT_BG = "#1a1a2e"


# System UI font candidates for the "Press F to reset camera" hint.
# We render the hint via VTK's 2D text actor (the only way to keep it
# from getting clobbered by GL repaints during splitter drags), but
# VTK's bundled fonts look mismatched against the rest of the Qt UI.
# Loading the OS font directly through ``vtkTextProperty.SetFontFile``
# bypasses VTK's bundled set and matches the surrounding chrome.
# First existing path wins; ``None`` falls back to VTK's Arial.
_RESET_HINT_FONT_CANDIDATES: tuple[str, ...] = (
    r"C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _resolve_reset_hint_font() -> str | None:
    for path in _RESET_HINT_FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


# Qualitative palette used for flat-color per-submesh rendering. Picked
# from matplotlib's "Set2" so the viewport keeps the same look it had
# before the per-submesh refactor; cycling by ``material_index`` means
# submeshes that share a material share a color, which is the cue users
# rely on to read the model.
_SET2_PALETTE: tuple[str, ...] = (
    "#66c2a5", "#fc8d62", "#8da0cb", "#e78ac3",
    "#a6d854", "#ffd92f", "#e5c494", "#b3b3b3",
)


def _color_for_material(material_index: int) -> str:
    return _SET2_PALETTE[material_index % len(_SET2_PALETTE)]


def _hex_to_rgb_floats(hex_color: str) -> tuple[float, float, float]:
    """Convert ``"#rrggbb"`` to a 0..1 float triple for VTK colour APIs."""
    h = hex_color.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))




def _actor_name(submesh_index: int) -> str:
    """Stable name for the actor representing one submesh.

    Used so ``plotter.add_mesh(..., name=…)`` can swap the flat-color
    actor for a textured one without depending on object identity of
    the previous actor handle.
    """
    return f"d4_submesh_{submesh_index}"


def _convert_positions(positions: list[tuple[float, float, float]]) -> np.ndarray:
    """D4-native LH Z-up → VTK/glTF RH Y-up.

    Per-vertex transform: ``(x, y, z) → (x, z, -y)``. Implemented with a
    column reorder followed by a sign flip on the new Y column.
    """
    if not positions:
        return np.zeros((0, 3), dtype=np.float32)
    arr = np.asarray(positions, dtype=np.float32)
    # Reorder columns: [0, 2, 1] gives (x, z, y); negate the new y column
    # to land at (x, z, -y).
    out = arr[:, [0, 2, 1]].copy()
    out[:, 2] = -out[:, 2]
    return out


def _convert_normals(
    normals: list[tuple[float, float, float, float]],
) -> np.ndarray:
    """Same axis swap as positions; drops the parser's 4th component."""
    if not normals:
        return np.zeros((0, 3), dtype=np.float32)
    arr = np.asarray(normals, dtype=np.float32)[:, :3]
    out = arr[:, [0, 2, 1]].copy()
    out[:, 2] = -out[:, 2]
    return out


def _build_face_array(indices: np.ndarray) -> np.ndarray:
    """Flat triangle-index numpy array → VTK face array.

    VTK expects ``[3, v0, v1, v2, 3, v3, v4, v5, ...]`` for triangle
    soup. We allocate one int64 array, set the leading per-triangle
    count, then stride-copy the indices into the remaining slots.
    """
    if indices.size == 0:
        return np.zeros((0,), dtype=np.int64)
    n_tris = indices.size // 3
    if n_tris == 0:
        return np.zeros((0,), dtype=np.int64)
    idx = indices[: n_tris * 3].astype(np.int64, copy=False)
    faces = np.empty(n_tris * 4, dtype=np.int64)
    faces[0::4] = 3
    faces[1::4] = idx[0::3]
    faces[2::4] = idx[1::3]
    faces[3::4] = idx[2::3]
    return faces


def _set_texture_coordinates(mesh, uvs: np.ndarray) -> bool:
    """Attach 2D UVs to a PolyData as the active texture coordinates.

    PyVista renamed ``active_t_coords`` → ``active_texture_coordinates``
    around 0.43; try the modern name first and fall back so the
    viewport still works on older installs.
    """
    if uvs.size == 0:
        return False
    coords = np.ascontiguousarray(uvs.astype(np.float32, copy=False))
    try:
        mesh.active_texture_coordinates = coords
        return True
    except (AttributeError, TypeError):
        pass
    try:
        mesh.active_t_coords = coords
        return True
    except (AttributeError, TypeError):
        return False


class _LoadingIndicator(QFrame):
    """Small floating overlay shown while a background operation is busy.

    Lives as a child of :class:`ViewportWidget` so it floats over the
    plotter without participating in the main stacked layout (which
    swaps the entire viewport between hint/loading/error/plotter
    states). The viewport drives :meth:`start`, :meth:`set_message`,
    and :meth:`stop` from its pipeline handlers; positioning is
    done by the viewport's ``resizeEvent``.
    """

    # Braille-pattern frames render compactly and animate smoothly even
    # at small font sizes. Same set used by ripgrep / fd / cargo so it
    # reads as a "spinner" to most users.
    SPINNER_FRAMES: tuple[str, ...] = (
        "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
    )
    FRAME_INTERVAL_MS = 80

    def __init__(self, parent: QWidget, object_name: str) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        # Don't intercept clicks — users dragging the camera should
        # still hit the underlying VTK interactor even when the spinner
        # happens to overlap the cursor.
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            f"QFrame#{object_name} {{"
            "  background-color: rgba(30, 30, 46, 220);"
            "  border: 1px solid #45475a;"
            "  border-radius: 8px;"
            "}"
            f"QFrame#{object_name} QLabel {{"
            "  background: transparent;"
            "  color: #cdd6f4;"
            "  font-size: 12px;"
            "}"
            f"QFrame#{object_name} QLabel#spinnerGlyph_{object_name} {{"
            "  color: #89b4fa;"
            "  font-size: 16px;"
            "  font-weight: bold;"
            "}"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 14, 8)
        row.setSpacing(10)

        self._glyph = QLabel(self.SPINNER_FRAMES[0], self)
        self._glyph.setObjectName(f"spinnerGlyph_{object_name}")
        self._glyph.setAlignment(Qt.AlignCenter)
        self._label = QLabel("", self)
        row.addWidget(self._glyph)
        row.addWidget(self._label)

        self._frame_index = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._advance_frame)

        self.hide()
        # Size to contents so the bounding box fits the message text;
        # the parent reads ``sizeHint`` after every ``set_message`` call
        # to center the indicator horizontally.
        self.adjustSize()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, message: str) -> None:
        self.set_message(message)
        self._frame_index = 0
        self._glyph.setText(self.SPINNER_FRAMES[0])
        self._timer.start()
        self.show()
        self.raise_()

    def set_message(self, message: str) -> None:
        self._label.setText(message)
        self.adjustSize()
        # The viewport repositions us after every message change since
        # the bounding-box width depends on the text.
        parent = self.parentWidget()
        if isinstance(parent, ViewportWidget):
            parent._reposition_indicators()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def is_active(self) -> bool:
        return self._timer.isActive()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _advance_frame(self) -> None:
        self._frame_index = (self._frame_index + 1) % len(self.SPINNER_FRAMES)
        self._glyph.setText(self.SPINNER_FRAMES[self._frame_index])


class ViewportWidget(QWidget):
    """PyVistaQt-backed 3D viewport with hint / loading / error overlays."""

    HINT_TEXT = "Select a model from the list to preview it here."

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plotter = None
        self._has_mesh = False
        # Per-submesh state populated by ``load_mesh`` and consumed by
        # ``apply_submesh_textures`` to swap flat-color actors for
        # textured ones once the texture worker reports back.
        self._mesh_data: "MeshData | None" = None
        self._submesh_meshes: dict[int, object] = {}
        self._submesh_actors: dict[int, object] = {}
        # Animation playback state — populated by ``set_animation`` and
        # cleared by ``clear_animation`` / ``load_mesh``.  Skinning math
        # runs in D4-native (LH Z-up) space; only the final positions are
        # converted to VTK before being pushed to the PolyData.
        self._anim_decoded = None
        self._anim_rest_positions_d4: "np.ndarray | None" = None
        self._anim_joints_global: "np.ndarray | None" = None
        self._anim_weights: "np.ndarray | None" = None
        self._anim_inv_bind: "np.ndarray | None" = None
        self._anim_rest_local: "np.ndarray | None" = None
        self._anim_hash_to_idx: dict[int, int] = {}
        self._anim_current_frame: int = 0
        # Held to keep the C++ side alive — VTK widgets are only
        # retained while a Python reference exists.
        self._orientation_widget = None
        self._axes_actor = None
        # Wheel-event observers — held so VTK can clean them up on
        # shutdown and so the per-event basis-snap callback isn't GC'd
        # between events. Each entry is ``(observed_object, tag)``.
        self._wheel_observer_tags: list[tuple[object, int]] = []
        self._wheel_event_count: int = 0
        # Recursion guard for the camera ModifiedEvent lock — our own
        # SetViewUp triggers ModifiedEvent, which would otherwise
        # re-enter the lock callback indefinitely.
        self._camera_lock_in_progress: bool = False
        # VTK 2D text actor for the "Press F to reset camera" hint.
        # Re-created on every ``plotter.clear()`` since clear() drops
        # 2D actors along with the scene's 3D content.
        self._reset_hint_actor = None
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._stack = QStackedLayout()
        self._stack.setStackingMode(QStackedLayout.StackOne)

        # Lazy plotter creation — keeps test environments without a
        # working VTK backend importable. The plotter is actually built
        # the first time a mesh is loaded or the viewport becomes
        # visible (whichever happens first).
        self._plotter_host = QWidget(self)
        plotter_layout = QVBoxLayout(self._plotter_host)
        plotter_layout.setContentsMargins(0, 0, 0, 0)
        self._plotter_layout = plotter_layout

        self._overlay = QLabel(self.HINT_TEXT, self)
        self._overlay.setObjectName("viewportOverlay")
        self._overlay.setAlignment(Qt.AlignCenter)
        self._overlay.setWordWrap(True)
        self._overlay.setStyleSheet(
            f"background-color: {VIEWPORT_BG}; color: #a6adc8; font-size: 14px;"
        )

        host = QWidget(self)
        host.setLayout(self._stack)
        self._stack.addWidget(self._plotter_host)   # index 0
        self._stack.addWidget(self._overlay)        # index 1
        self._stack.setCurrentWidget(self._overlay)
        outer.addWidget(host)

        # Floating loading indicators. Live outside the stacked layout
        # so they can overlay either the plotter or the placeholder
        # without being swapped out. ``resizeEvent`` keeps them pinned
        # to the bottom-center of the viewport, stacked vertically
        # (model indicator above texture indicator).
        self._model_indicator = _LoadingIndicator(self, "modelLoadingIndicator")
        self._model_indicator.hide()
        self._texture_indicator = _LoadingIndicator(self, "textureLoadingIndicator")
        self._texture_indicator.hide()

        # Top-left notice for features deferred to the Blender addon.
        self._blender_notice = QLabel(self)
        self._blender_notice.setObjectName("blenderNotice")
        self._blender_notice.setText(
            "Skin and Hair resolved through provided Blender addon."
        )
        self._blender_notice.setWordWrap(True)
        self._blender_notice.setStyleSheet(
            "QLabel#blenderNotice {"
            "  background-color: transparent;"
            "  color: #a6adc8;"
            "  font-size: 11px;"
            "  padding: 6px 10px;"
            "}"
        )
        self._blender_notice.adjustSize()
        self._blender_notice.hide()

        # The "Press F to reset camera" hint is rendered by VTK itself
        # (see ``_install_reset_hint_actor``). A QLabel sibling — or
        # even a child of ``plotter.interactor`` with
        # ``WA_AlwaysStackOnTop`` — gets visually clobbered by the GL
        # surface after splitter drags, because the QOpenGLWidget's
        # direct painting bypasses Qt's child compositor. A VTK 2D
        # actor lives inside the same paint pipeline as the 3D scene,
        # so it can't be desynchronized from it.

    # ------------------------------------------------------------------
    # Plotter lifecycle
    # ------------------------------------------------------------------

    def _ensure_plotter(self) -> bool:
        """Create the BackgroundPlotter on first use. Returns False on failure."""
        if self._plotter is not None:
            return True
        try:
            from pyvistaqt import BackgroundPlotter
        except Exception as exc:
            log.error("pyvistaqt import failed: %s", exc)
            self.show_error(f"3D viewport unavailable: {exc}")
            return False

        try:
            plotter = BackgroundPlotter(
                show=False,
                toolbar=False,
                menu_bar=False,
                # Re-parent the interactor into our layout — the default
                # BackgroundPlotter would otherwise pop up its own
                # top-level window.
                editor=False,
            )
            plotter.set_background(VIEWPORT_BG)

            # Two-light setup as called for in the spec — a key light
            # from the upper-front-right and a softer fill from the
            # lower-back-left.
            try:
                import pyvista as pv
                key = pv.Light(position=(5.0, 5.0, 10.0), intensity=0.8)
                fill = pv.Light(position=(-3.0, -2.0, 5.0), intensity=0.3)
                plotter.add_light(key)
                plotter.add_light(fill)
            except Exception as exc:  # non-fatal — default lighting still works
                log.debug("Could not install custom lights: %s", exc)
        except Exception as exc:
            log.exception("BackgroundPlotter construction failed")
            self.show_error(f"3D viewport unavailable: {exc}")
            return False

        self._plotter = plotter
        self._plotter_layout.addWidget(plotter.interactor)

        self._install_reset_hint_actor()

        # Camera ergonomics — both are best-effort; failures here only
        # affect orbit feel and the orientation indicator, not the
        # ability to render meshes.
        self._install_camera_constraints()
        self._install_orientation_gizmo()
        return True

    def _install_reset_hint_actor(self) -> None:
        """(Re)install the bottom-left F-key hint as a VTK 2D text actor.

        Called from ``_ensure_plotter`` and again after every
        ``plotter.clear()`` in ``load_mesh`` (clear() drops 2D actors
        too). The ``name=`` kwarg makes re-adds atomic — VTK replaces
        the named actor instead of stacking duplicates.

        ``font_file`` is forwarded to pyvista directly so the system
        UI font (Segoe UI on Windows) is used; pyvista handles the
        ``vtkTextProperty.SetFontFamily(VTK_FONT_FILE)`` /
        ``SetFontFile`` call ordering itself, which our previous
        manual ``GetTextProperty().SetFontFile`` after-the-fact didn't
        always take effect for.
        """
        plotter = self._plotter
        if plotter is None:
            return
        font_path = _resolve_reset_hint_font()
        try:
            actor = plotter.add_text(
                "Press F to reset camera",
                position="lower_left",
                font_size=8,
                color=(108 / 255, 112 / 255, 134 / 255),
                shadow=False,
                name="d4_reset_hint",
                font_file=font_path,
            )
        except Exception:
            log.debug("add_text for reset hint failed", exc_info=True)
            return

        self._reset_hint_actor = actor

    def _install_camera_constraints(self) -> None:
        """Lock the orbit style to a Y-up turntable.

        Uses ``Plotter.enable_terrain_style`` which installs VTK's
        ``vtkInteractorStyleTerrain``. That style locks the camera
        up-vector at the C++ level (azimuth on horizontal drag,
        elevation on vertical drag, no roll), which is the Blender /
        Maya / 3ds Max feel the spec calls for. Subclassing
        ``vtkInteractorStyleTrackballCamera`` and overriding ``Rotate``
        does not work reliably here — VTK's Python wrappers do not
        always trampoline virtual-method overrides back into Python,
        so per-frame "snap ViewUp back" never fires and roll
        accumulates within a drag. The terrain style sidesteps that.

        Defaults to ``mouse_wheel_zooms=True`` and ``shift_pans=True``
        so the user gets scroll-wheel dolly and Shift+left-drag pan
        out of the box, matching the spec's mouse-binding list.
        """
        plotter = self._plotter
        if plotter is None:
            return
        try:
            plotter.enable_terrain_style()
        except Exception:
            log.debug("enable_terrain_style failed", exc_info=True)
            return
        self._reorient_camera_to_y_up()
        # Defer until after the widget has been shown — the interactor
        # is only fully wired once Qt has painted the widget at least
        # once, and we need to register on the live iren.
        QTimer.singleShot(0, self._install_wheel_observers)

    def _install_wheel_observers(self) -> None:
        """Wire the per-wheel basis-snap that fixes the orientation throw.

        Two pieces hang off this:

        * ``_pre_wheel_snap`` runs at priority 1.0 on every wheel event,
          BEFORE VTK trackball's auto-wired ``OnMouseWheelForward``
          (which sits at the default priority 0.0). Snapping ViewUp +
          ``OrthogonalizeViewUp`` *and rendering* commits a clean basis
          to the view-transform cache before the dolly's own
          ``SetupCamera`` re-orthogonalizes it — that re-orthogonalize
          is what produced the visible "throw sideways" on the first
          wheel, because the cache was holding an intermediate state
          left over from ``reset_camera``.

        * ``_log_wheel_post`` is a diagnostic on ``EndInteractionEvent``
          that records post-dolly camera state for the first three
          events. Cheap when DEBUG logging is off (single int compare);
          gives us actionable data if the throw ever resurfaces.
        """
        plotter = self._plotter
        if plotter is None:
            return
        try:
            vtk_iren = plotter.interactor.GetRenderWindow().GetInteractor()
        except Exception:
            log.debug("wheel observers: no interactor", exc_info=True)
            return
        if vtk_iren is None:
            return
        try:
            for event_name in (
                "MouseWheelForwardEvent", "MouseWheelBackwardEvent",
            ):
                tag = vtk_iren.AddObserver(
                    event_name, self._pre_wheel_snap, 1.0,
                )
                self._wheel_observer_tags.append((vtk_iren, tag))
            tag = vtk_iren.AddObserver(
                "EndInteractionEvent", self._log_wheel_post, 0.0,
            )
            self._wheel_observer_tags.append((vtk_iren, tag))
        except Exception:
            log.debug("wheel observers: AddObserver failed", exc_info=True)

        # Camera ModifiedEvent lock — closes the gap between an
        # external resetter writing ViewUp (e.g. trackball's first-call
        # GrabFocus chain pushing (0,0,1)) and our next pre-wheel
        # snap. Without it, the user sees a single-frame Z→Y flip on
        # the wheel after the resetter fires.
        try:
            camera = plotter.renderer.GetActiveCamera()
            if camera is not None:
                tag = camera.AddObserver(
                    "ModifiedEvent", self._on_camera_modified,
                )
                self._wheel_observer_tags.append((camera, tag))
        except Exception:
            log.debug("camera lock: AddObserver failed", exc_info=True)

    def _pre_wheel_snap(self, _caller, event_name: str) -> None:
        """Pin ViewUp to (0,1,0) before each dolly fires.

        Diagnostics from a prior run showed ``OrthogonalizeViewUp()``
        was actively harmful: it writes the orthogonalized vector back
        into stored ViewUp (e.g. ``(-0.408, 0.816, -0.408)`` for a
        diagonal view), and something downstream — probably pyvista's
        wheel callback's poked-subplot/render path — keeps resetting
        stored ViewUp back to ``(0, 0, 1)`` between events. The visible
        "throw" is the ~70° transition between those two states.

        Storing literal ``(0, 1, 0)`` and letting VTK's internal
        ``SetupCamera`` orthogonalize for the matrix (without writing
        back) keeps the stored value stable, so any inter-event reset
        either does nothing or is a much smaller delta.
        """
        plotter = self._plotter
        if plotter is None:
            return
        try:
            camera = plotter.renderer.GetActiveCamera()
        except Exception:
            return
        if camera is None:
            return

        self._wheel_event_count += 1
        debug = (
            log.isEnabledFor(logging.DEBUG)
            and self._wheel_event_count <= 3
        )
        if debug:
            before = (
                camera.GetPosition(), camera.GetFocalPoint(),
                camera.GetViewUp(), camera.GetDistance(),
            )

        try:
            camera.SetViewUp(0.0, 1.0, 0.0)
            # Render commits the matrix derived from the new ViewUp so
            # the dolly that follows starts from a clean view-transform
            # cache instead of an intermediate stale one.
            plotter.render()
        except Exception:
            log.debug("pre-wheel snap failed", exc_info=True)
            return

        if debug:
            after = (
                camera.GetPosition(), camera.GetFocalPoint(),
                camera.GetViewUp(), camera.GetDistance(),
            )
            log.debug(
                "wheel #%d (%s) pre-dolly:\n"
                "  before: pos=%s focal=%s up=%s dist=%.4f\n"
                "  after:  pos=%s focal=%s up=%s dist=%.4f",
                self._wheel_event_count, event_name,
                before[0], before[1], before[2], before[3],
                after[0], after[1], after[2], after[3],
            )

    def _on_camera_modified(self, caller, _event_name: str) -> None:
        """Re-snap ViewUp the moment something writes a non-Y value.

        Catches the one-shot resetter (likely trackball's first-call
        ``GrabFocus`` chain or pyvista's first ``poked_subplot``
        invocation) that flips stored ViewUp to ``(0, 0, 1)`` between
        the first and second wheel events. Setting ViewUp back to
        ``(0, 1, 0)`` here triggers ModifiedEvent again, hence the
        recursion guard.
        """
        if self._camera_lock_in_progress:
            return
        try:
            up = caller.GetViewUp()
        except Exception:
            return
        # Tolerance is generous — sub-1e-3 drift is below user-visible
        # threshold and saves a re-snap on every dolly's internal
        # ``Modified()``.
        if (
            abs(up[0]) < 1e-3
            and abs(up[1] - 1.0) < 1e-3
            and abs(up[2]) < 1e-3
        ):
            return
        self._camera_lock_in_progress = True
        try:
            caller.SetViewUp(0.0, 1.0, 0.0)
        except Exception:
            log.debug("camera lock: re-snap failed", exc_info=True)
        finally:
            self._camera_lock_in_progress = False

    def _log_wheel_post(self, _caller, _event_name: str) -> None:
        """Diagnostic: record camera state after the dolly settles."""
        if self._wheel_event_count == 0 or self._wheel_event_count > 3:
            return
        if not log.isEnabledFor(logging.DEBUG):
            return
        plotter = self._plotter
        if plotter is None:
            return
        try:
            camera = plotter.renderer.GetActiveCamera()
        except Exception:
            return
        if camera is None:
            return
        log.debug(
            "wheel #%d post-dolly: pos=%s focal=%s up=%s dist=%.4f",
            self._wheel_event_count,
            camera.GetPosition(), camera.GetFocalPoint(),
            camera.GetViewUp(), camera.GetDistance(),
        )

    def _reorient_camera_to_y_up(self) -> None:
        """Pin ViewUp to literal (0, 1, 0) and commit the basis.

        We deliberately do NOT call ``OrthogonalizeViewUp`` here:
        diagnostics show that writing the orthogonalized vector back
        into stored ViewUp produces a ~70° visible jump on the next
        wheel event (when something resets stored ViewUp to (0,0,1)
        and our snap re-skews it). Storing the literal Y-up vector and
        letting VTK orthogonalize internally for the rendered matrix
        keeps the stored state stable across events.
        """
        plotter = self._plotter
        if plotter is None:
            return
        try:
            camera = plotter.renderer.GetActiveCamera()
        except Exception:
            log.debug("Camera reorient: GetActiveCamera failed", exc_info=True)
            return
        if camera is None:
            return
        try:
            camera.SetViewUp(0.0, 1.0, 0.0)
            plotter.renderer.ResetCameraClippingRange()
        except Exception:
            log.debug("Camera reorient failed", exc_info=True)
            return
        # Best-effort render commits the new basis to the view matrix
        # cache; without this the next interaction may snap.
        try:
            plotter.render()
        except Exception:
            log.debug("Camera reorient: render failed", exc_info=True)

    def _install_orientation_gizmo(self) -> None:
        """Pin a Catppuccin-colored XYZ gizmo to the top-right corner.

        Uses ``vtkOrientationMarkerWidget`` so the gizmo tracks the main
        camera automatically. Labels are hidden — the RGB color coding
        (X=red, Y=green, Z=blue) is universal and the default caption
        actors render as oversized billboards that fight the dark theme.
        """
        plotter = self._plotter
        if plotter is None:
            return
        try:
            from vtkmodules.vtkInteractionWidgets import (
                vtkOrientationMarkerWidget,
            )
            from vtkmodules.vtkRenderingAnnotation import vtkAxesActor
        except Exception as exc:
            log.debug("Orientation gizmo: vtkmodules import failed: %s", exc)
            return
        try:
            vtk_iren = plotter.interactor.GetRenderWindow().GetInteractor()
        except Exception:
            log.debug("Orientation gizmo: no VTK interactor", exc_info=True)
            return
        if vtk_iren is None:
            return

        try:
            axes = vtkAxesActor()
        except Exception:
            log.debug("Orientation gizmo: vtkAxesActor() failed", exc_info=True)
            return

        try:
            red = _hex_to_rgb_floats("#f38ba8")
            green = _hex_to_rgb_floats("#a6e3a1")
            blue = _hex_to_rgb_floats("#89b4fa")
            axes.GetXAxisShaftProperty().SetColor(*red)
            axes.GetXAxisTipProperty().SetColor(*red)
            axes.GetYAxisShaftProperty().SetColor(*green)
            axes.GetYAxisTipProperty().SetColor(*green)
            axes.GetZAxisShaftProperty().SetColor(*blue)
            axes.GetZAxisTipProperty().SetColor(*blue)
        except Exception:
            log.debug("Orientation gizmo: shaft/tip color failed",
                      exc_info=True)
        try:
            axes.GetXAxisCaptionActor2D().SetVisibility(0)
            axes.GetYAxisCaptionActor2D().SetVisibility(0)
            axes.GetZAxisCaptionActor2D().SetVisibility(0)
        except Exception:
            log.debug("Orientation gizmo: hiding captions failed",
                      exc_info=True)

        try:
            widget = vtkOrientationMarkerWidget()
            widget.SetOrientationMarker(axes)
            widget.SetInteractor(vtk_iren)
            widget.SetViewport(0.8, 0.8, 1.0, 1.0)  # top-right 20%
            widget.EnabledOn()
            widget.InteractiveOff()
        except Exception:
            log.debug("Orientation gizmo: widget enable failed",
                      exc_info=True)
            return

        self._orientation_widget = widget
        self._axes_actor = axes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_hint(self) -> None:
        self._overlay.setText(self.HINT_TEXT)
        self._stack.setCurrentWidget(self._overlay)

    def show_loading(self, message: str = "Loading model…") -> None:
        self._overlay.setText(message)
        self._stack.setCurrentWidget(self._overlay)

    def show_error(self, message: str) -> None:
        self._overlay.setText(message)
        self._overlay.setStyleSheet(
            f"background-color: {VIEWPORT_BG}; color: #f38ba8; font-size: 14px;"
        )
        self._stack.setCurrentWidget(self._overlay)

    def has_mesh(self) -> bool:
        return self._has_mesh

    # ------------------------------------------------------------------
    # Blender-addon notice
    # ------------------------------------------------------------------

    def set_blender_notice_visible(self, visible: bool) -> None:
        """Show or hide the top-left Blender addon notice overlay."""
        if visible:
            self._blender_notice.show()
            self._reposition_blender_notice()
        else:
            self._blender_notice.hide()

    # ------------------------------------------------------------------
    # Model-loading indicator
    # ------------------------------------------------------------------

    def start_model_loading(self, count: int = 1) -> None:
        """Show the floating model-loading spinner with a count."""
        noun = "model" if count == 1 else "models"
        self._model_indicator.start(f"{count} {noun} loading")
        self._reposition_indicators()

    def update_model_loading_count(self, count: int) -> None:
        """Update the model count mid-flight."""
        if self._model_indicator.is_active():
            noun = "model" if count == 1 else "models"
            self._model_indicator.set_message(f"{count} {noun} loading")

    def stop_model_loading(self) -> None:
        self._model_indicator.stop()
        # Reposition the texture indicator in case it's still visible —
        # it should slide down once the model indicator disappears.
        self._reposition_indicators()

    # ------------------------------------------------------------------
    # Texture-loading indicator
    # ------------------------------------------------------------------

    def start_texture_loading(self, message: str = "Resolving textures…") -> None:
        """Show the floating spinner with ``message`` underneath."""
        self._texture_indicator.start(message)
        self._reposition_indicators()

    def update_texture_loading_message(self, message: str) -> None:
        """Update the spinner's caption mid-flight (e.g. progress text)."""
        if self._texture_indicator.is_active():
            self._texture_indicator.set_message(message)

    def stop_texture_loading(self) -> None:
        self._texture_indicator.stop()

    # ------------------------------------------------------------------
    # Indicator positioning
    # ------------------------------------------------------------------

    def _reposition_indicators(self) -> None:
        """Pin indicators to the bottom-center, model above texture."""
        margin_bottom = 16
        spacing = 6

        # Position texture indicator at the bottom.
        tex = self._texture_indicator
        mdl = self._model_indicator

        if tex is not None and tex.is_active():
            tex.adjustSize()
            tex_size = tex.size()
            tex_x = max(0, (self.width() - tex_size.width()) // 2)
            tex_y = max(0, self.height() - tex_size.height() - margin_bottom)
            tex.move(tex_x, tex_y)
            # Model indicator goes above the texture indicator.
            if mdl is not None and mdl.is_active():
                mdl.adjustSize()
                mdl_size = mdl.size()
                mdl_x = max(0, (self.width() - mdl_size.width()) // 2)
                mdl_y = max(0, tex_y - mdl_size.height() - spacing)
                mdl.move(mdl_x, mdl_y)
        elif mdl is not None and mdl.is_active():
            # Only model indicator visible — pin to bottom-center.
            mdl.adjustSize()
            mdl_size = mdl.size()
            mdl_x = max(0, (self.width() - mdl_size.width()) // 2)
            mdl_y = max(0, self.height() - mdl_size.height() - margin_bottom)
            mdl.move(mdl_x, mdl_y)

    def _reposition_texture_indicator(self) -> None:
        """Legacy alias — delegates to unified repositioning."""
        self._reposition_indicators()

    def _reposition_blender_notice(self) -> None:
        """Pin the Blender notice to the top-left of the viewport."""
        notice = self._blender_notice
        if notice.isVisible():
            notice.adjustSize()
            notice.move(10, 10)

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt API
        super().resizeEvent(event)
        self._reposition_indicators()
        self._reposition_blender_notice()

    def load_mesh(self, mesh_data: "MeshData") -> None:
        """Replace the current scene with ``mesh_data``.

        MUST be called from the main (Qt) thread — VTK actor creation is
        not thread-safe. Each submesh becomes its own PolyData + actor
        so :meth:`apply_submesh_textures` can later swap individual
        actors for textured variants.
        """
        if not self._ensure_plotter():
            return

        plotter = self._plotter
        assert plotter is not None

        plotter.clear()
        # ``plotter.clear()`` also drops 2D text actors from the
        # renderer, so re-install the F-key hint for the new scene.
        self._install_reset_hint_actor()
        self._mesh_data = None
        self._submesh_meshes = {}
        self._submesh_actors = {}
        # A new model invalidates any in-flight animation playback.
        self._anim_decoded = None
        self._anim_rest_positions_d4 = None
        self._anim_joints_global = None
        self._anim_weights = None
        self._anim_inv_bind = None
        self._anim_rest_local = None
        self._anim_hash_to_idx = {}
        self._anim_current_frame = 0
        # Clear overlay tracking — plotter.clear() already removed the
        # VTK actors but the Python dicts would go stale.
        if hasattr(self, "_overlay_submesh_meshes"):
            self._overlay_submesh_meshes.clear()
            self._overlay_submesh_actors.clear()

        positions = _convert_positions(mesh_data.positions)
        normals = _convert_normals(mesh_data.normals)
        indices_arr = (
            np.asarray(mesh_data.indices, dtype=np.int64)
            if mesh_data.indices else np.zeros((0,), dtype=np.int64)
        )
        uvs_arr = (
            np.asarray(mesh_data.uvs, dtype=np.float32)
            if mesh_data.uvs else np.zeros((0, 2), dtype=np.float32)
        )

        n_verts = positions.shape[0]
        if n_verts == 0 or indices_arr.size == 0:
            self.show_error(f"Model '{mesh_data.name}' has no renderable geometry.")
            self._has_mesh = False
            return

        try:
            import pyvista as pv  # noqa: F401  (used in helpers below)
        except Exception as exc:
            log.error("pyvista import failed: %s", exc)
            self.show_error(f"3D viewport unavailable: {exc}")
            return

        submeshes = list(mesh_data.submeshes or [])
        if submeshes:
            ok = self._add_submesh_actors(
                positions, normals, uvs_arr, indices_arr, submeshes,
            )
        else:
            ok = self._add_single_actor(positions, normals, indices_arr)

        if not ok:
            return

        plotter.reset_camera()
        # Keep the orbit basis pinned to Y-up after the bounds-driven
        # camera reset so drag direction stays consistent across loads.
        self._reorient_camera_to_y_up()
        self._mesh_data = mesh_data
        self._has_mesh = True
        self._stack.setCurrentWidget(self._plotter_host)

    def apply_submesh_textures(
        self,
        mesh_data: "MeshData",
        submesh_textures: dict[int, np.ndarray],
    ) -> None:
        """Attach decoded base-color textures to per-submesh actors.

        Must be called from the main (Qt) thread. ``mesh_data`` is the
        token the caller received from the texture worker — comparing
        it identity-wise against the currently-loaded mesh discards
        stale results (the user may have selected a different model
        while the worker was running). Submeshes whose entry is missing
        from ``submesh_textures`` keep their flat-color fallback actor.
        """
        if self._plotter is None or not self._has_mesh:
            return
        if mesh_data is not self._mesh_data:
            return
        if not submesh_textures:
            return
        try:
            import pyvista as pv
        except Exception as exc:
            log.warning("pyvista unavailable for texture apply: %s", exc)
            return

        log.info(
            "apply_submesh_textures: incoming=%d known_actors=%d",
            len(submesh_textures), len(self._submesh_actors),
        )

        plotter = self._plotter
        applied = 0
        for sm_idx, arr in submesh_textures.items():
            sub_mesh = self._submesh_meshes.get(sm_idx)
            actor = self._submesh_actors.get(sm_idx)
            if sub_mesh is None or actor is None:
                log.warning(
                    "submesh %d skipped: mesh=%s actor=%s",
                    sm_idx, sub_mesh is not None, actor is not None,
                )
                continue
            # The submesh PolyData needs UVs for the texture to bind. If
            # parsing didn't yield a UV stream for this primitive we
            # leave it on the flat-color actor.
            if not _has_active_texcoords(sub_mesh):
                log.info(
                    "submesh %d has no active UVs — keeping flat color",
                    sm_idx,
                )
                continue

            try:
                tex = pv.Texture(arr)
            except Exception as exc:
                log.warning("pv.Texture failed for submesh %d: %s", sm_idx, exc)
                continue

            # ``name=…`` makes pyvista atomically remove the prior
            # actor registered under the same key before adding the
            # replacement. This is more reliable than calling
            # ``remove_actor(actor_handle)`` separately, which depends
            # on the Python actor reference still matching the
            # internal renderer registry — a fragile assumption after
            # Qt/VTK roundtrips.
            actor_name = _actor_name(sm_idx)
            try:
                new_actor = plotter.add_mesh(
                    sub_mesh,
                    texture=tex,
                    show_edges=False,
                    smooth_shading=True,
                    name=actor_name,
                )
            except Exception as exc:
                log.exception(
                    "plotter.add_mesh(textured) failed for submesh %d: %s",
                    sm_idx, exc,
                )
                # Restore a flat-color actor under the same name so the
                # submesh doesn't disappear from the scene.
                try:
                    new_actor = plotter.add_mesh(
                        sub_mesh,
                        color=_color_for_material(
                            self._material_index_for(sm_idx),
                        ),
                        show_edges=False,
                        smooth_shading=True,
                        name=actor_name,
                    )
                except Exception:
                    new_actor = None
                if new_actor is None:
                    self._submesh_actors.pop(sm_idx, None)
                    continue

            self._submesh_actors[sm_idx] = new_actor
            applied += 1

        log.info(
            "apply_submesh_textures: applied=%d/%d",
            applied, len(submesh_textures),
        )
        if applied:
            try:
                plotter.render()
            except Exception:
                log.debug("plotter.render after texture apply failed")

    def toggle_wireframe(self) -> None:
        if self._plotter is None or not self._has_mesh:
            return
        try:
            actors = self._plotter.renderer.actors
        except AttributeError:
            return
        for actor in list(actors.values()):
            try:
                prop = actor.GetProperty()
            except AttributeError:
                continue
            try:
                # 0 = points, 1 = wireframe, 2 = surface
                if prop.GetRepresentation() == 2:
                    prop.SetRepresentationToWireframe()
                else:
                    prop.SetRepresentationToSurface()
            except Exception:
                continue
        try:
            self._plotter.render()
        except Exception:
            pass

    def reset_view(self) -> None:
        if self._plotter is None or not self._has_mesh:
            return
        try:
            self._plotter.reset_camera()
            self._reorient_camera_to_y_up()
            self._plotter.render()
        except Exception:
            pass

    def set_submesh_visible(self, sm_idx: int, visible: bool) -> None:
        """Toggle visibility of the actor backing a single submesh."""
        actor = self._submesh_actors.get(sm_idx)
        if actor is None:
            return
        actor.SetVisibility(1 if visible else 0)
        if self._plotter is not None:
            try:
                self._plotter.render()
            except Exception:
                pass

    def set_all_submeshes_visible(self, visible: bool) -> None:
        """Toggle visibility of every per-submesh actor in one render."""
        for actor in self._submesh_actors.values():
            actor.SetVisibility(1 if visible else 0)
        if self._plotter is not None:
            try:
                self._plotter.render()
            except Exception:
                pass

    def set_overlay_submesh_visible(
        self, tag: str, sm_idx: int, visible: bool,
    ) -> None:
        """Toggle visibility of a single overlay submesh actor."""
        if not hasattr(self, "_overlay_submesh_actors"):
            return
        tag_actors = self._overlay_submesh_actors.get(tag)
        if not tag_actors:
            return
        actor = tag_actors.get(sm_idx)
        if actor is None:
            return
        actor.SetVisibility(1 if visible else 0)
        if self._plotter is not None:
            try:
                self._plotter.render()
            except Exception:
                pass

    def set_all_overlay_submeshes_visible(
        self, tag: str, visible: bool,
    ) -> None:
        """Toggle visibility of every actor under one overlay ``tag``."""
        if not hasattr(self, "_overlay_submesh_actors"):
            return
        tag_actors = self._overlay_submesh_actors.get(tag)
        if not tag_actors:
            return
        for actor in tag_actors.values():
            actor.SetVisibility(1 if visible else 0)
        if self._plotter is not None:
            try:
                self._plotter.render()
            except Exception:
                pass

    def shutdown(self) -> None:
        """Dispose of the plotter cleanly on window close."""
        try:
            self._texture_indicator.stop()
        except Exception:
            pass
        # Disable the orientation widget before the plotter / interactor
        # tears down — VTK warns (and in some builds aborts) if a widget
        # is destroyed while still attached to a live render window.
        if self._orientation_widget is not None:
            try:
                self._orientation_widget.EnabledOff()
            except Exception:
                pass
            self._orientation_widget = None
        self._axes_actor = None
        # Drop wheel observers before the plotter tears down — VTK
        # warns when callbacks outlive the object they observe.
        for obj, tag in self._wheel_observer_tags:
            try:
                obj.RemoveObserver(tag)
            except Exception:
                pass
        self._wheel_observer_tags = []
        if self._plotter is not None:
            try:
                self._plotter.close()
            except Exception:
                pass
            self._plotter = None
            self._has_mesh = False
            self._mesh_data = None
            self._submesh_meshes = {}
            self._submesh_actors = {}
            self._anim_decoded = None
            self._anim_rest_positions_d4 = None
            self._anim_joints_global = None
            self._anim_weights = None
            self._anim_inv_bind = None
            self._anim_rest_local = None
            self._anim_hash_to_idx = {}

    # ------------------------------------------------------------------
    # Animation playback
    # ------------------------------------------------------------------

    def set_animation(
        self,
        decoded: object,
        mesh_data: "MeshData",
    ) -> bool:
        """Wire ``decoded`` (a ``DecodedAnimation``) onto the loaded mesh.

        Pre-computes everything the per-frame skin pass needs:
        - rest positions in **D4-native** coordinates (skinning math
          runs in D4 space; the axis swap happens once on the output).
        - global per-vertex bone indices (palette → skeleton remap).
        - rest-pose local matrices and the parser-supplied inv-bind
          matrices, both as ``(B, 4, 4)`` float32 arrays.
        - the ``bone_hash → DecodedBoneAnimation index`` map.

        Returns ``False`` if the mesh isn't skinned or the data shape
        looks wrong.  In that case the caller should keep the mesh on
        its rest-pose render and surface a hint.
        """
        from d4extract.gui import skinning  # lazy: avoids numpy at GUI import time

        if mesh_data is None:
            return False
        if mesh_data is not self._mesh_data or not self._has_mesh:
            log.debug("set_animation: mesh mismatch / not loaded")
            return False
        skel = getattr(mesh_data, "skeleton", None)
        bones = getattr(skel, "bones", None) if skel is not None else None
        if not bones:
            log.info("set_animation: model has no skeleton — skipping")
            return False
        if not getattr(mesh_data, "joints", None) or not getattr(
            mesh_data, "weights", None,
        ):
            log.info(
                "set_animation: mesh has no JOINTS_0/WEIGHTS_0 stream — "
                "rendering rest pose"
            )
            return False

        # Cache the rest-pose positions in D4 space so we don't have to
        # un-convert from VTK coords each frame.
        rest_d4 = np.asarray(mesh_data.positions, dtype=np.float32)
        if rest_d4.size == 0:
            return False

        joints_global = skinning.remap_joints_to_global(
            mesh_data.joints,
            list(mesh_data.submeshes or []),
            len(bones),
        )
        weights = np.asarray(mesh_data.weights, dtype=np.float32)
        if joints_global.shape[0] != rest_d4.shape[0]:
            log.warning(
                "joints/positions length mismatch: %d vs %d — disabling animation",
                joints_global.shape[0], rest_d4.shape[0],
            )
            return False
        if weights.shape[0] != rest_d4.shape[0]:
            log.warning(
                "weights/positions length mismatch: %d vs %d — disabling animation",
                weights.shape[0], rest_d4.shape[0],
            )
            return False

        try:
            inv_bind = skinning.build_inv_bind_matrices(skel)
            rest_local = skinning.build_rest_local_matrices(skel)
        except Exception:
            log.exception("Failed to build skin matrices")
            return False

        self._anim_decoded = decoded
        self._anim_rest_positions_d4 = rest_d4
        self._anim_joints_global = joints_global
        self._anim_weights = weights
        self._anim_inv_bind = inv_bind
        self._anim_rest_local = rest_local
        self._anim_hash_to_idx = skinning.build_bone_hash_to_anim_map(decoded)
        self._anim_current_frame = 0

        # Drop the viewport on frame 0 immediately so the user sees the
        # animation as soon as they pick it from the dropdown — no need
        # to also click Play.
        try:
            self.show_frame(0)
        except Exception:
            log.exception("Initial show_frame(0) failed")
            return False
        return True

    def show_frame(self, frame: int) -> None:
        """Update the per-submesh PolyData for the given animation frame."""
        if (
            self._anim_decoded is None
            or self._anim_rest_positions_d4 is None
            or self._anim_joints_global is None
            or self._anim_weights is None
            or self._anim_inv_bind is None
            or self._anim_rest_local is None
        ):
            return
        if self._mesh_data is None or self._plotter is None:
            return

        decoded = self._anim_decoded
        frame_count = decoded.frame_count
        if frame_count <= 0:
            return
        # Clamp + wrap so the caller doesn't have to.
        if frame < 0 or frame >= frame_count:
            frame = frame % frame_count

        from d4extract.gui import skinning

        skel = self._mesh_data.skeleton

        try:
            world = skinning.compute_world_matrices(
                skel,
                decoded,
                frame,
                self._anim_hash_to_idx,
                self._anim_rest_local,
            )
            skin_mats = skinning.compute_skin_matrices(
                world, self._anim_inv_bind,
            )
            skinned_d4 = skinning.skin_vertices(
                self._anim_rest_positions_d4,
                self._anim_joints_global,
                self._anim_weights,
                skin_mats,
            )
        except Exception:
            log.exception("Skinning failed at frame %d", frame)
            return

        # D4 -> VTK once, on the final vertex stream.  Same axis swap
        # the loader uses, so the rest-pose viewport and animated
        # viewport stay in the same coordinate system.
        skinned_vtk = skinned_d4[:, [0, 2, 1]].copy()
        skinned_vtk[:, 2] = -skinned_vtk[:, 2]

        # Push per-submesh slices.  Index range [vo : vo+vc] mirrors
        # what ``_build_submesh_polydata`` carved out at load time.
        for sm_idx, sub_mesh in self._submesh_meshes.items():
            try:
                sm = self._mesh_data.submeshes[sm_idx]
            except (IndexError, TypeError):
                continue
            vo = max(0, sm.vertex_offset)
            vc = max(0, sm.vertex_count)
            if vc <= 0 or vo + vc > skinned_vtk.shape[0]:
                continue
            try:
                sub_mesh.points = skinned_vtk[vo:vo + vc]
            except Exception:
                log.exception("Could not push points for submesh %d", sm_idx)

        self._anim_current_frame = frame
        try:
            self._plotter.render()
        except Exception:
            log.debug("plotter.render() failed during animation", exc_info=True)

    def clear_animation(self) -> None:
        """Drop the animation and restore the rest-pose vertex stream."""
        if (
            self._anim_decoded is None
            and self._anim_rest_positions_d4 is None
        ):
            return
        # Restore VTK-space rest positions per submesh so the model
        # snaps back to the bind pose.
        if self._mesh_data is not None and self._plotter is not None:
            rest = _convert_positions(self._mesh_data.positions)
            for sm_idx, sub_mesh in self._submesh_meshes.items():
                try:
                    sm = self._mesh_data.submeshes[sm_idx]
                except (IndexError, TypeError):
                    continue
                vo = max(0, sm.vertex_offset)
                vc = max(0, sm.vertex_count)
                if vc <= 0 or vo + vc > rest.shape[0]:
                    continue
                try:
                    sub_mesh.points = rest[vo:vo + vc]
                except Exception:
                    log.exception(
                        "Could not restore rest pose for submesh %d", sm_idx,
                    )
            try:
                self._plotter.render()
            except Exception:
                log.debug("plotter.render() failed during rest-pose restore",
                          exc_info=True)

        self._anim_decoded = None
        self._anim_rest_positions_d4 = None
        self._anim_joints_global = None
        self._anim_weights = None
        self._anim_inv_bind = None
        self._anim_rest_local = None
        self._anim_hash_to_idx = {}
        self._anim_current_frame = 0

    # ------------------------------------------------------------------
    # Internals — actor construction
    # ------------------------------------------------------------------

    def _add_submesh_actors(
        self,
        positions: np.ndarray,
        normals: np.ndarray,
        uvs: np.ndarray,
        indices: np.ndarray,
        submeshes: list["Submesh"],
    ) -> bool:
        """Build one PolyData + actor per submesh. Returns True on success."""
        import pyvista as pv

        plotter = self._plotter
        assert plotter is not None

        any_added = False
        for sm_idx, sm in enumerate(submeshes):
            sub_mesh = self._build_submesh_polydata(
                positions, normals, uvs, indices, sm,
            )
            if sub_mesh is None:
                continue
            try:
                actor = plotter.add_mesh(
                    sub_mesh,
                    color=_color_for_material(sm.material_index),
                    show_edges=False,
                    smooth_shading=True,
                    # Named so a later texture-apply pass can swap this
                    # actor reliably via ``add_mesh(..., name=…)``,
                    # which removes any prior actor with the same name
                    # before adding the replacement.
                    name=_actor_name(sm_idx),
                )
            except Exception as exc:
                log.exception("plotter.add_mesh failed (submesh %d): %s",
                              sm_idx, exc)
                continue
            self._submesh_meshes[sm_idx] = sub_mesh
            self._submesh_actors[sm_idx] = actor
            any_added = True

        if not any_added:
            self.show_error("Model has no renderable submeshes.")
            self._has_mesh = False
            return False
        return True

    def _add_single_actor(
        self,
        positions: np.ndarray,
        normals: np.ndarray,
        indices: np.ndarray,
    ) -> bool:
        """Fallback path for fixtures that don't carry submesh ranges.

        The mock-mode synthetic mesh always populates submeshes, so this
        only fires for legacy tests / pathological models. Renders one
        flat-coloured PolyData covering the whole vertex stream.
        """
        import pyvista as pv

        plotter = self._plotter
        assert plotter is not None

        faces = _build_face_array(indices)
        if faces.size == 0:
            self.show_error("Model has no renderable triangles.")
            self._has_mesh = False
            return False

        mesh = pv.PolyData(positions, faces)
        if normals.shape[0] == positions.shape[0]:
            mesh.point_data["Normals"] = normals

        try:
            plotter.add_mesh(
                mesh,
                color="lightgray",
                show_edges=False,
                smooth_shading=True,
            )
        except Exception as exc:
            log.exception("plotter.add_mesh failed: %s", exc)
            self.show_error(f"Failed to render model: {exc}")
            return False
        return True

    def _build_submesh_polydata(
        self,
        positions: np.ndarray,
        normals: np.ndarray,
        uvs: np.ndarray,
        indices: np.ndarray,
        submesh: "Submesh",
    ):
        """Slice the global vertex / index streams down to one submesh.

        Same approach the glTF exporter uses: pull the ``vc`` vertices
        starting at ``vo``, take ``ic`` indices starting at ``io``, and
        re-base the indices so they address the slice instead of the
        global stream. UVs are attached as the active texture
        coordinates so :meth:`apply_submesh_textures` can later bind a
        ``pv.Texture`` without rebuilding the mesh.
        """
        import pyvista as pv

        vo = max(0, submesh.vertex_offset)
        vc = max(0, submesh.vertex_count)
        io = max(0, submesh.index_offset)
        ic = max(0, submesh.index_count)

        n_verts_total = positions.shape[0]
        if vc <= 0 or ic < 3 or vo + vc > n_verts_total:
            return None
        if io + ic > indices.size:
            ic = indices.size - io
            if ic < 3:
                return None

        sub_positions = positions[vo:vo + vc]
        # Re-base indices to address the submesh-local vertex array.
        # Out-of-range indices (some D4 models — see project memory on
        # the Goatman local indexing issue) are clipped so VTK doesn't
        # crash when building the cell array.
        sub_indices = indices[io:io + ic] - vo
        np.clip(sub_indices, 0, vc - 1, out=sub_indices)
        n_tris = sub_indices.size // 3
        if n_tris == 0:
            return None

        faces = _build_face_array(sub_indices)
        mesh = pv.PolyData(sub_positions, faces)

        if normals.shape[0] >= vo + vc:
            sub_normals = normals[vo:vo + vc]
            if sub_normals.shape[0] == vc:
                mesh.point_data["Normals"] = sub_normals

        if uvs.shape[0] >= vo + vc:
            sub_uvs = uvs[vo:vo + vc]
            if sub_uvs.shape[0] == vc:
                _set_texture_coordinates(mesh, sub_uvs)

        return mesh

    # ------------------------------------------------------------------
    # Overlay meshes (static / unskinned pieces rendered alongside the
    # primary skinned assembly — e.g. weapons)
    # ------------------------------------------------------------------

    def add_overlay_mesh(
        self, mesh_data: "MeshData", *, tag: str = "overlay",
    ) -> bool:
        """Add ``mesh_data`` to the current scene without clearing.

        Submesh actors are named ``{tag}_{i}`` so they can coexist with
        the primary mesh's ``d4_submesh_*`` actors.  Returns ``True`` if
        at least one actor was added.

        Overlay submesh meshes and actors are tracked in
        ``_overlay_submesh_meshes`` / ``_overlay_submesh_actors`` so
        :meth:`apply_overlay_textures` can later swap them for textured
        variants — mirroring how the primary mesh's texture pipeline
        works.

        If no primary mesh has been loaded yet (e.g. only static weapons
        with no skinned body), ``load_mesh`` must be called first — this
        method only *adds to* an existing scene.  If the plotter isn't
        active this is a silent no-op.
        """
        if self._plotter is None:
            return False

        positions = _convert_positions(mesh_data.positions)
        normals = _convert_normals(mesh_data.normals)
        indices_arr = (
            np.asarray(mesh_data.indices, dtype=np.int64)
            if mesh_data.indices else np.zeros((0,), dtype=np.int64)
        )
        uvs_arr = (
            np.asarray(mesh_data.uvs, dtype=np.float32)
            if mesh_data.uvs else np.zeros((0, 2), dtype=np.float32)
        )

        if positions.shape[0] == 0 or indices_arr.size == 0:
            return False

        try:
            import pyvista as pv  # noqa: F401
        except Exception:
            return False

        plotter = self._plotter
        any_added = False
        submeshes = list(mesh_data.submeshes or [])
        # Initialise tracking dicts for this tag.
        if not hasattr(self, "_overlay_submesh_meshes"):
            self._overlay_submesh_meshes: dict[str, dict[int, object]] = {}
            self._overlay_submesh_actors: dict[str, dict[int, object]] = {}
        tag_meshes: dict[int, object] = {}
        tag_actors: dict[int, object] = {}

        for sm_idx, sm in enumerate(submeshes):
            sub_mesh = self._build_submesh_polydata(
                positions, normals, uvs_arr, indices_arr, sm,
            )
            if sub_mesh is None:
                continue
            actor_name = f"{tag}_{sm_idx}"
            try:
                actor = plotter.add_mesh(
                    sub_mesh,
                    color=_color_for_material(sm.material_index),
                    show_edges=False,
                    smooth_shading=True,
                    name=actor_name,
                )
            except Exception as exc:
                log.exception(
                    "add_overlay_mesh failed (%s, submesh %d): %s",
                    tag, sm_idx, exc,
                )
                continue
            tag_meshes[sm_idx] = sub_mesh
            tag_actors[sm_idx] = actor
            any_added = True

        if any_added:
            self._overlay_submesh_meshes[tag] = tag_meshes
            self._overlay_submesh_actors[tag] = tag_actors

        return any_added

    def apply_overlay_textures(
        self,
        tag: str,
        submesh_textures: dict[int, np.ndarray],
    ) -> None:
        """Attach decoded textures to an overlay mesh's submesh actors.

        Mirrors :meth:`apply_submesh_textures` but targets the overlay
        actors registered under ``tag`` by a prior
        :meth:`add_overlay_mesh` call.
        """
        if self._plotter is None or not self._has_mesh:
            return
        if not submesh_textures:
            return
        if not hasattr(self, "_overlay_submesh_meshes"):
            return
        tag_meshes = self._overlay_submesh_meshes.get(tag)
        tag_actors = self._overlay_submesh_actors.get(tag)
        if not tag_meshes or not tag_actors:
            return

        try:
            import pyvista as pv
        except Exception:
            return

        plotter = self._plotter
        applied = 0
        for sm_idx, arr in submesh_textures.items():
            sub_mesh = tag_meshes.get(sm_idx)
            if sub_mesh is None:
                continue
            if not _has_active_texcoords(sub_mesh):
                continue
            try:
                tex = pv.Texture(arr)
            except Exception as exc:
                log.warning(
                    "pv.Texture failed for overlay %s submesh %d: %s",
                    tag, sm_idx, exc,
                )
                continue
            actor_name = f"{tag}_{sm_idx}"
            try:
                new_actor = plotter.add_mesh(
                    sub_mesh,
                    texture=tex,
                    show_edges=False,
                    smooth_shading=True,
                    name=actor_name,
                )
            except Exception as exc:
                log.exception(
                    "Textured overlay add_mesh failed (%s_%d): %s",
                    tag, sm_idx, exc,
                )
                continue
            tag_actors[sm_idx] = new_actor
            applied += 1

        if applied:
            log.info(
                "apply_overlay_textures(%s): applied=%d/%d",
                tag, applied, len(submesh_textures),
            )
            try:
                plotter.render()
            except Exception:
                pass

    def remove_overlay_actors(self, tag: str = "overlay") -> None:
        """Remove all actors whose name starts with ``{tag}_``.

        Called before a rebuild to clear stale weapon actors before
        adding fresh ones.
        """
        if self._plotter is None:
            return
        prefix = f"{tag}_"
        plotter = self._plotter
        try:
            names = [
                n for n in list(plotter.renderer.actors.keys())
                if isinstance(n, str) and n.startswith(prefix)
            ]
            for name in names:
                plotter.remove_actor(name)
        except Exception:
            log.debug("remove_overlay_actors(%s) failed", tag, exc_info=True)
        if hasattr(self, "_overlay_submesh_meshes"):
            to_remove = [
                k for k in self._overlay_submesh_meshes
                if k == tag or k.startswith(prefix)
            ]
            for k in to_remove:
                self._overlay_submesh_meshes.pop(k, None)
                self._overlay_submesh_actors.pop(k, None)

    def _material_index_for(self, sm_idx: int) -> int:
        if self._mesh_data is None:
            return sm_idx
        try:
            return self._mesh_data.submeshes[sm_idx].material_index
        except (AttributeError, IndexError):
            return sm_idx


def _has_active_texcoords(mesh) -> bool:
    """Whether ``mesh`` has UVs that VTK can use for texture mapping."""
    for attr in ("active_texture_coordinates", "active_t_coords"):
        try:
            coords = getattr(mesh, attr)
        except (AttributeError, TypeError):
            continue
        if coords is None:
            continue
        try:
            shape = coords.shape
        except AttributeError:
            continue
        if len(shape) == 2 and shape[0] > 0 and shape[1] >= 2:
            return True
    return False
