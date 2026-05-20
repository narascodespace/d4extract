"""Tests for the glTF 2.0 exporter."""

from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest
from pygltflib import GLTF2

from d4extract.export.gltf_export import (
    COORDINATE_TRANSFORMS,
    GltfExportError,
    GltfExporter,
    _validate_gltf,
)
from d4extract.formats.anim_parser import (
    DecodedAnimation,
    DecodedBoneAnimation,
    build_rest_pose_animation,
)
from d4extract.formats.app_parser import (
    Bone,
    BoneTransform,
    MeshData,
    Skeleton,
    Submesh,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _make_triangle_model(name: str = "triangle") -> MeshData:
    """Minimal model: 3 vertices, 1 triangle."""
    return MeshData(
        name=name,
        vertex_count=3,
        index_count=3,
        submesh_count=0,
        positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        normals=[(0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 1.0, 0.0)],
        colors=[(255, 255, 255, 255)] * 3,
        tangents=[(1.0, 0.0, 0.0, 1.0)] * 3,
        indices=[0, 1, 2],
    )


def _make_cube_model() -> MeshData:
    """Cube model: 8 vertices, 12 triangles (36 indices)."""
    positions = [
        (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
        (-1, -1,  1), (1, -1,  1), (1, 1,  1), (-1, 1,  1),
    ]
    normals = [(0, 0, 1, 0)] * 8
    colors = [(255, 255, 255, 255)] * 8
    tangents = [(1, 0, 0, 1)] * 8
    indices = [
        0, 1, 2, 0, 2, 3,  # front
        4, 6, 5, 4, 7, 6,  # back
        0, 4, 5, 0, 5, 1,  # bottom
        2, 6, 7, 2, 7, 3,  # top
        0, 3, 7, 0, 7, 4,  # left
        1, 5, 6, 1, 6, 2,  # right
    ]
    return MeshData(
        name="cube",
        vertex_count=8,
        index_count=36,
        submesh_count=2,
        positions=positions,
        normals=normals,
        colors=colors,
        tangents=tangents,
        indices=indices,
    )


def _make_large_model(n_verts: int = 70000) -> MeshData:
    """Model with more than 65535 vertices to test uint32 indices."""
    positions = [(float(i), float(i % 100), float(i % 10)) for i in range(n_verts)]
    normals = [(0.0, 0.0, 1.0, 0.0)] * n_verts
    colors = [(255, 255, 255, 255)] * n_verts
    tangents = [(1.0, 0.0, 0.0, 1.0)] * n_verts
    # Build triangle list referencing all vertices
    indices = []
    for i in range(0, n_verts - 2, 3):
        indices.extend([i, i + 1, i + 2])
    return MeshData(
        name="large_model",
        vertex_count=n_verts,
        index_count=len(indices),
        submesh_count=0,
        positions=positions,
        normals=normals,
        colors=colors,
        tangents=tangents,
        indices=indices,
    )


# ── Unit tests: basic export ────────────────────────────────────────


class TestSingleTriangle:
    def test_file_created(self, tmp_path):
        out = tmp_path / "tri.glb"
        GltfExporter().export(_make_triangle_model(), out)
        assert out.exists()
        assert out.stat().st_size > 0

    def test_glb_magic(self, tmp_path):
        out = tmp_path / "tri.glb"
        GltfExporter().export(_make_triangle_model(), out)
        magic = out.read_bytes()[:4]
        assert magic == b"glTF"

    def test_structure(self, tmp_path):
        out = tmp_path / "tri.glb"
        GltfExporter().export(_make_triangle_model(), out)
        g = GLTF2.load(str(out))
        assert len(g.meshes) == 1
        assert len(g.meshes[0].primitives) == 1
        assert g.accessors[0].count == 3  # positions
        # Last accessor is indices
        idx_acc = [a for a in g.accessors if a.type == "SCALAR"][0]
        assert idx_acc.count == 3

    def test_position_min_max(self, tmp_path):
        out = tmp_path / "tri.glb"
        GltfExporter().export(_make_triangle_model(), out)
        g = GLTF2.load(str(out))
        pos_acc = g.accessors[0]
        assert pos_acc.min == [0.0, 0.0, 0.0]
        assert pos_acc.max == [1.0, 1.0, 0.0]

    def test_has_normals(self, tmp_path):
        out = tmp_path / "tri.glb"
        GltfExporter().export(_make_triangle_model(), out)
        g = GLTF2.load(str(out))
        prim = g.meshes[0].primitives[0]
        assert prim.attributes.NORMAL is not None
        norm_acc = g.accessors[prim.attributes.NORMAL]
        assert norm_acc.count == 3
        assert norm_acc.type == "VEC3"

    def test_has_material(self, tmp_path):
        out = tmp_path / "tri.glb"
        GltfExporter().export(_make_triangle_model(), out)
        g = GLTF2.load(str(out))
        assert len(g.materials) == 1
        # Materials are now named per material_index slot ("Material_0",
        # "Material_1", ...).  Phase 5 will resolve real SNO names.
        assert g.materials[0].name == "Material_0"


class TestCubeModel:
    def test_index_count(self, tmp_path):
        out = tmp_path / "cube.glb"
        GltfExporter().export(_make_cube_model(), out)
        g = GLTF2.load(str(out))
        idx_acc = [a for a in g.accessors if a.type == "SCALAR"][0]
        assert idx_acc.count == 36

    def test_vertex_count(self, tmp_path):
        out = tmp_path / "cube.glb"
        GltfExporter().export(_make_cube_model(), out)
        g = GLTF2.load(str(out))
        assert g.accessors[0].count == 8

    def test_position_bounds(self, tmp_path):
        out = tmp_path / "cube.glb"
        GltfExporter().export(_make_cube_model(), out)
        g = GLTF2.load(str(out))
        assert g.accessors[0].min == [-1.0, -1.0, -1.0]
        assert g.accessors[0].max == [1.0, 1.0, 1.0]


# ── Coordinate transforms ───────────────────────────────────────────


class TestCoordinateTransforms:
    def test_none_passthrough(self, tmp_path):
        model = _make_triangle_model()
        out = tmp_path / "none.glb"
        GltfExporter(coordinate_transform="none").export(model, out)
        g = GLTF2.load(str(out))
        # Position data should match input
        assert g.accessors[0].min == [0.0, 0.0, 0.0]
        assert g.accessors[0].max == [1.0, 1.0, 0.0]

    def test_z_up_to_y_up(self, tmp_path):
        # Input: vertex at (0,0,5) -> z_up_to_y_up -> (0, 5, 0)
        model = MeshData(
            name="zup",
            vertex_count=3,
            index_count=3,
            submesh_count=0,
            positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 5.0)],
            normals=[(0, 0, 1, 0)] * 3,
            colors=[(255, 255, 255, 255)] * 3,
            tangents=[(1, 0, 0, 1)] * 3,
            indices=[0, 1, 2],
        )
        out = tmp_path / "zup.glb"
        GltfExporter(coordinate_transform="z_up_to_y_up").export(model, out)
        g = GLTF2.load(str(out))
        # (0,0,5) -> (0, 5, 0) means max Y should be 5
        assert g.accessors[0].max[1] == pytest.approx(5.0)
        # Z should be 0 (was originally Y=0, negated)
        assert g.accessors[0].min[2] == pytest.approx(0.0)

    def test_left_to_right(self, tmp_path):
        model = MeshData(
            name="lr",
            vertex_count=3,
            index_count=3,
            submesh_count=0,
            positions=[(2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (2.5, 1.0, 0.0)],
            normals=[(0, 0, 1, 0)] * 3,
            colors=[(255, 255, 255, 255)] * 3,
            tangents=[(1, 0, 0, 1)] * 3,
            indices=[0, 1, 2],
        )
        out = tmp_path / "lr.glb"
        GltfExporter(coordinate_transform="left_to_right").export(model, out)
        g = GLTF2.load(str(out))
        # X negated: [2,3] -> [-3,-2]
        assert g.accessors[0].min[0] == pytest.approx(-3.0)
        assert g.accessors[0].max[0] == pytest.approx(-2.0)

    def test_auto_detects_z_up(self, tmp_path):
        # Model taller in Z than Y -> should auto-detect z_up_to_y_up
        model = MeshData(
            name="auto_zup",
            vertex_count=3,
            index_count=3,
            submesh_count=0,
            positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 10.0)],
            normals=[(0, 0, 1, 0)] * 3,
            colors=[(255, 255, 255, 255)] * 3,
            tangents=[(1, 0, 0, 1)] * 3,
            indices=[0, 1, 2],
        )
        out = tmp_path / "auto.glb"
        GltfExporter(coordinate_transform="auto").export(model, out)
        g = GLTF2.load(str(out))
        # Z-up detected: (0,0,10) -> (0,10,0)
        assert g.accessors[0].max[1] == pytest.approx(10.0)

    def test_invalid_transform_raises(self):
        with pytest.raises(ValueError, match="Unknown coordinate_transform"):
            GltfExporter(coordinate_transform="banana")


