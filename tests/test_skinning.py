"""Tests for the GUI's CPU linear-blend skinning helpers."""

from __future__ import annotations

import math

import numpy as np
import pytest

from d4extract.formats.app_parser import (
    Bone, BoneTransform, Skeleton, Submesh,
)
from d4extract.gui.skinning import (
    build_bone_hash_to_anim_map,
    build_inv_bind_matrices,
    build_rest_local_matrices,
    compute_skin_matrices,
    compute_world_matrices,
    remap_joints_to_global,
    skin_vertices,
    trs_to_matrix,
)


IDENTITY_QUAT = (0.0, 0.0, 0.0, 1.0)
ZERO_VEC3 = (0.0, 0.0, 0.0)
UNIT_SCALE = (1.0, 1.0, 1.0)


# ─── trs_to_matrix ───────────────────────────────────────────────────


class TestTRSMatrix:
    def test_identity_yields_identity_matrix(self) -> None:
        m = trs_to_matrix(ZERO_VEC3, IDENTITY_QUAT, UNIT_SCALE)
        assert np.allclose(m, np.eye(4))

    def test_translation_only(self) -> None:
        m = trs_to_matrix((5.0, -3.0, 2.0), IDENTITY_QUAT, UNIT_SCALE)
        expected = np.eye(4)
        expected[0, 3] = 5.0
        expected[1, 3] = -3.0
        expected[2, 3] = 2.0
        assert np.allclose(m, expected)

    def test_rotation_90_around_z(self) -> None:
        # 90deg about +Z = (0, 0, sin(45deg), cos(45deg)).
        s = math.sin(math.pi / 4)
        c = math.cos(math.pi / 4)
        m = trs_to_matrix(ZERO_VEC3, (0.0, 0.0, s, c), UNIT_SCALE)
        # Rotating x-axis (1,0,0) about Z by +90deg produces (0, 1, 0).
        rotated = m @ np.array([1.0, 0.0, 0.0, 1.0], dtype=np.float32)
        assert rotated[0] == pytest.approx(0.0, abs=1e-5)
        assert rotated[1] == pytest.approx(1.0, abs=1e-5)
        assert rotated[2] == pytest.approx(0.0, abs=1e-5)

    def test_uniform_scale(self) -> None:
        m = trs_to_matrix(ZERO_VEC3, IDENTITY_QUAT, (2.0, 2.0, 2.0))
        v = m @ np.array([1.5, -2.0, 0.5, 1.0], dtype=np.float32)
        assert v[0] == pytest.approx(3.0)
        assert v[1] == pytest.approx(-4.0)
        assert v[2] == pytest.approx(1.0)

    def test_composition_order_is_TRS(self) -> None:
        """T * R * S — translation applies *after* rotate-then-scale."""
        # 180deg about Z + uniform 2x scale + offset.
        m = trs_to_matrix(
            (10.0, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),     # 180deg about Z = quat (0, 0, 1, 0)
            (2.0, 2.0, 2.0),
        )
        v = m @ np.array([1.0, 0.0, 0.0, 1.0], dtype=np.float32)
        # (1, 0, 0) -> scale by 2 -> (2, 0, 0) -> 180 about Z -> (-2, 0, 0)
        # -> translate +10 -> (8, 0, 0)
        assert v[0] == pytest.approx(8.0, abs=1e-5)
        assert v[1] == pytest.approx(0.0, abs=1e-5)
        assert v[2] == pytest.approx(0.0, abs=1e-5)


# ─── Skeleton helpers ────────────────────────────────────────────────


def _bone(
    index: int, parent: int, name_hash: int, *,
    local_t=ZERO_VEC3, local_q=IDENTITY_QUAT, local_s=UNIT_SCALE,
    inv_bind_t=ZERO_VEC3, inv_bind_q=IDENTITY_QUAT, inv_bind_s=UNIT_SCALE,
) -> Bone:
    return Bone(
        index=index, parent_index=parent, name_hash=name_hash,
        flags=0, lod=0,
        local_trs=BoneTransform(local_q, local_t, local_s),
        inv_bind_trs=BoneTransform(inv_bind_q, inv_bind_t, inv_bind_s),
    )


