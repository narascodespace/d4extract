"""End-to-end skeleton parsing + skinned glTF export tests.

These exercise the real .app samples in ``samples/base`` rather than
synthetic fixtures: the structural claims in
``docs/skeleton_format_spec.md`` are only meaningful against the
on-disk binaries.  The tests are skipped automatically when the
samples aren't checked out (e.g. on CI without the sample set).
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from pygltflib import GLTF2

from d4extract.export.gltf_export import GltfExporter, _compute_bone_pruning
from d4extract.formats.app_parser import parse_app, parse_skeleton

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _sample_pair(name: str) -> tuple[Path, Path] | None:
    meta = SAMPLES / "base" / "meta" / "Appearance" / f"{name}.app"
    pay = SAMPLES / "base" / "payload" / "Appearance" / f"{name}.app"
    return (meta, pay) if meta.exists() and pay.exists() else None


def _glb_validate(g: GLTF2) -> None:
    """Re-check structural invariants of a loaded .glb."""
    blob = g.binary_blob() or b""
    type_size = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
    comp_size = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
    for i, acc in enumerate(g.accessors):
        bv = g.bufferViews[acc.bufferView]
        end = (acc.byteOffset or 0) + acc.count * type_size[acc.type] * comp_size[acc.componentType]
        assert end <= bv.byteLength, f"accessor[{i}] overruns bufferView"
    for bv in g.bufferViews:
        assert bv.byteOffset + bv.byteLength <= len(blob)
    for sk in g.skins or []:
        for j in sk.joints:
            assert 0 <= j < len(g.nodes)
        if sk.inverseBindMatrices is not None:
            assert g.accessors[sk.inverseBindMatrices].count == len(sk.joints)
    seen_children: set[int] = set()
    for n in g.nodes:
        for c in n.children or []:
            assert c not in seen_children, "node has two parents"
            seen_children.add(c)


# ─── parse_skeleton ──────────────────────────────────────────────────


def test_parse_skeleton_static_returns_none() -> None:
    pair = _sample_pair("Goatman_BossTrophy")
    if pair is None:
        pytest.skip("Goatman sample not available")
    meta, pay = pair
    skel = parse_skeleton(meta.read_bytes(), pay.read_bytes())
    assert skel is None


def test_parse_skeleton_barF_H07_full_decode() -> None:
    pair = _sample_pair("barF_H07")
    if pair is None:
        pytest.skip("barF_H07 sample not available")
    meta, pay = pair
    skel = parse_skeleton(meta.read_bytes(), pay.read_bytes())
    assert skel is not None
    assert len(skel.bones) == 190
    assert skel.base_bone_count == 190
    assert skel.cloth_bone_count == 0
    assert skel.template_id == 0x0B38314E

    # Hierarchy invariants: every parent index lies before the child.
    roots = [b for b in skel.bones if b.parent_index == -1]
    assert len(roots) == 1
    for b in skel.bones:
        if b.parent_index != -1:
            assert 0 <= b.parent_index < b.index, (
                f"bone[{b.index}].parent_index={b.parent_index} not strictly earlier"
            )

    # Quaternion magnitudes ≈ 1.
    for b in skel.bones:
        for q in (b.local_trs.q, b.inv_bind_trs.q):
            mag2 = sum(x * x for x in q)
            assert 0.5 < mag2 < 1.5, f"non-unit quat in bone {b.index}: |q|²={mag2}"


def test_parse_skeleton_necF_cloth_split() -> None:
    pair = _sample_pair("necF_stor229_HLM")
    if pair is None:
        pytest.skip("necF_stor229_HLM sample not available")
    meta, pay = pair
    skel = parse_skeleton(meta.read_bytes(), pay.read_bytes())
    assert skel is not None
    assert len(skel.bones) == 298
    assert skel.base_bone_count == 192
    assert skel.cloth_bone_count == 106
    assert skel.template_id == 0xC71F3E7C


# ─── parse_app — bone palette propagation ────────────────────────────


def test_segment_pBoneIDs_propagates_to_submesh() -> None:
    pair = _sample_pair("barF_H07")
    if pair is None:
        pytest.skip("barF_H07 sample not available")
    m = parse_app(*pair)
    assert m.skeleton is not None
    assert len(m.submeshes) >= 1
    sm = m.submeshes[0]
    # First few bones in the LOD0 palette per d4data ground truth.
    assert sm.bone_palette[:5] == (103, 52, 8, 74, 99)
    assert all(g < len(m.skeleton.bones) for g in sm.bone_palette)


def test_necF_helmet_palette_is_single_bone() -> None:
    pair = _sample_pair("necF_stor229_HLM")
    if pair is None:
        pytest.skip("necF_stor229_HLM sample not available")
    m = parse_app(*pair)
    assert m.skeleton is not None
    # Every helmet segment is rigid-bound to bone 62.
    for sm in m.submeshes:
        assert sm.bone_palette == (62,)


# ─── glTF export — full skeleton ─────────────────────────────────────


def test_export_full_skeleton_barF(tmp_path: Path) -> None:
    pair = _sample_pair("barF_H07")
    if pair is None:
        pytest.skip("barF_H07 sample not available")
    m = parse_app(*pair)
    out = GltfExporter(coordinate_transform="z_up_to_y_up").export(
        m, tmp_path / "barF.glb",
    )
    g = GLTF2().load_binary(str(out))
    _glb_validate(g)
    assert g.skins is not None and len(g.skins) == 1
    assert len(g.skins[0].joints) == 190
    # mesh node + 190 bone nodes
    assert len(g.nodes) == 191
    # JOINTS_0 / WEIGHTS_0 present and matched in length
    prim = g.meshes[0].primitives[0]
    assert prim.attributes.JOINTS_0 is not None
    assert prim.attributes.WEIGHTS_0 is not None
    pos_acc = g.accessors[prim.attributes.POSITION]
    j_acc = g.accessors[prim.attributes.JOINTS_0]
    w_acc = g.accessors[prim.attributes.WEIGHTS_0]
    assert pos_acc.count == j_acc.count == w_acc.count == 13240
    # IBM count matches joint count
    ibm_acc = g.accessors[g.skins[0].inverseBindMatrices]
    assert ibm_acc.count == 190 and ibm_acc.type == "MAT4"


def test_export_full_skeleton_necF(tmp_path: Path) -> None:
    pair = _sample_pair("necF_stor229_HLM")
    if pair is None:
        pytest.skip("necF_stor229_HLM sample not available")
    m = parse_app(*pair)
    out = GltfExporter(coordinate_transform="z_up_to_y_up").export(
        m, tmp_path / "necF.glb",
    )
    g = GLTF2().load_binary(str(out))
    _glb_validate(g)
    assert len(g.skins[0].joints) == 298
    assert g.accessors[g.skins[0].inverseBindMatrices].count == 298


# ─── glTF export — pruning ───────────────────────────────────────────


def test_prune_bones_barF(tmp_path: Path) -> None:
    pair = _sample_pair("barF_H07")
    if pair is None:
        pytest.skip("barF_H07 sample not available")
    m = parse_app(*pair)
    keep_mask, remap = _compute_bone_pruning(m.skeleton, m.submeshes)
    weighted: set[int] = set()
    for sm in m.submeshes:
        weighted |= set(sm.bone_palette)
    active = sum(keep_mask)
    # Every weighted bone is active, plus its ancestor chain.
    assert weighted.issubset({i for i, k in enumerate(keep_mask) if k})
    assert active == len(remap)
    assert active >= len(weighted)

    out = GltfExporter(
        coordinate_transform="z_up_to_y_up", prune_bones=True,
    ).export(m, tmp_path / "barF_prune.glb")
    g = GLTF2().load_binary(str(out))
    _glb_validate(g)
    assert len(g.skins[0].joints) == active

    # Every JOINTS_0 byte must land within the pruned palette.
    blob = g.binary_blob()
    j_acc = g.accessors[g.meshes[0].primitives[0].attributes.JOINTS_0]
    bv = g.bufferViews[j_acc.bufferView]
    comp_size = 1 if j_acc.componentType == 5121 else 2
    fmt = "<4B" if comp_size == 1 else "<4H"
    for v in range(j_acc.count):
        for jv in struct.unpack_from(fmt, blob, bv.byteOffset + v * 4 * comp_size):
            assert jv < len(g.skins[0].joints)


def test_prune_bones_necF_helmet(tmp_path: Path) -> None:
    pair = _sample_pair("necF_stor229_HLM")
    if pair is None:
        pytest.skip("necF_stor229_HLM sample not available")
    m = parse_app(*pair)
    out = GltfExporter(
        coordinate_transform="z_up_to_y_up", prune_bones=True,
    ).export(m, tmp_path / "necF_prune.glb")
    g = GLTF2().load_binary(str(out))
    _glb_validate(g)
    # Helmet skinned to bone 62; pruned skeleton walks parent chain to root.
    n_joints = len(g.skins[0].joints)
    assert 1 <= n_joints <= 16, f"helmet pruned skeleton unexpectedly large: {n_joints}"


# ─── glTF export — static models stay static ─────────────────────────


def test_static_model_no_skin(tmp_path: Path) -> None:
    pair = _sample_pair("Goatman_BossTrophy")
    if pair is None:
        pytest.skip("Goatman sample not available")
    m = parse_app(*pair)
    assert m.skeleton is None
    out = GltfExporter(coordinate_transform="z_up_to_y_up").export(
        m, tmp_path / "Goatman.glb",
    )
    g = GLTF2().load_binary(str(out))
    _glb_validate(g)
    assert not g.skins, "static model should not emit a Skin"
    assert len(g.nodes) == 1


# ─── --no-skin opt-out keeps mesh static even when skeleton present ──


def test_azmodan_primary_path_matches_d4data(tmp_path: Path) -> None:
    """Azmodan must take the primary ``_build_lod0_mesh`` path and
    produce d4data-matching submeshes.

    This is a regression test for the false-positive index buffer
    detection that previously shifted the LOD0 IB's array_index out
    of alignment with the SubObjects' ``ibi`` field.
    """
    pair = _sample_pair("Azmodan")
    if pair is None:
        pytest.skip("Azmodan sample not available")
    m = parse_app(*pair)

    # Aggregate counts (from d4data JSON ground truth)
    assert m.vertex_count == 49_912
    assert m.index_count == 249_936
    assert len(m.submeshes) == 9
    assert m.skeleton is not None
    assert len(m.skeleton.bones) == 244
    assert m.skeleton.base_bone_count == 95
    assert m.skeleton.cloth_bone_count == 149
    assert m.skeleton.template_id == 0xB34AD8A3

    # Per-submesh material indices, segment counts, and palette sizes
    # (from d4data/json/base/meta/Appearance/Azmodan.app.json LOD[0])
    expected_mats = list(range(9))
    expected_vc = [32, 127, 30, 58, 786, 6602, 25378, 11778, 5121]
    expected_ic = [48, 480, 96, 228, 3198, 35244, 129294, 57942, 23406]
    expected_pal_sizes = [6, 7, 8, 6, 95, 30, 60, 88, 11]
    # First 5 palette entries per d4data
    expected_pal_head = [
        (81, 80, 69, 68, 33),
        (87, 1, 67, 65, 75),
        (1, 87, 83, 92, 66),
        (83, 1, 66, 2, 71),
        (193, 173, 192, 177, 176),
        (90, 89, 88, 87, 1),
        (98, 32, 95, 30, 93),
        (243, 1, 241, 240, 239),
        (93, 1, 2, 66, 32),
    ]

    for i, sm in enumerate(m.submeshes):
        assert sm.material_index == expected_mats[i]
        assert sm.vertex_count == expected_vc[i]
        assert sm.index_count == expected_ic[i]
        assert len(sm.bone_palette) == expected_pal_sizes[i]
        assert sm.bone_palette[:5] == expected_pal_head[i]
        # Every palette entry must be a valid global bone index.
        assert all(g < len(m.skeleton.bones) for g in sm.bone_palette)


def test_azmodan_skin_export_validates(tmp_path: Path) -> None:
    pair = _sample_pair("Azmodan")
    if pair is None:
        pytest.skip("Azmodan sample not available")
    m = parse_app(*pair)
    out = GltfExporter(coordinate_transform="z_up_to_y_up").export(
        m, tmp_path / "Azmodan.glb",
    )
    g = GLTF2().load_binary(str(out))
    _glb_validate(g)
    # 244 bones + 1 mesh node
    assert len(g.nodes) == 245
    assert len(g.skins[0].joints) == 244
    assert g.accessors[g.skins[0].inverseBindMatrices].count == 244
    assert len(g.meshes[0].primitives) == 9

    # Spot check that JOINTS_0 in primitive 7 (palette[1] = 1) lands on
    # the right global index after remap.
    blob = g.binary_blob()
    prim = g.meshes[0].primitives[7]
    j_acc = g.accessors[prim.attributes.JOINTS_0]
    bv = g.bufferViews[j_acc.bufferView]
    # Must be within bone count.
    comp_size = 1 if j_acc.componentType == 5121 else 2
    fmt = "<4B" if comp_size == 1 else "<4H"
    max_seen = 0
    for v in range(j_acc.count):
        for jv in struct.unpack_from(fmt, blob, bv.byteOffset + v * 4 * comp_size):
            max_seen = max(max_seen, jv)
    # Per d4data: prim 7's palette starts at 243 (mat 7 cloth bones).
    # Max remapped joint should be exactly 243 (cloth bone idx).
    assert max_seen == 243


def test_no_skin_opt_out(tmp_path: Path) -> None:
    pair = _sample_pair("barF_H07")
    if pair is None:
        pytest.skip("barF_H07 sample not available")
    m = parse_app(*pair)
    out = GltfExporter(
        coordinate_transform="z_up_to_y_up", export_skin=False,
    ).export(m, tmp_path / "barF_noskin.glb")
    g = GLTF2().load_binary(str(out))
    _glb_validate(g)
    assert not g.skins
    assert len(g.nodes) == 1
    # No JOINTS_0 / WEIGHTS_0 emitted on primitives.
    for prim in g.meshes[0].primitives:
        assert prim.attributes.JOINTS_0 is None
        assert prim.attributes.WEIGHTS_0 is None
