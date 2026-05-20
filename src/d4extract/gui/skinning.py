"""CPU linear-blend skinning helpers for the animation viewport.

All math runs in D4-native (left-handed Z-up) coordinates.  The
viewport handles the D4-to-VTK axis swap on the *final* skinned vertex
positions — never on bone transforms or inverse-bind matrices, since
that would tear the matrix multiply chain.

Contract with :class:`MeshData` from ``app_parser``:

- ``MeshData.joints`` is per-vertex 4-byte palette indices (raw values
  out of ``JOINTS_0``).  Each :class:`Submesh` carries its own
  ``bone_palette`` mapping those local bytes to global bone indices.
- ``MeshData.weights`` is per-vertex 4-float blend weights.
- ``Skeleton.bones`` is topologically ordered (parents before children),
  guaranteed by the existing skeleton parser.
- ``Bone.inv_bind_trs`` already carries the inverse-bind transform
  (``transformSkinningInv`` from the .app file) — we use it directly
  instead of recomputing from the rest pose.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from d4extract.formats.anim_parser import (
        DecodedAnimation,
        DecodedBoneAnimation,
    )
    from d4extract.formats.app_parser import (
        Bone,
        BoneTransform,
        MeshData,
        Skeleton,
        Submesh,
    )

log = logging.getLogger(__name__)


# ─── Low-level matrix builders ───────────────────────────────────────


def trs_to_matrix(
    translation: tuple[float, float, float],
    quat: tuple[float, float, float, float],
    scale: tuple[float, float, float],
) -> np.ndarray:
    """Build a 4x4 transform matrix from a (translation, quat, scale) triple.

    Quaternion is ``(x, y, z, w)`` — D3D order, matching both the
    skeleton parser (``Bone.local_trs.q``) and the animation decoder.
    Composition is ``T * R * S``.
    """
    x, y, z, w = quat
    sx, sy, sz = scale
    tx, ty, tz = translation

    # Pre-multiply rotation columns by scale (R * S without building S
    # separately).  The full matrix has translation in the last column.
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z

    m = np.empty((4, 4), dtype=np.float32)
    m[0, 0] = (1.0 - 2.0 * (yy + zz)) * sx
    m[0, 1] = (2.0 * (xy - wz)) * sy
    m[0, 2] = (2.0 * (xz + wy)) * sz
    m[0, 3] = tx
    m[1, 0] = (2.0 * (xy + wz)) * sx
    m[1, 1] = (1.0 - 2.0 * (xx + zz)) * sy
    m[1, 2] = (2.0 * (yz - wx)) * sz
    m[1, 3] = ty
    m[2, 0] = (2.0 * (xz - wy)) * sx
    m[2, 1] = (2.0 * (yz + wx)) * sy
    m[2, 2] = (1.0 - 2.0 * (xx + yy)) * sz
    m[2, 3] = tz
    m[3, 0] = 0.0
    m[3, 1] = 0.0
    m[3, 2] = 0.0
    m[3, 3] = 1.0
    return m


def _bone_transform_to_matrix(t: "BoneTransform") -> np.ndarray:
    return trs_to_matrix(t.wp, t.q, t.scale)


def build_inv_bind_matrices(skeleton: "Skeleton") -> np.ndarray:
    """Return the inverse-bind matrix array as ``(B, 4, 4) float32``.

    Uses each bone's parser-supplied ``inv_bind_trs`` directly — the
    skeleton parser reads it from ``transformSkinningInv`` in the .app
    payload, so we don't recompute (and risk diverging from) the
    engine's own bind pose.
    """
    n = len(skeleton.bones)
    mats = np.empty((n, 4, 4), dtype=np.float32)
    for bone in skeleton.bones:
        mats[bone.index] = _bone_transform_to_matrix(bone.inv_bind_trs)
    return mats


def build_rest_local_matrices(skeleton: "Skeleton") -> np.ndarray:
    """Return each bone's rest-pose local matrix (parent-relative)."""
    n = len(skeleton.bones)
    mats = np.empty((n, 4, 4), dtype=np.float32)
    for bone in skeleton.bones:
        mats[bone.index] = _bone_transform_to_matrix(bone.local_trs)
    return mats


# ─── Hierarchy walk ──────────────────────────────────────────────────


def compute_world_matrices(
    skeleton: "Skeleton",
    decoded: "DecodedAnimation",
    frame: int,
    bone_hash_to_anim: dict[int, int],
    rest_local_mats: np.ndarray,
) -> np.ndarray:
    """Compose every bone's world matrix at ``frame``.

    For each bone (in topological order):
      1. Build the bone's local 4x4 — from the animation curve when one
         exists for this bone hash, else from the cached rest-pose
         local matrix.
      2. Multiply by the parent's already-computed world matrix
         (or use the local directly for roots).

    All three TRS channels coming out of the decoder are **absolute
    local-space values**, not deltas: the translation decoder bakes
    ``baseline + keyframe_delta`` into its output for ``count >= 2``
    curves, returns the curve baseline directly for ``count == 1``
    (matches the engine: a count-1 curve overrides the rest pose with
    a custom absolute, as in dogLarge's loading-screen pose), and
    falls back to the bone's rest values for empty curves.  Rotation
    and scale curves are likewise stored absolute.  Skinning therefore
    plugs the decoded TRS straight into ``trs_to_matrix`` — adding
    ``bone.local_trs.*`` here would double-count the constant /
    fallback cases and produce the visible "spaghetti limb" stretch
    on real characters (chest worked because both bones happened to
    have rest_t close to zero).

    Returns ``(B, 4, 4) float32``.  Caller can multiply by the inverse-
    bind matrices to produce skin matrices.
    """
    n = len(skeleton.bones)
    world = np.empty((n, 4, 4), dtype=np.float32)
    bone_anims = decoded.bone_animations
    for bone in skeleton.bones:
        i = bone.index
        anim_idx = bone_hash_to_anim.get(bone.name_hash)
        if anim_idx is not None:
            ab = bone_anims[anim_idx]
            local = trs_to_matrix(
                ab.translations[frame],
                ab.rotations[frame],
                ab.scales[frame],
            )
        else:
            local = rest_local_mats[i]
        if bone.parent_index >= 0:
            world[i] = world[bone.parent_index] @ local
        else:
            world[i] = local
    return world