def _make_chain_skeleton(local_translations: list[tuple[float, float, float]]) -> Skeleton:
    """Build a parent -> child chain. inv_bind = inverse of world rest pose."""
    bones: list[Bone] = []
    world = []
    parent = -1
    accum = np.eye(4, dtype=np.float32)
    for i, t in enumerate(local_translations):
        local = trs_to_matrix(t, IDENTITY_QUAT, UNIT_SCALE)
        accum = local if parent == -1 else (world[parent] @ local)
        world.append(accum)
        inv = np.linalg.inv(accum)
        bones.append(Bone(
            index=i,
            parent_index=parent,
            name_hash=0x1000 + i,
            flags=0, lod=0,
            local_trs=BoneTransform(IDENTITY_QUAT, t, UNIT_SCALE),
            inv_bind_trs=BoneTransform(
                IDENTITY_QUAT,
                # Decompose inv-bind back to a TRS triple.  These are
                # pure translations so the inverse is just (-x, -y, -z).
                (-accum[0, 3], -accum[1, 3], -accum[2, 3]),
                UNIT_SCALE,
            ),
        ))
        parent = i
    return Skeleton(bones=bones, base_bone_count=len(bones))


# ─── build_inv_bind_matrices / rest_local ────────────────────────────


class TestSkeletonMatrices:
    def test_inv_bind_round_trip(self) -> None:
        skel = _make_chain_skeleton([(0, 0, 0), (1, 0, 0), (1, 0, 0)])
        inv_bind = build_inv_bind_matrices(skel)
        rest_local = build_rest_local_matrices(skel)

        # Walk the world rest poses and verify world @ inv_bind == identity.
        n = len(skel.bones)
        world = np.empty((n, 4, 4), dtype=np.float32)
        for bone in skel.bones:
            local = rest_local[bone.index]
            if bone.parent_index >= 0:
                world[bone.index] = world[bone.parent_index] @ local
            else:
                world[bone.index] = local
        for i in range(n):
            product = world[i] @ inv_bind[i]
            assert np.allclose(product, np.eye(4), atol=1e-5)


# ─── compute_world_matrices ──────────────────────────────────────────


class _StubBoneAnim:
    def __init__(
        self,
        bone_hash: int,
        translations,
        rotations,
        scales,
    ) -> None:
        self.bone_hash = bone_hash
        self.translations = translations
        self.rotations = rotations
        self.scales = scales


class _StubDecoded:
    def __init__(self, bone_animations) -> None:
        self.bone_animations = list(bone_animations)


