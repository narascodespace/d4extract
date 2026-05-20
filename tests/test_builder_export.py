"""Tests for ``CharacterBuilderPage.get_export_pieces`` — per-piece export.

``CharacterBuilderPage`` is a ``QWidget``; instantiating it needs a
``QApplication``. ``get_export_pieces`` only touches plain attributes
plus two stubbed collaborators (``slot_panel``, ``assembly_parts_panel``),
so the page is built via ``__new__`` to keep the suite QApplication-free
— the same pattern ``test_export_worker.py`` uses.
"""

from __future__ import annotations

from types import SimpleNamespace

from d4extract.formats.app_parser import (
    Bone,
    BoneTransform,
    MeshData,
    Skeleton,
    Submesh,
)
from d4extract.gui.widgets.character_builder.builder_page import (
    CharacterBuilderPage,
)

_REST = BoneTransform(
    q=(0.0, 0.0, 0.0, 1.0), wp=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0),
)


def _skeleton(template_id: int = 7, n_bones: int = 2) -> Skeleton:
    return Skeleton(
        bones=[
            Bone(index=i, parent_index=i - 1, name_hash=0xA00 + i,
                 flags=0, lod=0, local_trs=_REST, inv_bind_trs=_REST)
            for i in range(n_bones)
        ],
        base_bone_count=n_bones, cloth_bone_count=0,
        template_id=template_id,
    )


def _skinned_piece(name: str, skel: Skeleton) -> MeshData:
    """One-triangle skinned piece bound to ``skel``."""
    return MeshData(
        name=name,
        vertex_count=3, index_count=3, submesh_count=1,
        positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        normals=[(0.0, 0.0, 1.0, 0.0)] * 3,
        tangents=[(1.0, 0.0, 0.0, 1.0)] * 3,
        colors=[(1.0, 1.0, 1.0, 1.0)] * 3,
        uvs=[(0.0, 0.0)] * 3,
        joints=[(0, 0, 0, 0)] * 3,
        weights=[(1.0, 0.0, 0.0, 0.0)] * 3,
        indices=[0, 1, 2],
        submeshes=[Submesh(vertex_count=3, index_count=3,
                           material_index=0, bone_palette=(0, 1))],
        skeleton=skel,
    )


def _static_piece(name: str) -> MeshData:
    """One-triangle unskinned piece (weapon-like — no skeleton)."""
    return MeshData(
        name=name,
        vertex_count=3, index_count=3, submesh_count=1,
        positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        normals=[(0.0, 0.0, 1.0, 0.0)] * 3,
        tangents=[(1.0, 0.0, 0.0, 1.0)] * 3,
        colors=[(1.0, 1.0, 1.0, 1.0)] * 3,
        uvs=[(0.0, 0.0)] * 3,
        indices=[0, 1, 2],
        submeshes=[Submesh(vertex_count=3, index_count=3, material_index=0)],
        skeleton=None,
    )


def _page(pieces: dict[str, MeshData]) -> CharacterBuilderPage:
    """Build a ``CharacterBuilderPage`` via ``__new__`` with stub panels.

    ``pieces`` maps SNO path → ``MeshData``. ``_collect_equipped_paths``
    and ``_sno_for_piece`` run as the real methods against the stubbed
    ``slot_panel`` and ``_mesh_cache``.
    """
    page = CharacterBuilderPage.__new__(CharacterBuilderPage)
    page._mesh_cache = dict(pieces)
    paths = list(pieces)
    page.slot_panel = SimpleNamespace(
        get_customization_build=lambda: {},
        get_build=lambda: {f"slot{i}": p for i, p in enumerate(paths)},
    )
    page.assembly_parts_panel = SimpleNamespace(
        get_visibility_map=lambda: {},
    )
    return page


def test_get_export_pieces_returns_one_per_skinned_piece():
    skel = _skeleton()
    pieces = {
        "p/chest": _skinned_piece("chest", skel),
        "p/helmet": _skinned_piece("helmet", skel),
        "p/gloves": _skinned_piece("gloves", skel),
    }
    result = _page(pieces).get_export_pieces()

    assert result is not None
    skinned_pieces, canonical_skeleton, static_extras = result
    assert len(skinned_pieces) == 3
    for entry in skinned_pieces:
        mesh, filt = entry
        assert isinstance(mesh, MeshData)
        # Empty visibility map → every submesh visible → None filter.
        assert filt is None
    assert static_extras == []
    # The canonical skeleton matches what select_assembly_group picks —
    # every piece here shares the same skeleton object.
    assert canonical_skeleton is skel


def test_get_export_pieces_separates_static_extras():
    skel = _skeleton()
    pieces = {
        "p/chest": _skinned_piece("chest", skel),
        "p/helmet": _skinned_piece("helmet", skel),
        "p/sword": _static_piece("sword"),
    }
    result = _page(pieces).get_export_pieces()

    assert result is not None
    skinned_pieces, canonical_skeleton, static_extras = result
    assert len(skinned_pieces) == 2
    assert len(static_extras) == 1
    assert static_extras[0][0].name == "sword"
    # The canonical skeleton comes from the skinned set, not the weapon.
    assert canonical_skeleton is skel


def test_get_export_pieces_returns_none_when_empty():
    assert _page({}).get_export_pieces() is None


def test_get_export_pieces_weapons_only_has_empty_skinned_list():
    """A weapons-only scene yields an empty skinned list + no skeleton."""
    pieces = {
        "p/sword": _static_piece("sword"),
        "p/shield": _static_piece("shield"),
    }
    result = _page(pieces).get_export_pieces()

    assert result is not None
    skinned_pieces, canonical_skeleton, static_extras = result
    assert skinned_pieces == []
    assert canonical_skeleton is None
    assert len(static_extras) == 2
