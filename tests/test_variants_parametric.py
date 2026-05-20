"""Tests for the typed (image_swap / parametric_hsv) variant schema."""

from __future__ import annotations

from pathlib import Path

import pytest

from d4extract.formats.hair_colors import HairColor
from d4extract.formats.material_parser import Material, TextureRef, load_materials
from d4extract.formats.skin_tones import SkinTone
from d4extract.formats.variants import (
    KIND_IMAGE_SWAP,
    KIND_PARAMETRIC_COLOR,
    KIND_PARAMETRIC_HSV,
    VariantCategory,
    discover_variants,
    is_hair_material,
    is_skin_material,
    variants_to_sidecar_block,
)

D4DATA = Path("../d4data/json")


def _material(name: str = "m", shader: str = "", textures=()) -> Material:
    """Build a minimal :class:`Material` for skin-detector tests."""
    return Material(
        name=name, sno_id=0, shader_map=shader, textures=list(textures),
        base_color_factor=(1.0, 1.0, 1.0, 1.0), emissive_factor=(0.0, 0.0, 0.0),
        metallic_factor=1.0, roughness_factor=1.0, is_cloth_only=False,
    )


# ─── is_skin_material — the parametric-skin detector ─────────────────

def test_is_skin_material_hero_opaque_skin():
    assert is_skin_material(_material(shader="hero_opaque_skin"))


def test_is_skin_material_armor_skin_mat():
    """Exposed-skin cutouts shipped with armor (plain hero_opaque shader)."""
    assert is_skin_material(_material(name="armor_skin_mat", shader="hero_opaque"))


def test_is_skin_material_underscore_skin_name():
    assert is_skin_material(_material(name="body_skin_cutout", shader="hero_opaque"))


def test_is_skin_material_skin_mask_texture():
    tex = TextureRef(
        slot=145, role="SKIN_MASK", sno_id=1, path="x.tex",
        width=0, height=0, format=0, avg_rgba=(1.0, 1.0, 1.0, 1.0),
    )
    assert is_skin_material(_material(shader="hero_opaque", textures=[tex]))


def test_is_skin_material_excludes_teeth_nocustomization():
    """``chr_opaque_skin_noCustomization`` (teeth/tongue) is not tintable."""
    assert not is_skin_material(
        _material(name="base_teethtongue_mat",
                  shader="chr_opaque_skin_noCustomization")
    )


def test_is_skin_material_excludes_skinenabled_vfx():
    """``skinEnabled`` is a mesh-skinning feature flag, not a skin surface."""
    assert not is_skin_material(
        _material(
            name="spiM_stor196_goldBlade",
            shader="vfx_actor_blend_uber_unlit_vertexAnim_skinEnabled"
                   "_twoSided_noDOF",
        )
    )


def test_is_skin_material_excludes_cloth():
    assert not is_skin_material(
        _material(name="cloth_skin_mat", shader="hero_opaque_skin")
    )


def test_is_skin_material_plain_armor_is_not_skin():
    assert not is_skin_material(_material(name="armor_plate", shader="hero_opaque"))


# ─── is_hair_material — the parametric-hair detector ─────────────────

def test_is_hair_material_hero_hair():
    assert is_hair_material(_material(shader="hero_hair"))


def test_is_hair_material_hero_hair_2uv_beard():
    """Full-beard multi-UV variant (e.g. druM_B07_mat) — the bug fix."""
    assert is_hair_material(_material(shader="hero_hair_2uv"))


def test_is_hair_material_hero_hair_future_variants():
    """The prefix match catches any future ``hero_hair_<suffix>`` variant."""
    assert is_hair_material(_material(shader="hero_hair_alpha"))
    assert is_hair_material(_material(shader="hero_hair_dual_layer"))


def test_is_hair_material_hair_pbr_igc():
    """The fine-hair shader (eyelashes) counts as hair (exact match)."""
    assert is_hair_material(_material(shader="hair_pbr_igc"))


def test_is_hair_material_case_insensitive():
    assert is_hair_material(_material(shader="Hero_Hair"))
    assert is_hair_material(_material(shader="HERO_HAIR_2UV"))
    assert is_hair_material(_material(shader="HAIR_PBR_IGC"))


def test_is_hair_material_excludes_skin_and_opaque():
    """``hero_opaque*`` shaders must not be caught by the hair prefix."""
    assert not is_hair_material(_material(shader="hero_opaque_skin"))
    assert not is_hair_material(_material(shader="hero_opaque"))
    assert not is_hair_material(_material(shader="hero_opaque_alphatest"))