class TestComputeWorldMatrices:
    def test_single_bone_world_equals_local(self) -> None:
        skel = _make_chain_skeleton([(0, 0, 0)])
        rest_local = build_rest_local_matrices(skel)
        decoded = _StubDecoded([])
        world = compute_world_matrices(skel, decoded, 0, {}, rest_local)
        assert np.allclose(world[0], np.eye(4))

    def test_chain_world_is_parent_times_local(self) -> None:
        skel = _make_chain_skeleton([(0, 0, 0), (2, 0, 0), (3, 0, 0)])
        rest_local = build_rest_local_matrices(skel)
        decoded = _StubDecoded([])
        world = compute_world_matrices(skel, decoded, 0, {}, rest_local)
        # Bone 1 should be 2 units from origin; bone 2 should be 5.
        assert world[1][0, 3] == pytest.approx(2.0)
        assert world[2][0, 3] == pytest.approx(5.0)

    def test_anim_translation_overrides_rest(self) -> None:
        """``DecodedBoneAnimation`` carries absolute local translations
        (the decoder bakes baseline+delta on its way out).  Skinning
        plugs them straight into ``trs_to_matrix``.
        """
        skel = _make_chain_skeleton([(0, 0, 0)])
        rest_local = build_rest_local_matrices(skel)
        decoded = _StubDecoded([
            _StubBoneAnim(
                bone_hash=skel.bones[0].name_hash,
                translations=[(5.0, 0.0, 0.0)],   # absolute local
                rotations=[IDENTITY_QUAT],
                scales=[UNIT_SCALE],
            ),
        ])
        hash_map = build_bone_hash_to_anim_map(decoded)
        world = compute_world_matrices(skel, decoded, 0, hash_map, rest_local)
        assert world[0][0, 3] == pytest.approx(5.0)

    def test_unanimated_bones_use_rest_pose(self) -> None:
        skel = _make_chain_skeleton([(0, 0, 0), (3, 0, 0)])
        rest_local = build_rest_local_matrices(skel)
        # Animate only bone 0 to (0, 1, 0) absolute; bone 1 inherits its
        # rest local of (3, 0, 0) verbatim because it has no anim entry.
        decoded = _StubDecoded([
            _StubBoneAnim(
                bone_hash=skel.bones[0].name_hash,
                translations=[(0.0, 1.0, 0.0)],
                rotations=[IDENTITY_QUAT],
                scales=[UNIT_SCALE],
            ),
        ])
        hash_map = build_bone_hash_to_anim_map(decoded)
        world = compute_world_matrices(skel, decoded, 0, hash_map, rest_local)
        # Bone 1 = parent_world * rest_local = (0,1,0) + (3,0,0) -> (3,1,0).
        assert world[1][0, 3] == pytest.approx(3.0)
        assert world[1][1, 3] == pytest.approx(1.0)

    def test_translation_is_absolute_not_added_to_rest(self) -> None:
        """rest_t=(10, 0, 0) but anim translation = (0, 2, 0) absolute →
        world translation must be (0, 2, 0), NOT (10, 2, 0).  This is
        what guards against the spaghetti-limb regression that used to
        happen when skinning added rest_t to the decoded value (chest
        Neutral round-tripped only because rest_t for the lid happened
        to match the curve baseline; on real characters the doubled
        offsets stretched every limb).
        """
        skel = _make_chain_skeleton([(10, 0, 0)])
        rest_local = build_rest_local_matrices(skel)
        decoded = _StubDecoded([
            _StubBoneAnim(
                bone_hash=skel.bones[0].name_hash,
                translations=[(0.0, 2.0, 0.0)],
                rotations=[IDENTITY_QUAT],
                scales=[UNIT_SCALE],
            ),
        ])
        hash_map = build_bone_hash_to_anim_map(decoded)
        world = compute_world_matrices(skel, decoded, 0, hash_map, rest_local)
        assert world[0][0, 3] == pytest.approx(0.0)
        assert world[0][1, 3] == pytest.approx(2.0)

# ─── skin_vertices ───────────────────────────────────────────────────


