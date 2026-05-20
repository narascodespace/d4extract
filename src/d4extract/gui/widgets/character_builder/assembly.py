"""Multi-piece character assembly for the Character Builder viewport.

Every .app file for a given character class+gender embeds the *full*
skeleton with an identical ``Skeleton.template_id`` (BoneData.unk_a3acec8).
Two pieces that share a template id therefore use globally-consistent
JOINTS_0 byte values — bone index 47 in ``barM_P00.app`` is the same
bone as index 47 in ``barM_stor229_HLM.app``. That property is what
makes assembly possible without per-piece bone remapping: we can simply
concatenate the per-piece vertex / index streams under a single
canonical skeleton.

This module is **Qt-free**. It only depends on the parser dataclasses,
which lets it be unit-tested in isolation.

NOTE on bone pruning:
    The exporter's optional bone-pruning pass walks each piece's joint
    palette and drops bones that no submesh references. Pruning relies
    on the joint indices being valid against the skeleton it ships with.
    Once pieces are merged here, the joint values from each contributing
    piece are still valid against the *canonical* skeleton (because all
    contributors share the same template id), but pruning would change
    indices on the canonical skeleton without rewriting the merged
    JOINTS_0 stream — which would break the pieces it just trimmed away.
    Bone pruning must therefore be DISABLED when exporting an assembled
    character. Single-piece exports out of the Model Browser are
    unaffected.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import replace

from d4extract.formats.app_parser import MeshData, Skeleton, Submesh

log = logging.getLogger(__name__)


def merge_pieces(
    pieces: list[MeshData],
    *,
    name: str = "assembled",
) -> tuple[MeshData | None, list[MeshData]]:
    """Merge multiple skinned ``MeshData`` pieces under a shared skeleton.

    Pieces with no skeleton (static / unskinned) are dropped — assembly
    only makes sense for character meshes that share a rest pose. If
    pieces disagree on ``skeleton.template_id``, the largest matching
    group wins and the rest are excluded with a warning so the viewport
    doesn't render meshes whose joint indices don't address the chosen
    skeleton.

    Returns ``(merged, included)`` where ``included`` is the list of
    pieces actually merged in iteration order — callers correlate
    per-piece data (e.g. textures) with merged submesh offsets by
    walking it. Returns ``(None, [])`` if no compatible piece survives
    the filter.
    """
    group, canonical_skel = select_assembly_group(pieces)
    if not group:
        return None, []

    positions: list[tuple[float, float, float]] = []
    normals:   list[tuple[float, float, float, float]] = []
    tangents:  list[tuple[float, float, float, float]] = []
    colors:    list[tuple[float, float, float, float]] = []
    uvs:       list[tuple[float, float]] = []
    joints:    list[tuple[int, int, int, int]] = []
    weights:   list[tuple[float, float, float, float]] = []
    indices:   list[int] = []
    submeshes: list[Submesh] = []
    materials: list = []

    vertex_base = 0
    index_base = 0
    material_base = 0

    for piece in group:
        n_verts = len(piece.positions)

        positions.extend(piece.positions)
        normals.extend(piece.normals)
        tangents.extend(piece.tangents)
        colors.extend(piece.colors)
        uvs.extend(piece.uvs)
        # JOINTS_0 values pass through UNCHANGED — the whole point of
        # the shared template id. Same for weights.
        joints.extend(piece.joints)
        weights.extend(piece.weights)

        # Indices reference the local vertex array; rebase by the
        # accumulated offset so they address the merged stream.
        indices.extend(idx + vertex_base for idx in piece.indices)

        for sm in piece.submeshes:
            submeshes.append(replace(
                sm,
                vertex_offset=sm.vertex_offset + vertex_base,
                index_offset=sm.index_offset + index_base,
                material_index=sm.material_index + material_base,
            ))

        piece_materials = piece.materials or []
        materials.extend(piece_materials)

        vertex_base += n_verts
        index_base += len(piece.indices)
        material_base += len(piece_materials)

    merged = MeshData(
        name=name,
        vertex_count=len(positions),
        index_count=len(indices),
        submesh_count=len(submeshes),
        positions=positions,
        normals=normals,
        tangents=tangents,
        colors=colors,
        uvs=uvs,
        joints=joints,
        weights=weights,
        indices=indices,
        submeshes=submeshes,
        skeleton=canonical_skel,
        materials=materials if materials else None,
    )
    return merged, list(group)


def select_assembly_group(
    pieces: list[MeshData],
) -> tuple[list[MeshData], Skeleton | None]:
    """Pick the skinned pieces that share a skeleton, plus their skeleton.

    Drops unskinned pieces, groups the rest by ``skeleton.template_id``,
    and returns the largest matching group (ties broken by first-seen
    order) together with the skeleton chosen by
    :func:`_pick_canonical_skeleton`. Pieces in a smaller, mismatched
    group are excluded with a warning — their JOINTS_0 values don't
    address the chosen skeleton.

    Returns ``([], None)`` when no skinned piece is present.

    Shared by :func:`merge_pieces` (viewport rendering + animation
    playback) and the Character Builder's per-piece export path so both
    agree on which pieces participate and which skeleton is canonical.
    """
    skinned = [p for p in pieces if p.skeleton is not None]
    if not skinned:
        return [], None

    groups: dict[int, list[MeshData]] = defaultdict(list)
    for piece in skinned:
        groups[piece.skeleton.template_id].append(piece)

    # Largest group wins; ties broken by first-seen order.
    largest_template_id, group = max(
        groups.items(),
        key=lambda kv: (len(kv[1]), -list(groups).index(kv[0])),
    )

    _log_template_summary(skinned, largest_template_id)

    if len(groups) > 1:
        for tid, excluded in groups.items():
            if tid == largest_template_id:
                continue
            for ep in excluded:
                log.warning(
                    "Template mismatch — %s: %d != equipment template %d. "
                    "Excluding from assembly.",
                    ep.name, tid, largest_template_id,
                )

    return list(group), _pick_canonical_skeleton(group)


def _pick_canonical_skeleton(group: list[MeshData]) -> Skeleton | None:
    """Pick the skeleton with the most bones from a same-template group.

    All members of the group carry the full bone hierarchy, so this is
    largely a sanity check — ties are broken by first occurrence.
    """
    best: Skeleton | None = None
    best_count = -1
    for piece in group:
        skel = piece.skeleton
        if skel is None:
            continue
        count = skel.base_bone_count + skel.cloth_bone_count
        if count > best_count:
            best = skel
            best_count = count
    return best


def _log_template_summary(
    pieces: list[MeshData], canonical_template_id: int,
) -> None:
    """Emit a single INFO line listing each piece's template id.

    Important diagnostic: confirms our assumption that face/hair/beard
    pieces share template ids with equipment pieces for the same class.
    """
    parts = []
    for p in pieces:
        tid = p.skeleton.template_id if p.skeleton is not None else 0
        marker = "" if tid == canonical_template_id else "*"
        parts.append(f"{p.name}={tid}{marker}")
    log.info("Template IDs — canonical=%d, %s",
             canonical_template_id, ", ".join(parts))