# ── Edge cases ───────────────────────────────────────────────────────


class TestEdgeCases:
    def test_large_model_uses_uint32(self, tmp_path):
        out = tmp_path / "large.glb"
        model = _make_large_model(70000)
        GltfExporter().export(model, out)
        g = GLTF2.load(str(out))
        idx_acc = [a for a in g.accessors if a.type == "SCALAR"][0]
        # componentType 5125 = UNSIGNED_INT
        assert idx_acc.componentType == 5125

    def test_small_model_uses_uint16(self, tmp_path):
        out = tmp_path / "small.glb"
        GltfExporter().export(_make_triangle_model(), out)
        g = GLTF2.load(str(out))
        idx_acc = [a for a in g.accessors if a.type == "SCALAR"][0]
        # componentType 5123 = UNSIGNED_SHORT
        assert idx_acc.componentType == 5123

    def test_no_normals(self, tmp_path):
        out = tmp_path / "no_norms.glb"
        GltfExporter(export_normals=False).export(_make_triangle_model(), out)
        g = GLTF2.load(str(out))
        prim = g.meshes[0].primitives[0]
        assert prim.attributes.NORMAL is None

    def test_single_submesh(self, tmp_path):
        model = _make_triangle_model()
        model.submesh_count = 1
        out = tmp_path / "single_sub.glb"
        GltfExporter().export(model, out)
        g = GLTF2.load(str(out))
        assert len(g.meshes) == 1
        assert len(g.meshes[0].primitives) == 1

    def test_empty_name_gets_default_material(self, tmp_path):
        model = _make_triangle_model(name="")
        out = tmp_path / "noname.glb"
        GltfExporter().export(model, out)
        g = GLTF2.load(str(out))
        # Material should have some name (empty string fallback)
        assert g.materials[0].name is not None