class TestSkinVertices:
    def test_identity_skin_matrices_leave_positions_unchanged(self) -> None:
        rest = np.array(
            [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (-1.0, 0.0, 7.0)],
            dtype=np.float32,
        )
        joints = np.zeros((3, 4), dtype=np.int32)
        weights = np.zeros((3, 4), dtype=np.float32)
        weights[:, 0] = 1.0
        skin_mats = np.tile(np.eye(4, dtype=np.float32), (1, 1, 1))
        skinned = skin_vertices(rest, joints, weights, skin_mats)
        assert np.allclose(skinned, rest)

    def test_single_full_influence_follows_one_bone(self) -> None:
        """A vertex with weight=(1,0,0,0) bound to bone 1 must move by
        exactly bone 1's skin matrix."""
        rest = np.array([(0.0, 0.0, 0.0)], dtype=np.float32)
        joints = np.array([[1, 0, 0, 0]], dtype=np.int32)
        weights = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)

        # Two bones: bone 0 = identity; bone 1 = translate +(10, 0, 0).
        skin_mats = np.stack([
            np.eye(4, dtype=np.float32),
            trs_to_matrix((10.0, 0.0, 0.0), IDENTITY_QUAT, UNIT_SCALE),
        ])
        out = skin_vertices(rest, joints, weights, skin_mats)
        assert out.shape == (1, 3)
        assert out[0, 0] == pytest.approx(10.0)

    def test_blend_two_bones_50_50(self) -> None:
        rest = np.array([(0.0, 0.0, 0.0)], dtype=np.float32)
        joints = np.array([[0, 1, 0, 0]], dtype=np.int32)
        weights = np.array([[0.5, 0.5, 0.0, 0.0]], dtype=np.float32)
        skin_mats = np.stack([
            trs_to_matrix((4.0, 0.0, 0.0), IDENTITY_QUAT, UNIT_SCALE),
            trs_to_matrix((0.0, 6.0, 0.0), IDENTITY_QUAT, UNIT_SCALE),
        ])
        out = skin_vertices(rest, joints, weights, skin_mats)
        # Halfway between (4,0,0) and (0,6,0).
        assert out[0, 0] == pytest.approx(2.0)
        assert out[0, 1] == pytest.approx(3.0)

    def test_zero_vertex_count_returns_empty(self) -> None:
        rest = np.zeros((0, 3), dtype=np.float32)
        out = skin_vertices(
            rest,
            np.zeros((0, 4), dtype=np.int32),
            np.zeros((0, 4), dtype=np.float32),
            np.tile(np.eye(4, dtype=np.float32), (1, 1, 1)),
        )
        assert out.shape == (0, 3)


# ─── compute_skin_matrices ───────────────────────────────────────────


class TestComputeSkinMatrices:
    def test_world_times_inv_bind(self) -> None:
        world = np.tile(np.eye(4, dtype=np.float32), (2, 1, 1))
        # Each "world" matrix is an identity; multiplied by the inv-bind
        # arr gives the inv-bind back.
        inv_bind = np.zeros((2, 4, 4), dtype=np.float32)
        inv_bind[0] = trs_to_matrix((1.0, 0.0, 0.0), IDENTITY_QUAT, UNIT_SCALE)
        inv_bind[1] = trs_to_matrix((0.0, 0.0, -2.0), IDENTITY_QUAT, UNIT_SCALE)
        skin = compute_skin_matrices(world, inv_bind)
        assert np.allclose(skin, inv_bind)


# ─── remap_joints_to_global ──────────────────────────────────────────