def test_is_hair_material_excludes_unrelated_hair_substring():
    """A shader merely *containing* 'hair' but not the right prefix is out."""
    assert not is_hair_material(_material(shader="vfx_hair_blend_uber"))


def test_is_hair_material_empty_and_none_shader():
    """Defensive: empty / missing shader_map → False, no exceptions."""
    assert not is_hair_material(_material(shader=""))
    assert not is_hair_material(_material(shader=None))


# ─── Name-based safety net — helmet-attached hair ───────────────────


def test_is_hair_material_name_helmet_hair():
    """``*_Hair_mat`` name catches helmet hair with non-hero_hair shader.

    Real case: ``PalM_stor165_HLM_Hair_mat`` carries the
    ``hero_armorHair`` shader, so the shader-only check misses it.
    The name-based safety net picks it up so the tint chain still
    runs.
    """
    assert is_hair_material(_material(
        name="PalM_stor165_HLM_Hair_mat", shader="hero_armorHair",
    ))


def test_is_hair_material_name_is_case_insensitive():
    """The name suffix check is lower-cased before matching."""
    assert is_hair_material(_material(
        name="DruM_stor214_HLM_HAIR_mat", shader="hero_armorHair",
    ))


def test_is_hair_material_excludes_bartuc_helmet():
    """Bartuc unique-set helmet hair is fixed gear-design, not tinted.

    Its name carries no ``Hair`` token (``bartuc_druM_HLM_mat`` —
    the helmet shape *is* the hair) so it must stay out of the
    customization tint set, matching in-game behaviour.
    """
    assert not is_hair_material(_material(
        name="bartuc_druM_HLM_mat", shader="hero_opaque_alphatest",
    ))


def test_is_hair_material_excludes_regular_helmet():
    """A regular helmet (``*_HLM_mat`` without ``_Hair`` token) is not hair."""
    assert not is_hair_material(_material(
        name="warM_stor001_HLM_mat", shader="hero_opaque",
    ))


def test_is_hair_material_excludes_hair_adjacent_suffixes():
    """``*_hairpin_mat`` / ``*_hairlong_mat`` deliberately do not match.

    The name clause requires an underscore between ``hair`` and
    ``mat`` — these adjacent suffixes are jewelry (``_hairpin_mat``)
    or geometry hints (``_hairlong_mat`` / ``_hairshort_mat``) the
    audit confirmed are not hair-tint targets.
    """
    assert not is_hair_material(_material(
        name="npcf_ying_yue_HLM_hairpin_mat", shader="hero_opaque",
    ))
    assert not is_hair_material(_material(
        name="something_hairlong_mat", shader="hero_opaque",
    ))


# ─── parametric_color (hair) serialization ───────────────────────────

def _hair_color(id: str = "H01", **kw) -> HairColor:
    base = dict(
        id=id, display_name="DeepBrown", sort_order=0,
        usable_by=(1, 1, 1, 1, 1, 1, 1, 1),
        rgba_colors=(
            (0.0, 0.0, 0.0, 1.0), (0.1, 0.2, 0.3, 1.0), (0.4, 0.5, 0.6, 1.0),
        ),
        rgba_colors2=((0.7, 0.7, 0.7, 1.0),) * 3,
        influence=1.0,
    )
    base.update(kw)
    return HairColor(**base)


def test_parametric_color_block_serialization():
    """A parametric_color category serialises to the documented shape."""
    variants = {
        "hair_color": VariantCategory(
            KIND_PARAMETRIC_COLOR, [_hair_color(influence=0.5)], [9, 17],
        ),
        "makeup": VariantCategory(KIND_IMAGE_SWAP, []),
    }
    block = variants_to_sidecar_block(variants)

    hair = block["hair_color"]
    assert hair["kind"] == "parametric_color"
    assert hair["applies_to_materials"] == [9, 17]
    entry = hair["entries"][0]
    assert entry["id"] == "H01"
    assert entry["display_name"] == "DeepBrown"
    # ui_color is rgbaColors[0]; secondary/tertiary mirror [1]/[2].
    assert entry["ui_color"] == [0.0, 0.0, 0.0, 1.0]
    assert entry["secondary_color"] == [0.1, 0.2, 0.3, 1.0]
    assert entry["tertiary_color"] == [0.4, 0.5, 0.6, 1.0]
    assert len(entry["colors2"]) == 3
    assert entry["influence"] == 0.5
    assert entry["usable_by"] == [1] * 8


_DRUIDM_SIDECAR = (
    Path(__file__).resolve().parents[1]
    / "pythongui_exports" / "druidm.materials.json"
)


