"""Unit tests for ``combined_materials_for_export``.

The helper is the single source of truth for the order in which the
``.materials.json`` sidecar concatenates per-mesh materials — and the
list variant discovery must index against so each variant block's
``applies_to_materials`` lines up with the sidecar's ``materials[]``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from d4extract.export.gltf_export import combined_materials_for_export


def _mesh(*materials):
    """A minimal MeshData stand-in — the helper only reads ``.materials``."""
    return SimpleNamespace(materials=list(materials))


def test_single_mesh_no_extras():
    primary = _mesh("a", "b", "c")
    out = combined_materials_for_export(primary_mesh=primary)
    assert out == ["a", "b", "c"]
    # A fresh list — never an alias of the mesh's own materials.
    assert out is not primary.materials


def test_single_mesh_with_two_extras():
    primary = _mesh("p0", "p1")
    e1 = _mesh("e0")
    e2 = _mesh("e1", "e2")
    out = combined_materials_for_export(
        primary_mesh=primary, extra_meshes=[(e1, None), (e2, {0})],
    )
    assert out == ["p0", "p1", "e0", "e1", "e2"]


def test_assembly_four_pieces():
    pieces = [
        (_mesh("body0", "body1"), None),
        (_mesh("helm0"), None),
        (_mesh("hair0"), {1, 2}),
        (_mesh("torso0", "torso1"), None),
    ]
    out = combined_materials_for_export(skinned_pieces=pieces)
    assert out == ["body0", "body1", "helm0", "hair0", "torso0", "torso1"]


def test_assembly_two_pieces_plus_static_extra():
    pieces = [(_mesh("p0"), None), (_mesh("p1a", "p1b"), None)]
    extras = [(_mesh("weapon0"), None)]
    out = combined_materials_for_export(
        skinned_pieces=pieces, static_extras=extras,
    )
    assert out == ["p0", "p1a", "p1b", "weapon0"]


def test_mixed_call_raises():
    """Specifying both export shapes at once is a programming error."""
    with pytest.raises(ValueError):
        combined_materials_for_export(
            primary_mesh=_mesh("a"),
            skinned_pieces=[(_mesh("b"), None)],
        )


def test_mixed_call_raises_via_extras_only():
    """A single-mode extra + an assembly-mode static extra is also mixed."""
    with pytest.raises(ValueError):
        combined_materials_for_export(
            extra_meshes=[(_mesh("a"), None)],
            static_extras=[(_mesh("b"), None)],
        )


def test_empty_inputs_return_empty_list():
    assert combined_materials_for_export() == []


def test_falsy_materials_are_skipped():
    """Pieces / extras with ``None`` or empty materials contribute nothing."""
    pieces = [
        (_mesh("p0"), None),
        (SimpleNamespace(materials=None), None),
        (_mesh(), None),                       # empty materials list
        (_mesh("p1"), None),
    ]
    assert combined_materials_for_export(skinned_pieces=pieces) == ["p0", "p1"]


def test_single_mesh_none_materials_returns_empty():
    primary = SimpleNamespace(materials=None)
    assert combined_materials_for_export(primary_mesh=primary) == []