class TestRemapJoints:
    def test_palette_lookup(self) -> None:
        joints = [(0, 1, 0, 0), (1, 0, 0, 0)]
        # Submesh covers both vertices; palette maps local 0 -> 7, 1 -> 9.
        sm = Submesh(
            vertex_offset=0, vertex_count=2,
            index_offset=0, index_count=0,
            material_index=0,
            bone_palette=(7, 9),
        )
        out = remap_joints_to_global(joints, [sm], skeleton_bone_count=10)
        expected = np.array([[7, 9, 7, 7], [9, 7, 7, 7]], dtype=np.int32)
        assert np.array_equal(out, expected)

    def test_missing_palette_uses_raw_indices(self) -> None:
        joints = [(0, 1, 2, 0)]
        sm = Submesh(
            vertex_offset=0, vertex_count=1,
            index_offset=0, index_count=0,
            material_index=0,
            bone_palette=(),
        )
        out = remap_joints_to_global(joints, [sm], skeleton_bone_count=5)
        assert np.array_equal(
            out, np.array([[0, 1, 2, 0]], dtype=np.int32),
        )

    def test_out_of_range_palette_index_falls_back_to_zero(self) -> None:
        joints = [(0, 5, 0, 0)]   # local index 5 with palette of length 2
        sm = Submesh(
            vertex_offset=0, vertex_count=1,
            index_offset=0, index_count=0,
            material_index=0,
            bone_palette=(3, 4),
        )
        out = remap_joints_to_global(joints, [sm], skeleton_bone_count=10)
        # Local 0 -> palette[0]=3; local 5 is OOB -> 0.
        assert np.array_equal(
            out, np.array([[3, 0, 3, 3]], dtype=np.int32),
        )

    def test_palette_value_exceeding_skeleton_clamped(self) -> None:
        """A palette entry that points past the last bone is treated as 0."""
        joints = [(0, 1, 0, 0)]
        sm = Submesh(
            vertex_offset=0, vertex_count=1,
            index_offset=0, index_count=0,
            material_index=0,
            bone_palette=(2, 99),
        )
        out = remap_joints_to_global(joints, [sm], skeleton_bone_count=5)
        assert np.array_equal(
            out, np.array([[2, 0, 2, 2]], dtype=np.int32),
        )

    def test_multiple_submeshes_apply_their_own_palettes(self) -> None:
        joints = [
            (0, 1, 0, 0),  # submesh 0
            (1, 0, 0, 0),  # submesh 0
            (0, 1, 0, 0),  # submesh 1
            (1, 0, 0, 0),  # submesh 1
        ]
        sm0 = Submesh(
            vertex_offset=0, vertex_count=2,
            index_offset=0, index_count=0,
            material_index=0,
            bone_palette=(10, 11),
        )
        sm1 = Submesh(
            vertex_offset=2, vertex_count=2,
            index_offset=0, index_count=0,
            material_index=0,
            bone_palette=(20, 21),
        )
        out = remap_joints_to_global(joints, [sm0, sm1], skeleton_bone_count=30)
        expected = np.array([
            [10, 11, 10, 10],
            [11, 10, 10, 10],
            [20, 21, 20, 20],
            [21, 20, 20, 20],
        ], dtype=np.int32)
        assert np.array_equal(out, expected)


# ─── End-to-end one-bone rest-pose round-trip ────────────────────────


class TestEndToEndRestPose:
    def test_rest_pose_animation_round_trips_to_input_positions(self) -> None:
        """An animation that supplies each bone's rest local TRS as the
        per-frame absolute value should round-trip to the input vertex
        stream.

        The decoder produces absolute local TRS values (count==0 falls
        back to rest, count==1 returns baseline, count>=2 returns
        baseline+delta).  Mirror that here: the stub feeds rest_t /
        rest_q / rest_s as the "no motion" reading.
        """
        skel = _make_chain_skeleton([(0, 0, 0), (1, 0, 0), (1, 0, 0)])
        rest_local = build_rest_local_matrices(skel)
        inv_bind = build_inv_bind_matrices(skel)

        rest_positions = np.array([
            (0.5, 0.0, 0.0),
            (1.5, 0.0, 0.0),
            (2.5, 0.0, 0.0),
        ], dtype=np.float32)
        joints = np.array([
            [0, 0, 0, 0],
            [1, 0, 0, 0],
            [2, 0, 0, 0],
        ], dtype=np.int32)
        weights = np.zeros((3, 4), dtype=np.float32)
        weights[:, 0] = 1.0

        # "No motion" animation: each bone reports its rest TRS verbatim.
        decoded = _StubDecoded([
            _StubBoneAnim(
                bone_hash=b.name_hash,
                translations=[b.local_trs.wp],
                rotations=[b.local_trs.q],
                scales=[b.local_trs.scale],
            )
            for b in skel.bones
        ])
        hash_map = build_bone_hash_to_anim_map(decoded)

        world = compute_world_matrices(skel, decoded, 0, hash_map, rest_local)
        skin = compute_skin_matrices(world, inv_bind)
        skinned = skin_vertices(rest_positions, joints, weights, skin)

        assert np.allclose(skinned, rest_positions, atol=1e-5)
