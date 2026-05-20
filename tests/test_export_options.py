"""Tests for the "Include Animations" export toggle.

Two halves:
  * ``EXPORT_OPTIONS`` carries the toggle, so the export menu surfaces a
    checkbox and ``get_export_options()`` emits ``include_animations``.
  * ``D4ExportWindow._gate_animations`` consumes it — when off, the worker
    is handed ``anim_infos=None`` / ``rest_pose_map=None`` so no
    animations (not even the synthetic rest_pose) are decoded or
    embedded.

``D4ExportWindow`` is a ``QMainWindow``; ``_gate_animations`` is a
``@staticmethod``, so it is exercised without constructing the window
(no QApplication needed).
"""

from __future__ import annotations

from d4extract.gui.main_window import D4ExportWindow
from d4extract.gui.widgets.export_button import EXPORT_OPTIONS


def test_include_animations_option_registered():
    """The toggle is in EXPORT_OPTIONS so the export menu builds a
    checkbox for it and get_export_options() reports its state."""
    by_key = {o.key: o for o in EXPORT_OPTIONS}
    assert "include_animations" in by_key
    opt = by_key["include_animations"]
    assert opt.label == "Include Animations"
    assert opt.kwarg == "include_animations"
    assert opt.default is True  # default ON — current behavior preserved


def test_export_without_animations_passes_none_to_worker():
    """'Include Animations' unchecked → both anim_infos and
    rest_pose_map drop to None, so the worker decodes no .ani files and
    emits a glb with zero animations (incl. no synthetic rest_pose)."""
    anim_infos = ["clip_a", "clip_b"]
    rest_pose_map = {0xAAA: ((0, 0, 0, 1), (0, 0, 0), (1, 1, 1))}
    out_infos, out_rpm = D4ExportWindow._gate_animations(
        {"include_animations": False}, anim_infos, rest_pose_map,
    )
    assert out_infos is None
    assert out_rpm is None


def test_export_with_animations_default_passes_populated_lists():
    """Default (checked) preserves existing behavior: anim_infos and
    rest_pose_map pass through untouched."""
    anim_infos = ["clip_a", "clip_b"]
    rest_pose_map = {0xAAA: ((0, 0, 0, 1), (0, 0, 0), (1, 1, 1))}
    out_infos, out_rpm = D4ExportWindow._gate_animations(
        {"include_animations": True}, anim_infos, rest_pose_map,
    )
    assert out_infos is anim_infos
    assert out_rpm is rest_pose_map


def test_gate_animations_defaults_to_included_when_key_absent():
    """An options dict with no 'include_animations' key behaves as
    include — the toggle never silently drops animations by omission."""
    anim_infos = ["clip"]
    rest_pose_map = {1: "x"}
    out_infos, out_rpm = D4ExportWindow._gate_animations(
        {}, anim_infos, rest_pose_map,
    )
    assert out_infos is anim_infos
    assert out_rpm is rest_pose_map