@pytest.mark.skipif(
    not _DRUIDM_SIDECAR.is_file(), reason="druidm sidecar not present",
)
def test_is_hair_material_catches_real_hero_hair_2uv_beard():
    """Reproduces the user's druidm export: the ``hero_hair_2uv`` full
    beard (``druM_B07_mat``) now classifies as hair alongside the
    plain-``hero_hair`` stubble + head hair and the ``hair_pbr_igc``
    eyelashes. The old exact-match predicate dropped the beard.
    """
    import json

    data = json.loads(_DRUIDM_SIDECAR.read_text(encoding="utf-8"))
    hits: dict[str, str] = {}
    for m in data["materials"]:
        # Synthetic Material — is_hair_material only reads shader_map.
        mat = _material(name=m["name"], shader=m["shader_map"])
        if is_hair_material(mat):
            hits[m["name"]] = m["shader_map"]

    assert "druM_B07_mat" in hits
    assert hits["druM_B07_mat"] == "hero_hair_2uv"
    # Regression: the previously-working targets are still caught.
    assert "DruM_H05_mat" in hits                       # hero_hair  head hair
    assert "Global_Male_Facialhair_01_Stubble" in hits  # hero_hair  stubble
    assert "Global_Eyelashes_F00_mat" in hits           # hair_pbr_igc lashes


@pytest.mark.skipif(not D4DATA.is_dir(), reason="d4data not available")
def test_discover_hair_color_is_parametric():
    variants = discover_variants("barM_P00", D4DATA)
    hair = variants["hair_color"]
    assert hair.kind == KIND_PARAMETRIC_COLOR
    assert len(hair.entries) == 31
    assert all(isinstance(e, HairColor) for e in hair.entries)

    # Every applies_to index points at a hair-shader material.
    mats = load_materials("barM_P00", D4DATA)
    assert hair.applies_to_materials
    for idx in hair.applies_to_materials:
        assert 0 <= idx < len(mats)
        assert is_hair_material(mats[idx])


def test_parametric_block_serialization():
    """A parametric_hsv category serialises to the documented shape."""
    tones = [
        SkinTone("S00", "Tone One", 0, 0.1, -0.2, 0.3, 0.5,
                 (0.2, 0.3, 0.4, 1.0)),
    ]
    variants = {
        "skin": VariantCategory(KIND_PARAMETRIC_HSV, list(tones), [0, 2]),
        "makeup": VariantCategory(KIND_IMAGE_SWAP, []),
    }
    block = variants_to_sidecar_block(variants)

    skin = block["skin"]
    assert skin["kind"] == "parametric_hsv"
    assert skin["applies_to_materials"] == [0, 2]
    entry = skin["entries"][0]
    assert entry["id"] == "S00"
    assert entry["display_name"] == "Tone One"
    assert entry["hue"] == 0.1
    assert entry["saturation"] == -0.2
    assert entry["darken"] == 0.5
    assert len(entry["ui_color"]) == 4

    # image_swap categories stay typed too, even when empty.
    assert block["makeup"]["kind"] == "image_swap"
    assert block["makeup"]["entries"] == []
    # parametric-only key must not leak onto image_swap blocks.
    assert "applies_to_materials" not in block["makeup"]


@pytest.mark.skipif(not D4DATA.is_dir(), reason="d4data not available")
def test_discover_skin_is_parametric():
    variants = discover_variants("barM_P00", D4DATA)
    skin = variants["skin"]
    assert skin.kind == KIND_PARAMETRIC_HSV
    assert len(skin.entries) >= 1
    assert all(isinstance(e, SkinTone) for e in skin.entries)

    # Every applies_to index points at a skin-shader material.
    mats = load_materials("barM_P00", D4DATA)
    assert skin.applies_to_materials
    for idx in skin.applies_to_materials:
        assert 0 <= idx < len(mats)
        assert "skin" in (mats[idx].shader_map or "").lower()


@pytest.mark.skipif(not D4DATA.is_dir(), reason="d4data not available")
def test_other_categories_are_image_swap():
    variants = discover_variants("barM_P00", D4DATA)
    for kind in ("eyes", "makeup", "material", "markings"):
        assert variants[kind].kind == KIND_IMAGE_SWAP


@pytest.mark.skipif(not D4DATA.is_dir(), reason="d4data not available")
def test_full_sidecar_block_roundtrips():
    variants = discover_variants("barM_P00", D4DATA)
    block = variants_to_sidecar_block(variants)
    assert set(block) == {
        "skin", "eyes", "makeup", "material", "hair_color", "markings",
    }
    for cat in block.values():
        assert cat["kind"] in (
            "image_swap", "parametric_hsv", "parametric_color",
        )
        assert isinstance(cat["entries"], list)