# ── Validation ───────────────────────────────────────────────────────


class TestValidation:
    def test_valid_gltf_passes(self, tmp_path):
        out = tmp_path / "valid.glb"
        exporter = GltfExporter()
        gltf = exporter._build_gltf(_make_triangle_model())
        _validate_gltf(gltf)  # should not raise

    def test_deterministic_output(self, tmp_path):
        model = _make_triangle_model()
        out1 = tmp_path / "a.glb"
        out2 = tmp_path / "b.glb"
        GltfExporter().export(model, out1)
        GltfExporter().export(model, out2)
        assert out1.read_bytes() == out2.read_bytes()


# ── Batch export ─────────────────────────────────────────────────────


class TestBatchExport:
    def test_batch_exports_multiple(self, tmp_path):
        models = [
            (_make_triangle_model("tri1"), tmp_path / "tri1.glb"),
            (_make_cube_model(), tmp_path / "cube.glb"),
        ]
        exporter = GltfExporter()
        progress_calls = []
        results = exporter.export_batch(models, lambda c, t: progress_calls.append((c, t)))
        assert len(results) == 2
        assert all(p.exists() for p in results)
        assert progress_calls == [(1, 2), (2, 2)]


# ── Integration tests with real samples ──────────────────────────────


SAMPLES = Path("samples")
META = SAMPLES / "base" / "meta" / "Appearance"
PAYLOAD = SAMPLES / "base" / "payload" / "Appearance"

SAMPLE_MODELS = [
    "Goatman_BossTrophy",
    "offHandsSorc_stor029",
    "barF_H07",
    "necF_stor229_HLM",
    "Azmodan",
]


def _has_samples() -> bool:
    return META.is_dir() and PAYLOAD.is_dir()


@pytest.mark.skipif(not _has_samples(), reason="Sample files not available")
class TestIntegration:
    @pytest.mark.parametrize("name", SAMPLE_MODELS)
    def test_export_real_model(self, name, tmp_path):
        from d4extract.formats.app_parser import parse_app

        meta_path = META / f"{name}.app"
        payload_path = PAYLOAD / f"{name}.app"
        if not meta_path.exists() or not payload_path.exists():
            pytest.skip(f"{name} sample files not found")

        mesh = parse_app(meta_path, payload_path)
        out = tmp_path / f"{name}.glb"
        GltfExporter().export(mesh, out)

        # Verify file
        assert out.exists()
        assert out.read_bytes()[:4] == b"glTF"

        # Per-submesh primitives: one Primitive per Submesh, each with
        # its own POSITION/indices sliced from the global LOD0 buffers.
        # Verify counts match per-submesh and that the sum of primitive
        # index counts equals the parsed total (overlapping vertex
        # ranges across submeshes, like Azmodan, may legitimately
        # duplicate vertices in the export — that's per-primitive
        # locality, not a bug).
        g = GLTF2.load(str(out))
        prims = g.meshes[0].primitives
        assert len(prims) == len(mesh.submeshes) or not mesh.submeshes
        if mesh.submeshes:
            for prim, sm in zip(prims, mesh.submeshes):
                pos_count = g.accessors[prim.attributes.POSITION].count
                idx_count = g.accessors[prim.indices].count
                assert pos_count == sm.vertex_count
                assert idx_count == sm.index_count
        total_idx = sum(g.accessors[p.indices].count for p in prims)
        assert total_idx == mesh.index_count

        # Position bounds should be set on every primitive's POSITION.
        for p in prims:
            pos_acc = g.accessors[p.attributes.POSITION]
            assert pos_acc.min is not None
            assert pos_acc.max is not None


# ── Animation tests (Phase 6) ───────────────────────────────────────


def _bone(idx: int, parent: int, name_hash: int) -> Bone:
    """Skeleton bone with identity rest pose."""
    rest = BoneTransform(
        q=(0.0, 0.0, 0.0, 1.0),
        wp=(0.0, 0.0, 0.0),
        scale=(1.0, 1.0, 1.0),
    )
    return Bone(
        index=idx, parent_index=parent, name_hash=name_hash, flags=0, lod=0,
        local_trs=rest, inv_bind_trs=rest,
    )


def _make_skinned_two_bone_model() -> MeshData:
    """Minimal skinned model: 2-bone chain, single 3-vert triangle.

    Both bones get a node + node index in the exported glTF, so any
    DecodedBoneAnimation that hits either ``name_hash`` produces a real
    channel. ``bone_palette = (0, 1)`` lets JOINTS_0 byte 0 and 1 map
    cleanly to the global bone indices.
    """
    return MeshData(
        name="rigged_tri",
        vertex_count=3,
        index_count=3,
        submesh_count=1,
        positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        normals=[(0.0, 0.0, 1.0, 0.0)] * 3,
        tangents=[(1.0, 0.0, 0.0, 1.0)] * 3,
        colors=[(1.0, 1.0, 1.0, 1.0)] * 3,
        uvs=[(0.0, 0.0)] * 3,
        joints=[(0, 0, 0, 0), (0, 0, 0, 0), (1, 0, 0, 0)],
        weights=[(1.0, 0.0, 0.0, 0.0)] * 3,
        indices=[0, 1, 2],
        submeshes=[Submesh(
            vertex_count=3, index_count=3, material_index=0,
            bone_palette=(0, 1),
        )],
        skeleton=Skeleton(
            bones=[_bone(0, -1, 0xAAA), _bone(1, 0, 0xBBB)],
            base_bone_count=2, cloth_bone_count=0, template_id=99,
        ),
    )