# ─── Joint remapping (palette → global) ──────────────────────────────


def remap_joints_to_global(
    joints: list[tuple[int, int, int, int]],
    submeshes: list["Submesh"],
    skeleton_bone_count: int,
) -> np.ndarray:
    """Convert the per-submesh-palette joint indices to global indices.

    For every vertex in submesh ``s``:

        global_index = palette[joint_byte]   if palette present
        global_index = joint_byte            otherwise (Azmodan path)

    Out-of-range values clamp to 0 — matches the glTF exporter's
    behaviour (``_remap_joints`` in ``gltf_export``) and stays safe
    when the unused slot's weight is 0.

    Returns ``(N, 4) int32`` — one row per vertex of the global mesh.
    """
    if not joints:
        return np.zeros((0, 4), dtype=np.int32)

    # Start with the raw JOINTS_0 bytes; we'll overwrite per-submesh
    # rows below using each submesh's palette.  Rows the submesh ranges
    # don't cover (gaps from cloth-only or unused vertices) keep their
    # raw values clamped to [0, B-1].
    raw = np.asarray(joints, dtype=np.int32)
    out = raw.copy()
    out[out >= skeleton_bone_count] = 0
    out[out < 0] = 0

    n_verts = raw.shape[0]
    for sm in submeshes:
        palette = sm.bone_palette
        if not palette:
            continue
        vo = sm.vertex_offset
        vc = sm.vertex_count
        if vc <= 0:
            continue
        end = min(vo + vc, n_verts)
        if end <= vo:
            continue
        # Look up each local byte through the palette; out-of-range
        # bytes (or palette entries that point past the skeleton) fall
        # back to 0 — matches the glTF exporter's ``_remap_joints``
        # contract.  We gather with a clipped index then mask the
        # invalid rows back to 0 in a second pass so the OOB rows can't
        # accidentally take palette[0].
        pal = np.asarray(palette, dtype=np.int32)
        local = raw[vo:end]
        valid = (local >= 0) & (local < pal.shape[0])
        clipped = np.where(valid, local, 0)
        gathered = pal[clipped]
        gathered = np.where(valid, gathered, 0)
        gathered[gathered >= skeleton_bone_count] = 0
        gathered[gathered < 0] = 0
        out[vo:end] = gathered

    return out


# ─── Linear blend skinning ───────────────────────────────────────────


def skin_vertices(
    rest_positions: np.ndarray,
    joints: np.ndarray,
    weights: np.ndarray,
    skin_matrices: np.ndarray,
) -> np.ndarray:
    """Apply LBS to ``rest_positions`` using precomputed skin matrices.

    Args:
        rest_positions: ``(N, 3) float32`` — D4-native rest positions.
        joints: ``(N, 4) int32`` — global skeleton bone indices.
        weights: ``(N, 4) float32`` — blend weights (need not sum to 1;
            common in D4 since the source uses ``UNORM`` quads).
        skin_matrices: ``(B, 4, 4) float32`` — ``world_mat @ inv_bind``
            for every bone.

    Returns ``(N, 3) float32`` — skinned positions in D4 space.
    """
    n_verts = rest_positions.shape[0]
    if n_verts == 0:
        return rest_positions

    # Promote to homogeneous coords once.
    ones = np.ones((n_verts, 1), dtype=np.float32)
    rest_h = np.concatenate([rest_positions, ones], axis=1)  # (N, 4)

    out = np.zeros((n_verts, 3), dtype=np.float32)
    # Four influences per vertex; vectorize each separately to keep the
    # working set small (numpy broadcasts the (B, 4, 4) gather and
    # einsum-style multiplies do the rest).
    for j in range(4):
        bone_idx = joints[:, j]                    # (N,)
        w = weights[:, j].astype(np.float32, copy=False)
        if not np.any(w):
            continue
        mats = skin_matrices[bone_idx]             # (N, 4, 4)
        # (N, 4, 4) x (N, 4) -> (N, 4) per-vertex matrix-vector mult.
        transformed = np.einsum("nij,nj->ni", mats, rest_h)
        out += transformed[:, :3] * w[:, np.newaxis]
    return out


# ─── Convenience: build skin matrices ────────────────────────────────


def compute_skin_matrices(
    world_matrices: np.ndarray,
    inv_bind_matrices: np.ndarray,
) -> np.ndarray:
    """Combine world and inv-bind into per-bone skin matrices."""
    return world_matrices @ inv_bind_matrices


# ─── Bone hash -> anim index map ─────────────────────────────────────


def build_bone_hash_to_anim_map(
    decoded: "DecodedAnimation",
) -> dict[int, int]:
    """Index ``DecodedAnimation.bone_animations`` by ``bone_hash``."""
    return {
        ab.bone_hash: i for i, ab in enumerate(decoded.bone_animations)
    }
