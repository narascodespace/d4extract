"""Tests for the body-skin selection behind the armor_skin_mat swap.

``swap_armor_skin_materials`` itself manipulates ``bpy.data`` and is
verified by the headless Blender harness (like all addon bpy code). Its
pure, testable core — picking the body skin material out of the sidecar
— lives on ``Sidecar.body_skin_materials()``. That method is in
``blender_addon/core/sidecar.py``, which is deliberately ``bpy``-free;
it is loaded here straight from its file path so the addon package's
``__init__`` (which pulls ``bpy``) is never triggered.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SIDECAR_PY = (
    Path(__file__).resolve().parents[1]
    / "blender_addon" / "core" / "sidecar.py"
)


def _load_sidecar_module():
    """Import ``blender_addon/core/sidecar.py`` standalone (no bpy)."""
    spec = importlib.util.spec_from_file_location(
        "d4_addon_sidecar_under_test", _SIDECAR_PY,
    )
    module = importlib.util.module_from_spec(spec)
    # Register before exec: sidecar.py uses @dataclass(slots=True), and
    # dataclasses resolves the owning module via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sc = _load_sidecar_module()


def _tex(role: str, slot: int = 0):
    return sc.SidecarTexture(
        role=role, slot=slot, sno_id=0, path=f"{role}.tex",
        width=0, height=0, format=0,
    )


def _mat(name: str, shader: str, textures=()):
    return sc.SidecarMaterial(
        name=name, sno_id=0, shader_map=shader,
        base_color_factor=(1.0, 1.0, 1.0, 1.0),
        emissive_factor=(0.0, 0.0, 0.0),
        metallic_factor=1.0, roughness_factor=1.0, is_cloth_only=False,
        textures=tuple(textures),
    )


def _sidecar(materials):
    return sc.Sidecar(path="x", materials=tuple(materials), variants={})


# A head skin uses the same shader as the body but has no SKIN_MASK.
_HEAD = _mat("spiM_P01_HED_mat", "hero_opaque_skin", [_tex("BASE_COLOR")])
_BODY = _mat(
    "spiM_P00_BOD_mat", "hero_opaque_skin",
    [_tex("BASE_COLOR"), _tex("SKIN_MASK", slot=145)],
)
_ARMOR = _mat("armor_skin_mat", "hero_opaque", [_tex("BASE_COLOR")])
_PLATE = _mat("plate_mat", "hero_opaque", [_tex("BASE_COLOR")])


def test_body_skin_materials_picks_the_body():
    """The SKIN_MASK-bearing material is the body; the head is not."""
    side = _sidecar([_HEAD, _BODY, _ARMOR, _PLATE])
    body = side.body_skin_materials()
    assert body == (_BODY,)


def test_body_skin_materials_head_only_is_empty():
    """Head skin alone (no SKIN_MASK) is not a body-skin candidate."""
    side = _sidecar([_HEAD, _ARMOR, _PLATE])
    assert side.body_skin_materials() == ()


def test_body_skin_materials_none_when_no_skin():
    """A monster / weapon-only import has no body skin material."""
    side = _sidecar([_ARMOR, _PLATE])
    assert side.body_skin_materials() == ()


def test_body_skin_materials_multi_region_returns_all():
    """Multi-region characters surface every candidate (caller picks 1st)."""
    body2 = _mat(
        "spiM_P00_BOD2_mat", "hero_opaque_skin",
        [_tex("SKIN_MASK", slot=145)],
    )
    side = _sidecar([_HEAD, _BODY, body2, _ARMOR])
    assert side.body_skin_materials() == (_BODY, body2)


def test_body_skin_materials_shader_match_is_case_insensitive():
    side = _sidecar([
        _mat("BodyMat", "Hero_Opaque_Skin", [_tex("SKIN_MASK", slot=145)]),
    ])
    assert len(side.body_skin_materials()) == 1