def _walking_translations(n_frames: int) -> list[tuple[float, float, float]]:
    """``n_frames`` distinct translations so the channel isn't static."""
    return [(float(i), 0.0, 0.0) for i in range(n_frames)]


def _identity_rotations(n_frames: int) -> list[tuple[float, float, float, float]]:
    return [(0.0, 0.0, 0.0, 1.0)] * n_frames


def _unit_scales(n_frames: int) -> list[tuple[float, float, float]]:
    return [(1.0, 1.0, 1.0)] * n_frames


class TestAnimationExport:
    def test_animation_channels_created(self, tmp_path):
        """Two bones × varying T = 2 channels (rotation+scale are static)."""
        mesh = _make_skinned_two_bone_model()
        anim = DecodedAnimation(
            frame_rate=30.0, frame_count=3, compression=0,
            permutation_index=0, name="walk",
            bone_animations=[
                DecodedBoneAnimation(
                    bone_hash=0xAAA,
                    translations=_walking_translations(3),
                    rotations=_identity_rotations(3),
                    scales=_unit_scales(3),
                ),
                DecodedBoneAnimation(
                    bone_hash=0xBBB,
                    translations=_walking_translations(3),
                    rotations=_identity_rotations(3),
                    scales=_unit_scales(3),
                ),
            ],
        )
        out = tmp_path / "anim.glb"
        GltfExporter().export(mesh, out, animations=[anim])

        g = GLTF2.load(str(out))
        assert g.animations is not None
        assert len(g.animations) == 1
        a = g.animations[0]
        assert a.name == "walk"
        # Only translation channels should exist (rot+scale are static).
        assert len(a.channels) == 2
        assert all(c.target.path == "translation" for c in a.channels)
        # One sampler per channel; both reference the SAME input
        # accessor (timestamps shared across channels of one anim).
        assert len(a.samplers) == 2
        assert a.samplers[0].input == a.samplers[1].input

    def test_animation_timestamps(self, tmp_path):
        """Input accessor packs ``[0, 1/30, 2/30]`` for 3 frames @ 30fps."""
        mesh = _make_skinned_two_bone_model()
        anim = DecodedAnimation(
            frame_rate=30.0, frame_count=3, compression=0,
            permutation_index=0, name="walk",
            bone_animations=[DecodedBoneAnimation(
                bone_hash=0xAAA,
                translations=_walking_translations(3),
                rotations=_identity_rotations(3),
                scales=_unit_scales(3),
            )],
        )
        out = tmp_path / "anim.glb"
        GltfExporter().export(mesh, out, animations=[anim])

        g = GLTF2.load(str(out))
        ts_acc_idx = g.animations[0].samplers[0].input
        ts_acc = g.accessors[ts_acc_idx]
        assert ts_acc.count == 3
        # min/max bracket the first/last frame timestamps.
        assert ts_acc.min == [0.0]
        assert ts_acc.max == pytest.approx([2.0 / 30.0])
        # Read the actual timestamp bytes back from the binary blob.
        bv = g.bufferViews[ts_acc.bufferView]
        blob = g.binary_blob()
        raw = blob[bv.byteOffset : bv.byteOffset + bv.byteLength]
        timestamps = list(struct.unpack(f"<{ts_acc.count}f", raw))
        for i, t in enumerate(timestamps):
            assert t == pytest.approx(i / 30.0, rel=1e-5, abs=1e-7)

    def test_animation_coordinate_conversion(self, tmp_path):
        """D4 (x,y,z) → glTF (x, z, -y) applied to translation values."""
        mesh = _make_skinned_two_bone_model()
        # Pick D4-space translations that produce distinct, predictable
        # glTF-space outputs after the swap.
        d4_translations = [
            (1.0, 2.0, 3.0),
            (4.0, 5.0, 6.0),
            (7.0, 8.0, 9.0),
        ]
        anim = DecodedAnimation(
            frame_rate=30.0, frame_count=3, compression=0,
            permutation_index=0, name="walk",
            bone_animations=[DecodedBoneAnimation(
                bone_hash=0xAAA,
                translations=d4_translations,
                rotations=_identity_rotations(3),
                scales=_unit_scales(3),
            )],
        )
        out = tmp_path / "anim.glb"
        GltfExporter(coordinate_transform="z_up_to_y_up").export(
            mesh, out, animations=[anim],
        )

        g = GLTF2.load(str(out))
        ch = next(c for c in g.animations[0].channels
                  if c.target.path == "translation")
        out_acc = g.accessors[g.animations[0].samplers[ch.sampler].output]
        bv = g.bufferViews[out_acc.bufferView]
        blob = g.binary_blob()
        raw = blob[bv.byteOffset : bv.byteOffset + bv.byteLength]
        values = list(struct.unpack(f"<{out_acc.count * 3}f", raw))

        for i, (x, y, z) in enumerate(d4_translations):
            expected = (x, z, -y)  # z_up_to_y_up
            actual = (values[i*3], values[i*3+1], values[i*3+2])
            assert actual == pytest.approx(expected, abs=1e-6)

    def test_animation_static_channel_skip(self, tmp_path):
        """Identical translations across frames → no translation channel."""
        mesh = _make_skinned_two_bone_model()
        # Static T, varying R, static S.
        rotations = [
            (0.0, 0.0, 0.0, 1.0),
            (0.0, math.sin(0.1), 0.0, math.cos(0.1)),
            (0.0, math.sin(0.2), 0.0, math.cos(0.2)),
        ]
        anim = DecodedAnimation(
            frame_rate=30.0, frame_count=3, compression=0,
            permutation_index=0, name="rotate_only",
            bone_animations=[DecodedBoneAnimation(
                bone_hash=0xAAA,
                translations=[(0.0, 0.0, 0.0)] * 3,  # static
                rotations=rotations,
                scales=_unit_scales(3),               # static
            )],
        )
        out = tmp_path / "anim.glb"
        GltfExporter().export(mesh, out, animations=[anim])

        g = GLTF2.load(str(out))
        paths = [c.target.path for c in g.animations[0].channels]
        assert paths == ["rotation"]

    def test_animation_unmapped_bone_skipped(self, tmp_path):
        """Bone hash absent from skeleton → no channel, no crash."""
        mesh = _make_skinned_two_bone_model()
        anim = DecodedAnimation(
            frame_rate=30.0, frame_count=2, compression=0,
            permutation_index=0, name="ghost",
            bone_animations=[
                DecodedBoneAnimation(
                    bone_hash=0xDEADBEEF,  # not in skeleton
                    translations=_walking_translations(2),
                    rotations=_identity_rotations(2),
                    scales=_unit_scales(2),
                ),
                DecodedBoneAnimation(
                    bone_hash=0xAAA,       # in skeleton
                    translations=_walking_translations(2),
                    rotations=_identity_rotations(2),
                    scales=_unit_scales(2),
                ),
            ],
        )
        out = tmp_path / "anim.glb"
        GltfExporter().export(mesh, out, animations=[anim])

        g = GLTF2.load(str(out))
        # Only the real bone produced a channel — the ghost was dropped.
        assert len(g.animations[0].channels) == 1
        # And that channel targets the kept bone's node, not bone 0
        # (bone 0xAAA is bone idx 0 → node idx 1; bone 0xBBB is idx 1 → node 2).
        assert g.animations[0].channels[0].target.node == 1

    def test_animation_permutation_naming(self, tmp_path):
        """Non-zero permutation index appears in the animation name."""
        mesh = _make_skinned_two_bone_model()
        anim = DecodedAnimation(
            frame_rate=30.0, frame_count=2, compression=0,
            permutation_index=3, name="walk",
            bone_animations=[DecodedBoneAnimation(
                bone_hash=0xAAA,
                translations=_walking_translations(2),
                rotations=_identity_rotations(2),
                scales=_unit_scales(2),
            )],
        )
        out = tmp_path / "anim.glb"
        GltfExporter().export(mesh, out, animations=[anim])
        g = GLTF2.load(str(out))
        assert g.animations[0].name == "walk_p3"

    def test_no_animation_backward_compat(self, tmp_path):
        """Export without ``animations`` produces no glTF Animation entries."""
        mesh = _make_skinned_two_bone_model()
        out = tmp_path / "static.glb"
        GltfExporter().export(mesh, out)  # no animations kwarg
        g = GLTF2.load(str(out))
        # pygltflib serialises "no animations" as either an empty list
        # or omits the key altogether — both shapes count as "none".
        assert not g.animations

    def test_constant_override_emits_channel(self, tmp_path):
        """A constant decoded value differing from rest must NOT be skipped.

        Decoder ``count == 1`` curves return a constant baseline that
        may differ from the bone's rest pose (e.g. wings held in a
        "ready" position). With the old ``_values_vary`` predicate the
        channel got dropped as static, then the bone fell back to its
        rest TRS in glTF — silently snapping the wings to rest. The
        rest-aware predicate ``_channel_differs_from_rest`` keeps the
        channel because the constant override differs from rest.

        Regression for: npc_crow garbled in Blender (complex skeletons
        with count=1 baselines).
        """
        # Custom skeleton: bone A has rest_t=(0,0,0), bone B has
        # rest_t=(5, 0, 0). Bone B's animation will be a constant
        # (10, 0, 0) — a clear override of rest, not a match.
        rest_a = BoneTransform(
            q=(0.0, 0.0, 0.0, 1.0), wp=(0.0, 0.0, 0.0),
            scale=(1.0, 1.0, 1.0),
        )
        rest_b = BoneTransform(
            q=(0.0, 0.0, 0.0, 1.0), wp=(5.0, 0.0, 0.0),
            scale=(1.0, 1.0, 1.0),
        )
        skel = Skeleton(
            bones=[
                Bone(index=0, parent_index=-1, name_hash=0xAAA,
                     flags=0, lod=0, local_trs=rest_a, inv_bind_trs=rest_a),
                Bone(index=1, parent_index=0, name_hash=0xBBB,
                     flags=0, lod=0, local_trs=rest_b, inv_bind_trs=rest_b),
            ],
            base_bone_count=2, cloth_bone_count=0, template_id=99,
        )
        mesh = MeshData(
            name="rigged_tri", vertex_count=3, index_count=3,
            submesh_count=1,
            positions=[(0.0,0.0,0.0),(1.0,0.0,0.0),(0.0,1.0,0.0)],
            normals=[(0.0,0.0,1.0,0.0)] * 3,
            tangents=[(1.0,0.0,0.0,1.0)] * 3,
            colors=[(1.0,1.0,1.0,1.0)] * 3,
            uvs=[(0.0,0.0)] * 3,
            joints=[(0,0,0,0),(0,0,0,0),(1,0,0,0)],
            weights=[(1.0,0.0,0.0,0.0)] * 3,
            indices=[0, 1, 2],
            submeshes=[Submesh(
                vertex_count=3, index_count=3, material_index=0,
                bone_palette=(0, 1),
            )],
            skeleton=skel,
        )

        anim = DecodedAnimation(
            frame_rate=30.0, frame_count=4, compression=0,
            permutation_index=0, name="hold_pose",
            bone_animations=[
                # Bone A: every frame matches rest_a → no channel.
                DecodedBoneAnimation(
                    bone_hash=0xAAA,
                    translations=[(0.0, 0.0, 0.0)] * 4,
                    rotations=_identity_rotations(4),
                    scales=_unit_scales(4),
                ),
                # Bone B: constant (10, 0, 0) — DIFFERS from rest_b
                # (5, 0, 0). Must emit a translation channel.
                DecodedBoneAnimation(
                    bone_hash=0xBBB,
                    translations=[(10.0, 0.0, 0.0)] * 4,
                    rotations=_identity_rotations(4),
                    scales=_unit_scales(4),
                ),
            ],
        )
        out = tmp_path / "override.glb"
        # ``coordinate_transform="none"`` keeps the assertion math
        # straightforward — rest_t and animated_t pass through
        # unchanged.
        GltfExporter(coordinate_transform="none").export(
            mesh, out, animations=[anim],
        )

        g = GLTF2.load(str(out))
        assert g.animations and len(g.animations) == 1
        a = g.animations[0]
        # Exactly one channel should have been emitted: bone B's
        # translation override. Bone A's identity-rest match was
        # correctly skipped.
        assert len(a.channels) == 1
        assert a.channels[0].target.path == "translation"
        # And that channel targets bone B's node (bones map to
        # base_index=1, 2 in the node list — mesh node is 0).
        assert a.channels[0].target.node == 2
        # Verify the actual exported values are the override (10, 0,
        # 0), not the rest pose (5, 0, 0). Reading back the binary
        # blob is the only way to catch a regression where the
        # channel exists but stores the wrong values.
        out_acc = g.accessors[a.samplers[a.channels[0].sampler].output]
        bv = g.bufferViews[out_acc.bufferView]
        blob = g.binary_blob()
        raw = blob[bv.byteOffset : bv.byteOffset + bv.byteLength]
        values = list(struct.unpack(f"<{out_acc.count * 3}f", raw))
        for i in range(out_acc.count):
            assert (values[i*3], values[i*3+1], values[i*3+2]) == \
                pytest.approx((10.0, 0.0, 0.0))

    def test_animation_with_no_varying_channels_is_dropped(self, tmp_path):
        """Animations whose every curve is static must NOT emit an entry.

        Regression for: Blender bailing with ``couldn't parse gltf,
        check that the file is valid``. glTF 2.0 §3.6.2 mandates
        ``minItems: 1`` on both ``animation.channels`` and
        ``animation.samplers``; emitting an Animation whose channels
        all got static-skipped produces invalid output the validator
        flags as ``Property 'channels' must be defined``. The fix
        drops the entire entry rather than the individual fields.
        """
        mesh = _make_skinned_two_bone_model()
        # All-rest animation — every bone curve is constant.
        flat = DecodedAnimation(
            frame_rate=30.0, frame_count=4, compression=0,
            permutation_index=0, name="rest_only",
            bone_animations=[
                DecodedBoneAnimation(
                    bone_hash=0xAAA,
                    translations=[(0.0, 0.0, 0.0)] * 4,
                    rotations=_identity_rotations(4),
                    scales=_unit_scales(4),
                ),
                DecodedBoneAnimation(
                    bone_hash=0xBBB,
                    translations=[(0.0, 0.0, 0.0)] * 4,
                    rotations=_identity_rotations(4),
                    scales=_unit_scales(4),
                ),
            ],
        )
        # Real animation — one bone actually translates.
        real = DecodedAnimation(
            frame_rate=30.0, frame_count=4, compression=0,
            permutation_index=0, name="moving",
            bone_animations=[
                DecodedBoneAnimation(
                    bone_hash=0xBBB,
                    translations=_walking_translations(4),
                    rotations=_identity_rotations(4),
                    scales=_unit_scales(4),
                ),
            ],
        )
        out = tmp_path / "mixed.glb"
        GltfExporter().export(mesh, out, animations=[flat, real])

        g = GLTF2.load(str(out))
        # The static animation was dropped; only ``moving`` survived.
        assert g.animations and len(g.animations) == 1
        kept = g.animations[0]
        assert kept.name == "moving"
        # And — most importantly — the kept entry has non-empty
        # channels + samplers, satisfying the glTF schema.
        assert len(kept.channels) >= 1
        assert len(kept.samplers) >= 1


