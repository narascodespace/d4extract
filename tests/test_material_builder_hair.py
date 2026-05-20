"""Tests for material_builder's bpy-free hair helpers.

``material_builder.py`` imports ``bpy`` at module load (it has bpy-using
helpers further down), so it cannot be reached through the normal
``blender_addon`` package — that triggers operators / panels that pull
the real ``bpy``. Instead, ``bpy`` is stubbed in ``sys.modules`` and
``material_builder`` is loaded under a faux package alongside
``sidecar`` (the same standalone-load technique
``test_armor_skin_swap.py`` / ``test_hair_color_sidecar.py`` use).

Only pure-data helpers like :func:`_hair_base_color_is_dark` are
exercised here; node-graph helpers (which actually call ``bpy.types``)
are verified by the headless Blender harness.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_ADDON_CORE = (
    Path(__file__).resolve().parents[1] / "blender_addon" / "core"
)
_PKG = "d4_addon_mb_under_test"

# Stub bpy (and the submodules material_builder touches at import time)
# before any addon module loads. The helpers under test never call bpy
# at runtime, so a bare ``types.ModuleType`` placeholder is enough.
for _name in ("bpy", "bpy.types", "bpy.utils", "bpy.utils.previews", "bpy.props"):
    sys.modules.setdefault(_name, types.ModuleType(_name))


def _load(submodule: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        f"{_PKG}.{submodule}", _ADDON_CORE / filename,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.{submodule}"] = mod
    spec.loader.exec_module(mod)
    return mod


# Faux parent package so ``from .sidecar import ...`` resolves inside
# material_builder when it is loaded by file path.
_pkg = types.ModuleType(_PKG)
_pkg.__path__ = [str(_ADDON_CORE)]
sys.modules[_PKG] = _pkg

sc = _load("sidecar", "sidecar.py")
mb = _load("material_builder", "material_builder.py")


def _tex(role: str, avg: tuple[float, float, float, float]) -> "sc.SidecarTexture":
    return sc.SidecarTexture(
        role=role, slot=0, sno_id=0, path=f"{role}.tex",
        width=0, height=0, format=0, avg_rgba=avg,
    )


def _mat(textures: list) -> "sc.SidecarMaterial":
    return sc.SidecarMaterial(
        name="m", sno_id=0, shader_map="hero_hair",
        base_color_factor=(1.0, 1.0, 1.0, 1.0),
        emissive_factor=(0.0, 0.0, 0.0),
        metallic_factor=1.0, roughness_factor=1.0, is_cloth_only=False,
        textures=tuple(textures),
    )


# ── _hair_base_color_is_dark ────────────────────────────────────────


def test_dark_helper_near_white_returns_false():
    """Head-hair-style avg_rgba (≈0.97) -> not dark."""
    m = _mat([_tex("BASE_COLOR", (0.97, 0.97, 0.97, 0.50))])
    assert mb._hair_base_color_is_dark(m) is False


def test_dark_helper_near_black_returns_true():
    """Eyelash-style avg_rgba (≈0.02) -> dark."""
    m = _mat([_tex("BASE_COLOR", (0.02, 0.02, 0.02, 0.26))])
    assert mb._hair_base_color_is_dark(m) is True


def test_dark_helper_boundary_returns_false():
    """Exactly at the 0.30 threshold counts as light (not strictly less)."""
    m = _mat([_tex("BASE_COLOR", (0.30, 0.30, 0.30, 1.0))])
    assert mb._hair_base_color_is_dark(m) is False


def test_dark_helper_none_returns_false():
    """No sidecar material at all -> fall through to the legacy link path."""
    assert mb._hair_base_color_is_dark(None) is False


def test_dark_helper_no_base_color_returns_false():
    """A material with no BASE_COLOR texture -> not dark."""
    m = _mat([_tex("NORMAL", (0.5, 0.5, 1.0, 1.0))])
    assert mb._hair_base_color_is_dark(m) is False


def test_dark_helper_uses_max_channel():
    """A texture with one bright channel still classifies as light.

    Detector consults ``max(rgb)``, not the per-channel average —
    a stylised hair texture with one bright primary should fall into
    the Multiply path so the tint inherits its variation.
    """
    m = _mat([_tex("BASE_COLOR", (0.05, 0.45, 0.05, 1.0))])
    assert mb._hair_base_color_is_dark(m) is False


def test_dark_helper_custom_threshold():
    """The threshold knob lets a stricter caller flag mid-tones."""
    m = _mat([_tex("BASE_COLOR", (0.40, 0.40, 0.40, 1.0))])
    assert mb._hair_base_color_is_dark(m) is False
    assert mb._hair_base_color_is_dark(m, threshold=0.50) is True


def test_dark_threshold_constant_matches_default():
    """The module constant matches the documented 0.30 default."""
    assert mb._HAIR_DARK_BASE_COLOR_THRESHOLD == pytest.approx(0.30)


# ── _hair_alpha_is_in_rgb ───────────────────────────────────────────


def test_alpha_in_rgb_facial_hair_uniform_alpha_dark_rgb():
    """Stubble / beard atlas: alpha = 1.0, RGB dark -> mask in RGB."""
    m = _mat([_tex("BASE_COLOR", (0.11, 0.11, 0.11, 1.0))])
    assert mb._hair_alpha_is_in_rgb(m) is True


def test_alpha_in_rgb_full_beard_2uv():
    """The 0.287 / alpha=1.0 druidm full-beard atlas — bug-fix target."""
    m = _mat([_tex("BASE_COLOR", (0.287, 0.287, 0.287, 1.0))])
    assert mb._hair_alpha_is_in_rgb(m) is True


def test_alpha_in_rgb_eyelashes_keep_alpha_channel():
    """Eyelashes are dark RGB but have a real per-pixel alpha — must
    stay on the alpha-channel path or strands would disappear.

    Empirical: ``Global_Eyelashes_F00_mat`` ships avg_rgba.a ≈ 0.256.
    """
    m = _mat([_tex("BASE_COLOR", (0.02, 0.02, 0.02, 0.256))])
    assert mb._hair_alpha_is_in_rgb(m) is False


def test_alpha_in_rgb_head_hair_keep_alpha_channel():
    """Head hair: near-white RGB + real per-pixel alpha -> alpha channel."""
    m = _mat([_tex("BASE_COLOR", (0.97, 0.97, 0.97, 0.50))])
    assert mb._hair_alpha_is_in_rgb(m) is False


def test_alpha_in_rgb_uniform_alpha_but_mid_rgb():
    """Hypothetical uniform-alpha mid-RGB material -> alpha channel.

    Both signals are required; uniform alpha alone is not enough.
    Prevents a stray ``alpha=1.0`` mid-tone material from being
    misclassified as a strand atlas.
    """
    m = _mat([_tex("BASE_COLOR", (0.55, 0.55, 0.55, 1.0))])
    assert mb._hair_alpha_is_in_rgb(m) is False


def test_alpha_in_rgb_none_returns_false():
    """sidecar_mat=None -> alpha-channel path (pre-fix default)."""
    assert mb._hair_alpha_is_in_rgb(None) is False


def test_alpha_in_rgb_no_base_color_returns_false():
    """No BASE_COLOR texture at all -> alpha-channel path (default)."""
    m = _mat([_tex("NORMAL", (0.5, 0.5, 1.0, 1.0))])
    assert mb._hair_alpha_is_in_rgb(m) is False


def test_alpha_in_rgb_alpha_just_below_uniform_threshold():
    """A 0.98 alpha avg is below the 0.99 uniform ceiling -> alpha path."""
    m = _mat([_tex("BASE_COLOR", (0.10, 0.10, 0.10, 0.98))])
    assert mb._hair_alpha_is_in_rgb(m) is False


def test_alpha_uniform_threshold_constant():
    """The uniform-alpha ceiling matches the documented 0.99 default."""
    assert mb._HAIR_ALPHA_UNIFORM_THRESHOLD == pytest.approx(0.99)


def test_hair_alpha_from_rgb_node_name_constant():
    """The node-name constant is exposed for harness use."""
    assert mb.HAIR_ALPHA_FROM_RGB_NODE == "d4_hair_alpha_from_rgb"


# ── Spot check on the real spiritbornm sidecar ──────────────────────


# ── _hair_alpha_routing (three-way) ─────────────────────────────────


def test_routing_beard_sidecar_says_rgb_mask():
    """Beard atlas: sidecar uniform-opaque + dark RGB -> ``rgb`` (no boost)."""
    m = _mat([_tex("BASE_COLOR", (0.11, 0.11, 0.11, 1.0))])
    assert mb._hair_alpha_routing(m, image_alpha_uniform=False) == "rgb"
    # Image-alpha state is irrelevant when the sidecar already decided.
    assert mb._hair_alpha_routing(m, image_alpha_uniform=True) == "rgb"


def test_routing_head_hair_real_alpha():
    """Head hair: sidecar alpha is real + image alpha varies -> ``alpha``."""
    m = _mat([_tex("BASE_COLOR", (0.97, 0.97, 0.97, 0.50))])
    assert mb._hair_alpha_routing(m, image_alpha_uniform=False) == "alpha"


def test_routing_eyelash_image_alpha_flat():
    """Eyelash: sidecar said alpha is real but the loaded image's
    alpha is uniformly opaque -> ``rgb_boost`` (the bug-fix case).
    """
    m = _mat([_tex("BASE_COLOR", (0.02, 0.02, 0.02, 0.256))])
    assert mb._hair_alpha_routing(m, image_alpha_uniform=True) == "rgb_boost"


def test_routing_eyelash_image_alpha_varies():
    """Same eyelash sidecar but the loaded image's alpha actually
    varies -> ``alpha`` (no boost needed)."""
    m = _mat([_tex("BASE_COLOR", (0.02, 0.02, 0.02, 0.256))])
    assert mb._hair_alpha_routing(m, image_alpha_uniform=False) == "alpha"


def test_routing_no_sidecar_with_uniform_alpha():
    """sidecar_mat=None + uniform alpha -> ``rgb_boost`` (runtime fallback)."""
    assert mb._hair_alpha_routing(None, image_alpha_uniform=True) == "rgb_boost"


def test_routing_no_sidecar_with_varying_alpha():
    """sidecar_mat=None + varying alpha -> ``alpha`` (conservative)."""
    assert mb._hair_alpha_routing(None, image_alpha_uniform=False) == "alpha"


# ── _image_alpha_is_effectively_uniform ─────────────────────────────


class _FakeImage:
    """Minimal duck-typed bpy.types.Image stand-in for pixel sampling."""

    def __init__(self, width: int, height: int, channels: int, alphas):
        """``alphas`` is a list of per-pixel alpha values (length w*h)."""
        self.size = (width, height)
        self.channels = channels
        # Lay out as flat [R,G,B,A, R,G,B,A, ...] like bpy.types.Image.pixels.
        # RGB content is irrelevant to the uniformity check; pad with 0.
        n = width * height
        self.pixels = [0.0] * (n * channels)
        if channels >= 4 and len(alphas) == n:
            for i, a in enumerate(alphas):
                self.pixels[i * channels + 3] = a


def test_uniform_helper_none_returns_false():
    """``img=None`` -> caller cannot decide; default to ``False``."""
    assert mb._image_alpha_is_effectively_uniform(None) is False


def test_uniform_helper_3_channels_returns_true():
    """Image with fewer than 4 channels -> alpha is missing entirely."""
    img = _FakeImage(2, 2, 3, alphas=[])
    assert mb._image_alpha_is_effectively_uniform(img) is True


def test_uniform_helper_all_opaque_returns_true():
    """4-channel image with every alpha == 1.0 -> uniform."""
    img = _FakeImage(4, 4, 4, alphas=[1.0] * 16)
    assert mb._image_alpha_is_effectively_uniform(img) is True


def test_uniform_helper_one_transparent_returns_false():
    """A single transparent pixel is enough to break uniformity."""
    alphas = [1.0] * 16
    alphas[7] = 0.0
    img = _FakeImage(4, 4, 4, alphas=alphas)
    assert mb._image_alpha_is_effectively_uniform(img) is False


def test_uniform_helper_below_ceiling_returns_false():
    """Alpha 0.98 (below the 0.99 ceiling) counts as non-uniform."""
    img = _FakeImage(4, 4, 4, alphas=[0.98] * 16)
    assert mb._image_alpha_is_effectively_uniform(img) is False


def test_uniform_helper_zero_size_returns_false():
    """Defensive: a 0×0 image cannot be classified -> ``False``."""
    img = _FakeImage(0, 0, 4, alphas=[])
    assert mb._image_alpha_is_effectively_uniform(img) is False


def test_uniform_helper_strided_sample_finds_transparent():
    """The strided sampler still catches non-uniformity in large images.

    With a 64×64 image and only 16 samples, a non-opaque pixel placed
    at an exact stride boundary must still trip the check.
    """
    n = 64 * 64
    alphas = [1.0] * n
    # 16 samples -> stride 256 -> sample index 0, 256, 512, ...
    alphas[256] = 0.5
    img = _FakeImage(64, 64, 4, alphas=alphas)
    assert mb._image_alpha_is_effectively_uniform(
        img, sample_count=16,
    ) is False


def test_routing_constants_match_string_values():
    """The internal route constants match the documented string values."""
    assert mb._HAIR_ALPHA_ROUTE_ALPHA == "alpha"
    assert mb._HAIR_ALPHA_ROUTE_RGB == "rgb"
    assert mb._HAIR_ALPHA_ROUTE_RGB_BOOST == "rgb_boost"


def test_boost_exponent_constant():
    """The POW boost exponent matches the documented 0.2 default."""
    assert mb._HAIR_ALPHA_BOOST_EXPONENT == pytest.approx(0.2)


def test_hair_alpha_boost_node_name_constant():
    """The boost-node name constant is exposed for harness use."""
    assert mb.HAIR_ALPHA_BOOST_NODE == "d4_hair_alpha_boost"


def test_alpha_in_rgb_against_real_spiritbornm_sidecar():
    """Real exporter data: classify each hair material end to end.

    Drives the parser -> SidecarMaterial -> ``_hair_alpha_is_in_rgb``
    chain on the on-disk spiritbornm sidecar so a regression in the
    parser's ``avg_rgba`` plumbing trips this test.
    """
    repo_side = (
        Path(__file__).resolve().parents[1]
        / "pythongui_exports" / "spiritbornm.materials.json"
    )
    if not repo_side.is_file():
        pytest.skip("spiritbornm sidecar not present")
    side = sc.load_sidecar_file(str(repo_side))
    assert side is not None
    expected = {
        # facial-hair atlases: alpha = 1.0, dark RGB -> RGB path
        "Global_Male_Facialhair_01_Stubble":  True,
        "Global_Male_Facialhair_03_Mustache": True,
        # eyelashes: dark RGB but real per-pixel alpha -> alpha path
        "Global_Eyelashes_F00_mat":           False,
        # head hair: near-white -> alpha path
        "spiM_H07_mat":                       False,
    }
    for name, want in expected.items():
        m = side.material_by_name(name)
        assert m is not None, f"sidecar missing {name!r}"
        got = mb._hair_alpha_is_in_rgb(m)
        assert got is want, (
            f"{name}: avg_rgba="
            f"{m.texture_for_role('BASE_COLOR').avg_rgba} -> {got}, "
            f"expected {want}"
        )


# ── Real-world spot check via the production sidecar parser ─────────


def test_dark_helper_against_real_spiritbornm_sidecar(tmp_path):
    """Spot-check on the on-disk spiritbornm sidecar: parser populates
    ``avg_rgba`` and the helper classifies each hair material correctly.

    Read-only — verifies the sidecar -> SidecarMaterial parse chain
    actually feeds the dark detector with real exporter data.
    """
    repo_side = (
        Path(__file__).resolve().parents[1]
        / "pythongui_exports" / "spiritbornm.materials.json"
    )
    if not repo_side.is_file():
        pytest.skip("spiritbornm sidecar not present")
    side = sc.load_sidecar_file(str(repo_side))
    assert side is not None
    expected_dark = {
        "Global_Eyelashes_F00_mat": True,
        "Global_Male_Facialhair_01_Stubble": True,
        "Global_Male_Facialhair_03_Mustache": True,
        "spiM_H07_mat": False,
    }
    for name, want_dark in expected_dark.items():
        m = side.material_by_name(name)
        assert m is not None, f"sidecar missing {name!r}"
        got = mb._hair_base_color_is_dark(m)
        assert got is want_dark, (
            f"{name}: avg_rgba="
            f"{m.texture_for_role('BASE_COLOR').avg_rgba} -> {got}, "
            f"expected {want_dark}"
        )
