"""Tests for ExportWorker's variant discovery + texture pre-extraction.

The worker is built via ``__new__`` so ``QThread.__init__`` (and a
QApplication / event loop) is never needed — only plain attributes the
helpers read are set. Same pattern as ``test_export_worker.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from d4extract.formats.material_parser import TextureRef
from d4extract.formats.skin_tones import SkinTone
from d4extract.formats.variants import (
    KIND_IMAGE_SWAP,
    KIND_PARAMETRIC_HSV,
    VariantCategory,
    VariantEntry,
)
from d4extract.gui.workers.export_worker import ExportWorker

# Tests run with cwd = d4extract/. d4data is the sibling checkout.
D4DATA = Path("../d4data/json")


def _bare_worker() -> ExportWorker:
    """An ExportWorker with no Qt init — just the attrs the helpers read."""
    worker = ExportWorker.__new__(ExportWorker)
    worker._mesh = None
    worker._skinned_pieces = None
    worker._extra_meshes = []
    worker._variants = None
    worker._d4data_path = None
    return worker


def _makeup_tref(path: str) -> TextureRef:
    return TextureRef(
        slot=-1, role="MAKEUP", sno_id=1, path=path,
        width=0, height=0, format=0, avg_rgba=(1.0, 1.0, 1.0, 1.0),
    )


def test_wanted_texture_paths_empty_when_nothing_referenced():
    worker = _bare_worker()
    assert worker._wanted_texture_paths() == []


def test_wanted_texture_paths_includes_variant_textures():
    """Variant (makeup/markings) textures join the pre-extraction set."""
    worker = _bare_worker()
    entry = VariantEntry(
        kind="makeup", id="m0", display_name="M0",
        applies_to_material_idx=0, target_role="MAKEUP",
        texture=_makeup_tref("base/meta/Texture/makeup0.tex"),
    )
    worker._variants = {
        "makeup": VariantCategory(kind=KIND_IMAGE_SWAP, entries=[entry]),
    }
    paths = worker._wanted_texture_paths()
    # Path is translated from the meta/ tree to the payload/ tree.
    assert "base/payload/Texture/makeup0.tex" in paths


def test_wanted_texture_paths_skips_parametric_skin():
    """Parametric (skin HSV) categories carry no textures — no crash."""
    worker = _bare_worker()
    tone = SkinTone("S00", "Tone", 0, 0.0, 0.0, 0.0, 1.0, (1.0, 1.0, 1.0, 1.0))
    worker._variants = {
        "skin": VariantCategory(
            kind=KIND_PARAMETRIC_HSV, entries=[tone],
            applies_to_materials=[0, 2],
        ),
    }
    assert worker._wanted_texture_paths() == []


def test_discover_variants_noop_without_d4data():
    """No d4data path → discovery is a silent no-op (variants stay None)."""
    worker = _bare_worker()
    worker._d4data_path = None
    worker._discover_variants()
    assert worker._variants is None


def test_discover_variants_noop_without_primary_materials():
    """A primary mesh with no materials → no-op (cannot discover)."""
    worker = _bare_worker()
    worker._d4data_path = D4DATA
    worker._mesh = SimpleNamespace(name="x", materials=None)
    worker._discover_variants()
    assert worker._variants is None


@pytest.mark.skipif(not D4DATA.is_dir(), reason="d4data not available")
def test_discover_variants_populates_skin_block():
    """With d4data + resolved materials, the skin block is discovered."""
    from d4extract.formats.material_parser import load_materials

    mats = load_materials("spiM_P00", D4DATA)
    worker = _bare_worker()
    worker._d4data_path = D4DATA
    worker._mesh = SimpleNamespace(name="spiM_P00", materials=mats)
    worker._discover_variants()

    assert worker._variants is not None
    skin = worker._variants["skin"]
    assert skin.kind == KIND_PARAMETRIC_HSV
    assert len(skin.entries) > 0
    assert skin.applies_to_materials  # at least one skin material


# ─── Assembly variant discovery — the multi-piece scoping fix ────────


@pytest.mark.skipif(not D4DATA.is_dir(), reason="d4data not available")
def test_discover_variants_assembly_includes_non_primary_hair():
    """Hair materials on non-primary assembly pieces reach the sidecar.

    The Character Builder bug: ``_discover_variants`` used to run
    against only the first skinned piece, so an equipped hair piece's
    ``hero_hair`` material never made it into
    ``variants.hair_color.applies_to_materials`` — the dropdown
    populated but Apply Variants found nothing to tint.

    Mock: a real Character Builder export (a CASC ``.app`` parse)
    cannot be constructed in a unit test, so ``_skinned_pieces`` is a
    hand-built list of ``SimpleNamespace`` meshes carrying real
    ``load_materials()`` output for a spiritborn body + hair piece +
    facial-hair piece — the same roster the on-disk ``spiritbornm``
    Character Builder export holds.
    """
    from d4extract.formats.material_parser import load_materials
    from d4extract.formats.variants import is_hair_material

    body = load_materials("spiM_P00", D4DATA)   # eyelashes + stubble
    hair = load_materials("spiM_H07", D4DATA)   # spiM_H07_mat (hero_hair)
    face = load_materials("spiM_B03", D4DATA)   # mustache  (hero_hair)
    combined = body + hair + face

    worker = _bare_worker()
    worker._d4data_path = D4DATA
    worker._mesh = None
    worker._skinned_pieces = [
        (SimpleNamespace(name="spiM_P00", materials=body), None),
        (SimpleNamespace(name="spiM_H07", materials=hair), None),
        (SimpleNamespace(name="spiM_B03", materials=face), None),
    ]
    worker._discover_variants()

    assert worker._variants is not None
    hc = worker._variants["hair_color"]
    expected = [i for i, m in enumerate(combined) if is_hair_material(m)]
    # All four hair targets — eyelashes + stubble (body piece) AND the
    # hair piece + facial-hair piece (non-primary) — are present.
    assert hc.applies_to_materials == expected
    assert len(hc.applies_to_materials) == 4
    # At least one index lies beyond the first piece's slice — exactly
    # the target the old primary-only discovery dropped.
    assert any(i >= len(body) for i in hc.applies_to_materials)


@pytest.mark.skipif(not D4DATA.is_dir(), reason="d4data not available")
def test_discover_variants_assembly_beats_primary_only():
    """Combined-list discovery strictly out-targets primary-only.

    A direct A/B of the old vs new discovery input — the regression
    guard for the multi-piece scoping bug.
    """
    from d4extract.formats.material_parser import load_materials
    from d4extract.formats.variants import discover_variants

    body = load_materials("spiM_P00", D4DATA)
    hair = load_materials("spiM_H07", D4DATA)

    primary_only = discover_variants("spiM_P00", D4DATA, body)
    combined = discover_variants("spiM_P00", D4DATA, body + hair)

    assert (
        len(combined["hair_color"].applies_to_materials)
        > len(primary_only["hair_color"].applies_to_materials)
    )


@pytest.mark.skipif(not D4DATA.is_dir(), reason="d4data not available")
def test_discover_variants_single_mesh_unchanged():
    """Regression: the single-mesh path discovers exactly as before."""
    from d4extract.formats.material_parser import load_materials
    from d4extract.formats.variants import discover_variants

    mats = load_materials("spiM_P00", D4DATA)
    worker = _bare_worker()
    worker._d4data_path = D4DATA
    worker._mesh = SimpleNamespace(name="spiM_P00", materials=mats)
    worker._skinned_pieces = None
    worker._discover_variants()

    assert worker._variants is not None
    direct = discover_variants("spiM_P00", D4DATA, mats)
    assert (
        worker._variants["hair_color"].applies_to_materials
        == direct["hair_color"].applies_to_materials
    )