# ── Synthetic rest-pose Action ───────────────────────────────────────


class TestRestPoseExport:
    """The synthetic ``rest_pose`` Action (see build_rest_pose_animation).

    A rest-pose animation holds every bone AT rest. Without an opt-out,
    ``_channel_differs_from_rest`` would prune all 3N channels and the
    exporter would drop the channel-less entry — leaving Blender users
    with no clickable "snap to bind" Action. ``force_static_channels``
    bypasses that skip.
    """

    def test_force_static_emits_all_channels(self, tmp_path):
        """force_static_channels=True → every bone gets T+R+S channels."""
        mesh = _make_skinned_two_bone_model()  # 2 bones, identity rest
        rest_anim = build_rest_pose_animation(mesh.skeleton)
        rest_anim.force_static_channels = True

        out = tmp_path / "rest.glb"
        GltfExporter().export(mesh, out, animations=[rest_anim])

        g = GLTF2.load(str(out))
        assert g.animations is not None and len(g.animations) == 1
        a = g.animations[0]
        assert a.name == "rest_pose"
        # N bones × 3 channels (translation, rotation, scale).
        assert len(a.channels) == 2 * 3
        assert sorted(c.target.path for c in a.channels) == [
            "rotation", "rotation",
            "scale", "scale",
            "translation", "translation",
        ]
        # Single rest keyframe — all samplers share one timestamp input.
        assert len({s.input for s in a.samplers}) == 1
        assert g.accessors[a.samplers[0].input].count == 1

    def test_force_static_off_drops_the_entry(self, tmp_path):
        """force_static_channels=False (the default) → rest-pose is inert.

        Every channel equals rest, so the static-skip optimisation
        prunes them all and the exporter drops the now channel-less
        entry (glTF 2.0 §3.6.2 requires minItems:1). Proves the
        optimisation still applies to anything NOT flagged.
        """
        mesh = _make_skinned_two_bone_model()
        rest_anim = build_rest_pose_animation(mesh.skeleton)
        # Leave force_static_channels at its default False.

        out = tmp_path / "rest_off.glb"
        GltfExporter().export(mesh, out, animations=[rest_anim])

        g = GLTF2.load(str(out))
        assert not g.animations

    def test_rest_pose_channels_hold_bind_values(self, tmp_path):
        """The emitted channels carry each bone's rest TRS verbatim."""
        # Posed skeleton: bone B has a non-identity rest translation.
        rest_a = BoneTransform(q=(0.0, 0.0, 0.0, 1.0), wp=(0.0, 0.0, 0.0),
                               scale=(1.0, 1.0, 1.0))
        rest_b = BoneTransform(q=(0.0, 0.0, 0.0, 1.0), wp=(7.0, 8.0, 9.0),
                               scale=(1.0, 1.0, 1.0))
        skel = Skeleton(
            bones=[
                Bone(index=0, parent_index=-1, name_hash=0xAAA,
                     flags=0, lod=0, local_trs=rest_a, inv_bind_trs=rest_a),
                Bone(index=1, parent_index=0, name_hash=0xBBB,
                     flags=0, lod=0, local_trs=rest_b, inv_bind_trs=rest_b),
            ],
            base_bone_count=2, cloth_bone_count=0, template_id=99,
        )
        mesh = MeshData(
            name="rigged_tri", vertex_count=3, index_count=3, submesh_count=1,
            positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            normals=[(0.0, 0.0, 1.0, 0.0)] * 3,
            tangents=[(1.0, 0.0, 0.0, 1.0)] * 3,
            colors=[(1.0, 1.0, 1.0, 1.0)] * 3,
            uvs=[(0.0, 0.0)] * 3,
            joints=[(0, 0, 0, 0), (0, 0, 0, 0), (1, 0, 0, 0)],
            weights=[(1.0, 0.0, 0.0, 0.0)] * 3,
            indices=[0, 1, 2],
            submeshes=[Submesh(vertex_count=3, index_count=3,
                               material_index=0, bone_palette=(0, 1))],
            skeleton=skel,
        )
        rest_anim = build_rest_pose_animation(skel)
        rest_anim.force_static_channels = True

        out = tmp_path / "rest_vals.glb"
        # coordinate_transform="none" keeps the TRS values unchanged so
        # the readback is a direct comparison.
        GltfExporter(coordinate_transform="none").export(
            mesh, out, animations=[rest_anim],
        )

        g = GLTF2.load(str(out))
        a = g.animations[0]
        # Bone B is bone index 1 → node index 2.
        ch = next(c for c in a.channels
                  if c.target.path == "translation" and c.target.node == 2)
        out_acc = g.accessors[a.samplers[ch.sampler].output]
        bv = g.bufferViews[out_acc.bufferView]
        raw = g.binary_blob()[bv.byteOffset : bv.byteOffset + bv.byteLength]
        values = list(struct.unpack(f"<{out_acc.count * 3}f", raw))
        assert out_acc.count == 1  # single rest keyframe
        assert tuple(values) == pytest.approx((7.0, 8.0, 9.0))


