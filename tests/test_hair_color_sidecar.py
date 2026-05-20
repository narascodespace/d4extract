"""Tests for the addon sidecar parser's ``parametric_color`` (hair) support.

``blender_addon/core/sidecar.py`` is deliberately ``bpy``-free; it is
loaded straight from its file path (the same technique as
``test_armor_skin_swap.py``) so the addon package's ``bpy``-importing
``__init__`` is never triggered.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SIDECAR_PY = (
    Path(__file__).resolve().parents[1]
    / "blender_addon" / "core" / "sidecar.py"
)

D4DATA = Path("../d4data/json")


def _load_sidecar_module():
    """Import ``blender_addon/core/sidecar.py`` standalone (no bpy)."""
    spec = importlib.util.spec_from_file_location(
        "d4_addon_sidecar_hair_under_test", _SIDECAR_PY,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sc = _load_sidecar_module()


def _hair_block(n: int = 2) -> dict:
    """A ``parametric_color`` variant block with ``n`` entries."""
    return {
        "kind": "parametric_color",
        "entries": [
            {
                "id": f"H{i:02d}",
                "display_name": f"Colour{i}",
                "sort_order": i - 1,
                "ui_color": [0.1, 0.2, 0.3, 1.0],
                "secondary_color": [0.4, 0.5, 0.6, 1.0],
                "tertiary_color": [0.7, 0.8, 0.9, 1.0],
                "colors2": [[0.0, 0.0, 0.0, 1.0]] * 3,
                "influence": 0.5 if i == 2 else 1.0,
                "usable_by": [1] * 8,
            }
            for i in range(1, n + 1)
        ],
        "applies_to_materials": [9, 17],
    }


def test_parse_parametric_color_category():
    cat = sc._parse_variant_category("hair_color", _hair_block(3))
    assert cat.kind == "parametric_color"
    assert len(cat.hair_entries) == 3
    assert cat.applies_to_materials == (9, 17)
    # The other kinds' entry tuples stay empty.
    assert cat.image_entries == ()
    assert cat.skin_entries == ()

    e = cat.hair_entries[0]
    assert isinstance(e, sc.SidecarHairColor)
    assert e.id == "H01"
    assert e.display_name == "Colour1"
    assert e.sort_order == 0
    assert e.ui_color == (0.1, 0.2, 0.3, 1.0)
    assert e.secondary_color == (0.4, 0.5, 0.6, 1.0)
    assert e.tertiary_color == (0.7, 0.8, 0.9, 1.0)
    assert len(e.colors2) == 3
    assert all(isinstance(c, tuple) and len(c) == 4 for c in e.colors2)
    assert e.usable_by == (1,) * 8
    # influence is plumbed per-entry.
    assert cat.hair_entries[1].influence == 0.5


def test_parse_hair_color_defensive_defaults():
    """Missing / malformed fields fall back without raising."""
    e = sc._parse_hair_color({"id": "H99"})
    assert e.id == "H99"
    assert e.display_name == "H99"
    assert e.sort_order == 0
    assert e.ui_color == (1.0, 1.0, 1.0, 1.0)
    assert e.secondary_color == (1.0, 1.0, 1.0, 1.0)
    assert e.colors2 == ()
    assert e.influence == 1.0
    assert e.usable_by == ()


def test_unknown_kind_falls_back_to_image_swap():
    """An unrecognised category kind degrades to image_swap, not a crash."""
    cat = sc._parse_variant_category(
        "hair_color", {"kind": "bogus_future_kind", "entries": []},
    )
    assert cat.kind == "image_swap"


def test_legacy_flat_array_still_image_swap():
    """The pre-schema flat-array variant shape keeps parsing as image_swap."""
    cat = sc._parse_variant_category("makeup", [])
    assert cat.kind == "image_swap"


def test_empty_hair_color_block_parses_clean():
    """A model with no hair materials ships an empty image_swap block."""
    cat = sc._parse_variant_category(
        "hair_color", {"kind": "image_swap", "entries": []},
    )
    assert cat.kind == "image_swap"
    assert cat.hair_entries == ()


@pytest.mark.skipif(not D4DATA.is_dir(), reason="d4data not available")
def test_discover_serialize_parse_roundtrip():
    """discover_variants -> sidecar block -> addon parser, end to end."""
    from d4extract.formats.variants import (
        discover_variants,
        variants_to_sidecar_block,
    )

    variants = discover_variants("barM_P00", D4DATA)
    block = variants_to_sidecar_block(variants)
    cat = sc._parse_variant_category("hair_color", block["hair_color"])

    assert cat.kind == "parametric_color"
    assert len(cat.hair_entries) == 31
    ids = [e.id for e in cat.hair_entries]
    assert ids == [f"H{i:02d}" for i in range(1, 32)]
    # H01 DeepBrown's primary tint is opaque black (uint8 zero).
    assert cat.hair_entries[0].ui_color == (0.0, 0.0, 0.0, 1.0)
    assert cat.applies_to_materials