# ── Per-piece assembly export (Character Builder) ───────────────────


class TestAssemblyExport:
    """``export_assembly``: N skinned meshes sharing one skin.

    The Character Builder writes each equipped piece as its own glTF
    mesh node so Blender shows one Armature with N independently-
    toggleable mesh objects, rather than one consolidated mesh.
    """

    @staticmethod
    def _skeleton() -> Skeleton:
        """Two-bone skeleton, shared by every piece in a test assembly."""
        return Skeleton(
            bones=[_bone(0, -1, 0xAAA), _bone(1, 0, 0xBBB)],
            base_bone_count=2, cloth_bone_count=0, template_id=99,
        )

    @staticmethod
    def _piece(name: str) -> MeshData:
        """A distinctly-named skinned one-triangle piece."""
        m = _make_skinned_two_bone_model()
        m.name = name
        return m

    def test_one_mesh_node_per_piece_sharing_skin(self, tmp_path):
        skel = self._skeleton()
        pieces = [
            (self._piece("character_chest"), None),
            (self._piece("character_helmet"), None),
        ]
        out = tmp_path / "assembly.glb"
        GltfExporter().export_assembly(pieces, skel, out)

        g = GLTF2.load(str(out))
        # Two skinned meshes, exactly one shared skin.
        assert len(g.meshes) == 2
        assert len(g.skins) == 1
        # One node per piece, each referencing skin 0.
        skinned_nodes = [n for n in g.nodes if n.skin is not None]
        assert len(skinned_nodes) == 2
        assert all(n.skin == 0 for n in skinned_nodes)
        # The two mesh nodes point at distinct meshes.
        assert {n.mesh for n in skinned_nodes} == {0, 1}
        # Mesh names are derived from the source pieces.
        assert {m.name for m in g.meshes} == {
            "character_chest", "character_helmet",
        }

    def test_static_extras_have_no_skin(self, tmp_path):
        skel = self._skeleton()
        pieces = [
            (self._piece("character_chest"), None),
            (self._piece("character_helmet"), None),
        ]
        weapon = _make_triangle_model("static_weapon")
        out = tmp_path / "assembly_weapon.glb"
        GltfExporter().export_assembly(
            pieces, skel, out, static_extras=[(weapon, None)],
        )

        g = GLTF2.load(str(out))
        # 2 skinned + 1 extra = 3 meshes, still one skin.
        assert len(g.meshes) == 3
        assert len(g.skins) == 1
        # The extra mesh's node carries no skin reference.
        weapon_idx = next(
            i for i, m in enumerate(g.meshes) if m.name == "static_weapon"
        )
        weapon_nodes = [n for n in g.nodes if n.mesh == weapon_idx]
        assert len(weapon_nodes) == 1
        assert weapon_nodes[0].skin is None
        # Both skinned piece nodes still share skin 0.
        skinned_nodes = [n for n in g.nodes if n.skin is not None]
        assert len(skinned_nodes) == 2
        assert all(n.skin == 0 for n in skinned_nodes)

    def test_animation_targets_shared_bones(self, tmp_path):
        """One animation drives the bone nodes shared by every piece."""
        skel = self._skeleton()
        pieces = [
            (self._piece("character_chest"), None),
            (self._piece("character_helmet"), None),
        ]
        anim = DecodedAnimation(
            frame_rate=30.0, frame_count=3, compression=0,
            permutation_index=0, name="walk",
            bone_animations=[DecodedBoneAnimation(
                bone_hash=0xAAA,
                translations=_walking_translations(3),
                rotations=_identity_rotations(3),
                scales=_unit_scales(3),
            )],
        )
        out = tmp_path / "assembly_anim.glb"
        GltfExporter().export_assembly(pieces, skel, out, animations=[anim])

        g = GLTF2.load(str(out))
        assert g.animations is not None and len(g.animations) == 1
        # The channel targets a bone node that is part of the shared skin.
        bone_nodes = set(g.skins[0].joints)
        assert all(
            c.target.node in bone_nodes for c in g.animations[0].channels
        )

    def test_single_piece_assembly(self, tmp_path):
        """Degenerate one-piece assembly: 1 mesh + 1 skin, no crash."""
        skel = self._skeleton()
        out = tmp_path / "assembly_single.glb"
        GltfExporter().export_assembly(
            [(self._piece("character_face"), None)], skel, out,
        )
        g = GLTF2.load(str(out))
        assert len(g.meshes) == 1
        assert len(g.skins) == 1
        assert g.nodes[0].skin == 0

    def test_per_piece_submesh_filter(self, tmp_path):
        """A piece whose submesh filter excludes everything emits no node."""
        skel = self._skeleton()
        pieces = [
            (self._piece("character_chest"), None),
            (self._piece("character_helmet"), set()),  # filter out all
        ]
        out = tmp_path / "assembly_filtered.glb"
        GltfExporter().export_assembly(pieces, skel, out)

        g = GLTF2.load(str(out))
        # Only the unfiltered piece survives.
        assert len(g.meshes) == 1
        assert g.meshes[0].name == "character_chest"
        assert len(g.skins) == 1
