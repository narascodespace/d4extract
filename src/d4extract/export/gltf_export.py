"""glTF 2.0 / .glb exporter for parsed .app models.

The parser (``d4extract.formats.app_parser``) already converts every
on-disk format into the glTF-friendly type — packed normals/tangents
to float32, half-float UVs to float32, UNORM colors/weights to float32
in [0, 1], JOINTS as uint8 quadruples — so this module is a pure
wiring layer.  It does NOT re-decode or re-convert any attribute; it
only slices, transforms coordinates, packs into a binary buffer, and
emits accessors / bufferViews / primitives.

Per-submesh primitives
======================
Each ``Submesh`` becomes its own glTF primitive.  Vertex data is
sliced as ``positions[vo : vo+vc]`` and indices as
``indices[io : io+ic]``; indices are then rebased to start at 0
within the primitive.  This keeps each primitive self-contained and
matches the engine's per-draw-call boundaries.

Materials
=========
One ``Material`` is emitted per UNIQUE ``submesh.material_index``,
named ``"Material_{idx}"`` — Phase 5 will resolve actual SNO names
through ``ptAppearanceMaterials``.  Multiple primitives that share the
same ``material_index`` reference the same Material entry.

Coordinate system
=================
The parser preserves D4-native coordinates (left-handed, Z-up).  The
exporter applies a coordinate transform per the ``coordinate_transform``
constructor argument; the ``"z_up_to_y_up"`` preset is the same
``(x, y, z) -> (x, z, -y)`` mapping that
``app_parser.convert_to_gltf_coords`` exposes, applied componentwise to
positions, normals, and tangents.

Attribute matrix (out)
======================
    POSITION    VEC3 FLOAT  always
    NORMAL      VEC3 FLOAT  if export_normals  and model.normals
    TANGENT     VEC4 FLOAT  if export_tangents and model.tangents
    TEXCOORD_0  VEC2 FLOAT  if export_uvs      and model.uvs
    TEXCOORD_1  VEC2 FLOAT  if export_uvs      and model.uvs_1
    COLOR_0     VEC4 FLOAT  if export_colors   and model.colors
    JOINTS_0/WEIGHTS_0      skipped (Phase 4 — needs skin + skeleton)

Indices use UINT16 when the per-primitive max index fits, UINT32
otherwise.  UVs pass through unchanged: D4 and glTF both define V=0
at the top of the texture (DirectX convention; see glTF 2.0 spec
§ 3.7.4.1), so no flip is needed.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

log = logging.getLogger(__name__)

from pygltflib import (
    ARRAY_BUFFER,
    ELEMENT_ARRAY_BUFFER,
    FLOAT,
    SCALAR,
    UNSIGNED_BYTE,
    UNSIGNED_INT,
    UNSIGNED_SHORT,
    VEC2,
    VEC3,
    VEC4,
    MAT4,
    Accessor,
    Animation as GltfAnimation,
    AnimationChannel,
    AnimationChannelTarget,
    AnimationSampler,
    Asset,
    Attributes,
    Buffer,
    BufferView,
    GLTF2,
    Image as GltfImage,
    Material,
    Mesh,
    NormalMaterialTexture,
    OcclusionTextureInfo,
    Node,
    PbrMetallicRoughness,
    Primitive,
    Sampler,
    Scene,
    Skin,
    Texture as GltfTexture,
    TextureInfo,
)

if TYPE_CHECKING:
    # Imported for type hints only — keeps the anim_parser import out of
    # the hot path for callers that never export animation.
    from d4extract.formats.anim_parser import DecodedAnimation

from d4extract.formats.app_parser import (
    BoneTransform,
    MeshData,
    Skeleton,
    Submesh,
)
from d4extract.formats.material_parser import (
    Material as D4Material,
    TextureRef,
    materials_to_sidecar,
)
from d4extract.formats.texture_parser import (
    TextureDecodeError,
    combine_metallic_roughness,
    decode_tex,
    image_min_alpha,
    reconstruct_normal_z,
    texture_payload_path,
    to_png_bytes,
)


class GltfExportError(Exception):
    """Raised when glTF export fails validation or writing."""


# Coordinate transform presets: each maps (x, y, z) -> (x', y', z').
# ``z_up_to_y_up`` matches ``app_parser.convert_to_gltf_coords``.
COORDINATE_TRANSFORMS: dict[str, Callable[[float, float, float], tuple[float, float, float]]] = {
    "none": lambda x, y, z: (x, y, z),
    "z_up_to_y_up": lambda x, y, z: (x, z, -y),
    "left_to_right": lambda x, y, z: (-x, y, z),
}


# Companion transforms for bone TRS components.  When the position
# transform is the change of basis ``v' = M v``, a unit quaternion
# ``q = (qx, qy, qz, qw)`` representing rotation ``R`` becomes
# ``q'`` representing ``M R M⁻¹`` — which for our diagonal/permutation
# matrices is just the same component permutation/sign-flip applied to
# the quaternion's vector part.  Scales are non-negative so only the
# axis permutation (no sign flip) applies.
QUATERNION_TRANSFORMS: dict[str, Callable[[tuple[float, float, float, float]], tuple[float, float, float, float]]] = {
    "none":          lambda q: q,
    "z_up_to_y_up":  lambda q: (q[0], q[2], -q[1], q[3]),
    "left_to_right": lambda q: (-q[0], q[1], q[2], q[3]),
}

SCALE_TRANSFORMS: dict[str, Callable[[tuple[float, float, float]], tuple[float, float, float]]] = {
    "none":          lambda s: s,
    "z_up_to_y_up":  lambda s: (s[0], s[2], s[1]),
    "left_to_right": lambda s: s,
}


def _tangent_w_sign_for(
    transform: Callable[[float, float, float], tuple[float, float, float]],
) -> float:
    """Return +1 for a determinant-+1 transform, -1 for a mirror.

    glTF's ``TANGENT.w`` is the bitangent handedness sign used as
    ``B = w · (N × T)``. A coordinate transform that preserves
    orientation (det = +1) leaves ``w`` unchanged; a mirror (det = -1)
    flips it. Computed by probing the transform on the basis vectors so
    the rule generalises to any preset registered in
    :data:`COORDINATE_TRANSFORMS`.
    """
    e1 = transform(1.0, 0.0, 0.0)
    e2 = transform(0.0, 1.0, 0.0)
    e3 = transform(0.0, 0.0, 1.0)
    det = (
        e1[0] * (e2[1] * e3[2] - e2[2] * e3[1])
        - e1[1] * (e2[0] * e3[2] - e2[2] * e3[0])
        + e1[2] * (e2[0] * e3[1] - e2[1] * e3[0])
    )
    return -1.0 if det < 0 else 1.0


def _auto_detect_transform(
    positions: list[tuple[float, float, float]],
) -> str:
    """Guess a coordinate convention from bounding box proportions."""
    if not positions:
        return "none"
    xs = [p[0] for p in positions]
    ys = [p[1] for p in positions]
    zs = [p[2] for p in positions]
    y_span = max(ys) - min(ys)
    z_span = max(zs) - min(zs)
    if z_span > y_span * 1.5 and z_span > 0.1:
        return "z_up_to_y_up"
    return "none"


def _variant_block(variants: "dict | None") -> "dict | None":
    """Convert a discovered-variants dict into the sidecar ``variants`` block.

    Returns ``None`` when no variants were passed, so
    :func:`materials_to_sidecar` omits the block. Imported lazily to
    keep ``variants`` an optional dependency of the export path.
    """
    if not variants:
        return None
    from d4extract.formats.variants import variants_to_sidecar_block
    return variants_to_sidecar_block(variants)


def combined_materials_for_export(
    primary_mesh: MeshData | None = None,
    *,
    extra_meshes: list[tuple[MeshData, set[int] | None]] | None = None,
    skinned_pieces: list[tuple[MeshData, set[int] | None]] | None = None,
    static_extras: list[tuple[MeshData, set[int] | None]] | None = None,
) -> list:
    """The concatenated materials list the sidecar will contain.

    Mirrors the order in which :meth:`GltfExporter.export` and
    :meth:`GltfExporter.export_assembly` build their ``combined`` locals
    when writing the ``.materials.json`` sidecar. Variant discovery must
    run against this same list so each variant block's
    ``applies_to_materials`` indices line up with the sidecar's
    ``materials[]`` array.

    The caller picks exactly ONE shape:

    * **single-mesh export** — pass ``primary_mesh`` (plus optional
      ``extra_meshes``); ``skinned_pieces`` / ``static_extras`` stay
      ``None``. Returns ``primary_mesh``'s materials followed by each
      extra mesh's, in order.
    * **assembly export** — pass ``skinned_pieces`` (plus optional
      ``static_extras``); ``primary_mesh`` / ``extra_meshes`` stay
      ``None``. Returns every piece's materials then every static
      extra's, in ``(*skinned_pieces, *static_extras)`` order.

    A mesh whose ``materials`` is falsy (``None`` / empty) contributes
    nothing — matching what both ``export`` paths already do. An
    all-``None`` call returns ``[]``.

    Raises :class:`ValueError` if both shapes are specified at once.
    """
    single_set = primary_mesh is not None or extra_meshes is not None
    assembly_set = skinned_pieces is not None or static_extras is not None
    if single_set and assembly_set:
        raise ValueError(
            "combined_materials_for_export: pass either the single-mesh "
            "shape (primary_mesh / extra_meshes) or the assembly shape "
            "(skinned_pieces / static_extras), not both."
        )

    combined: list = []
    if assembly_set:
        for mesh, _ in (*(skinned_pieces or ()), *(static_extras or ())):
            if mesh.materials:
                combined.extend(mesh.materials)
    else:
        if primary_mesh is not None and primary_mesh.materials:
            combined.extend(primary_mesh.materials)
        for extra_mesh, _ in extra_meshes or ():
            if extra_mesh.materials:
                combined.extend(extra_mesh.materials)
    return combined


class GltfExporter:
    """Convert ``MeshData`` into a glTF 2.0 binary (.glb)."""

    def __init__(
        self,
        coordinate_transform: str = "none",
        export_normals: bool = True,
        export_tangents: bool = True,
        export_uvs: bool = True,
        export_colors: bool = True,
        export_skin: bool = True,
        prune_bones: bool = False,
        write_materials_sidecar: bool = True,
        texture_dir: Path | None = None,
        embed_textures: bool = False,
        include_cloth: bool = False,
    ):
        if coordinate_transform not in (*COORDINATE_TRANSFORMS, "auto"):
            raise ValueError(
                f"Unknown coordinate_transform: {coordinate_transform!r}. "
                f"Valid: {', '.join([*COORDINATE_TRANSFORMS, 'auto'])}"
            )
        self.coordinate_transform = coordinate_transform
        self.export_normals = export_normals
        self.export_tangents = export_tangents
        self.export_uvs = export_uvs
        self.export_colors = export_colors
        self.export_skin = export_skin
        self.prune_bones = prune_bones
        self.write_materials_sidecar = write_materials_sidecar
        self.texture_dir = Path(texture_dir) if texture_dir else None
        self.embed_textures = embed_textures
        # Drop physics-only cloth submeshes by default — see _build_gltf
        # for the filter. CLI exposes this as ``--include-cloth``.
        self.include_cloth = include_cloth
        # Set after a successful ``export`` call when textures were embedded.
        # CLI uses these for the per-export summary line; per-material counts
        # live on each material's ``extras['d4_embedded_bytes']``.
        self.last_embedded_bytes: int = 0
        self.last_image_count: int = 0
        # Reflect the final exported geometry, not the source mesh totals.
        # When cloth submeshes are filtered these shrink accordingly.
        self.last_vertex_count: int = 0
        self.last_triangle_count: int = 0
        self.last_submesh_count: int = 0
        self.last_skipped_cloth: list[tuple[int, str]] = []  # (submesh_idx, mat_name)

    # ── public API ─────────────────────────────────────────────────

    def export(
        self,
        model: MeshData,
        output_path: Path,
        *,
        submesh_filter: set[int] | None = None,
        extra_meshes: list[tuple[MeshData, set[int] | None]] | None = None,
        animations: list["DecodedAnimation"] | None = None,
        variants: "dict | None" = None,
    ) -> Path:
        """Build and write a .glb file for ``model``.

        ``submesh_filter`` — when set, restrict the exported geometry to
        submeshes whose original index (i.e. position in
        ``model.submeshes`` before any cloth filtering) is in the set.
        ``None`` exports every survivor of the cloth filter, identical
        to the pre-feature behaviour.

        ``extra_meshes`` — additional ``MeshData`` objects to include as
        separate, unskinned mesh nodes in the same scene. Each entry is
        ``(mesh, per_mesh_submesh_filter)``. Used by the Character
        Builder to ship weapons alongside a skinned body without
        forcing them into the skinning pipeline (they sit at the
        scene origin, ready to be re-parented in Blender).

        ``animations`` — decoded ``DecodedAnimation`` objects to embed
        as glTF animations. Each becomes a separate ``Animation`` entry
        targeting the bone nodes built from ``model.skeleton``. Channels
        whose values are constant across all frames are dropped, and
        channels whose ``bone_hash`` doesn't map to a kept bone are
        silently skipped — animations may reference bones absent from a
        pruned skeleton or from a different appearance variant.

        ``variants`` — discovered customization variants
        (``dict[str, list[VariantEntry]]`` from
        :func:`d4extract.formats.variants.discover_variants`). Their
        textures are embedded into the glb (when texture embedding is
        on) and a ``variants`` block is written into the sidecar so the
        Blender addon can populate its Skin / Eyes / Makeup / Material /
        Markings dropdowns. ``None`` omits the block entirely.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        gltf = self._build_gltf(
            model,
            submesh_filter=submesh_filter,
            extra_meshes=extra_meshes,
            animations=animations,
            variants=variants,
        )
        _validate_gltf(gltf)
        gltf.save(str(output_path))
        if self.write_materials_sidecar and model.materials:
            sidecar_path = output_path.with_suffix(".materials.json")
            with open(sidecar_path, "w", encoding="utf-8") as f:
                import json as _json
                # Sidecar concatenates materials across primary + every
                # extra mesh so a downstream Blender importer sees the
                # full roster — same order they appear in the glTF
                # ``materials`` array. ``combined_materials_for_export``
                # is the single source of truth for that ordering — it
                # is also what variant discovery indexes against.
                combined = combined_materials_for_export(
                    primary_mesh=model, extra_meshes=extra_meshes or None,
                )
                _json.dump(
                    materials_to_sidecar(combined, _variant_block(variants)),
                    f, indent=2, ensure_ascii=False,
                )
        return output_path

    def export_assembly(
        self,
        skinned_pieces: list[tuple[MeshData, set[int] | None]],
        canonical_skeleton: Skeleton | None,
        output_path: Path,
        *,
        static_extras: list[tuple[MeshData, set[int] | None]] | None = None,
        animations: list["DecodedAnimation"] | None = None,
        variants: "dict | None" = None,
    ) -> Path:
        """Build and write a .glb where each skinned piece is its own mesh.

        Unlike :meth:`export`, which writes one consolidated ``MeshData``
        as a single glTF mesh node, this emits **one mesh node per
        equipped piece**, all referencing a single shared ``Skin`` built
        from ``canonical_skeleton``. After Blender's glTF import that
        yields one Armature with N child mesh objects — each
        independently toggleable in the Outliner.

        ``skinned_pieces`` — each ``(MeshData, submesh_filter)`` keeps
        its own geometry / materials. Every piece's JOINTS_0 values must
        already address ``canonical_skeleton`` without remapping (the
        Character Builder guarantees this via the shared-template-id
        invariant — see ``assembly.py``).

        ``canonical_skeleton`` — the single skeleton all skinned pieces
        share; drives the lone ``Skin``, inverse bind matrices, bone
        nodes, and animation channels.

        ``static_extras`` — unskinned overlay meshes (weapons). Emitted
        as standalone mesh nodes at the scene root with no skin, exactly
        as :meth:`export`'s ``extra_meshes`` are.

        ``animations`` — decoded clips embedded as glTF animations
        targeting the shared bone nodes; they apply to every piece.

        ``variants`` — discovered customization variants, handled
        exactly as in :meth:`export`: textures embedded into the glb and
        a ``variants`` block written into the sidecar. ``None`` omits it.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        gltf = self._build_assembly_gltf(
            skinned_pieces,
            canonical_skeleton,
            static_extras=static_extras,
            animations=animations,
            variants=variants,
        )
        _validate_gltf(gltf)
        gltf.save(str(output_path))
        if self.write_materials_sidecar:
            # Concatenate materials across every piece + extra in the
            # same order they appear in the glTF ``materials`` array, so
            # a downstream importer sees the full roster.
            # ``combined_materials_for_export`` owns that ordering — the
            # same list variant discovery indexes against.
            combined = combined_materials_for_export(
                skinned_pieces=skinned_pieces, static_extras=static_extras,
            )
            if combined:
                sidecar_path = output_path.with_suffix(".materials.json")
                with open(sidecar_path, "w", encoding="utf-8") as f:
                    import json as _json
                    _json.dump(
                        materials_to_sidecar(
                            combined, _variant_block(variants),
                        ),
                        f, indent=2, ensure_ascii=False,
                    )
        return output_path

    def export_batch(
        self,
        models: list[tuple[MeshData, Path]],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[Path]:
        """Export multiple models; calls ``progress_callback(current, total)`` on each."""
        results: list[Path] = []
        total = len(models)
        for i, (model, path) in enumerate(models):
            results.append(self.export(model, path))
            if progress_callback:
                progress_callback(i + 1, total)
        return results

    # ── internal: build a complete GLTF2 object from MeshData ─────

    @staticmethod
    def _embed_variant_textures(
        texture_pack: "TexturePack | None",
        variants: "dict | None",
    ) -> None:
        """Embed every discovered variant texture into the glb buffer.

        Back-fills each ``VariantEntry.image_index`` in place with the
        glTF ``images`` array index. When textures are not being
        embedded (no ``texture_pack``) — or no variants were discovered —
        this is a no-op and every entry keeps ``image_index = -1``, which
        the Blender addon treats as "texture unavailable, skip the swap".

        Variant images are appended to the same ``images`` list as the
        material textures, so their indices are stable once embedded.
        """
        if not variants or texture_pack is None:
            return
        # ``variants`` maps category -> VariantCategory. Only image-swap
        # categories carry textures; parametric (skin HSV) ones do not.
        for cat in variants.values():
            if getattr(cat, "kind", "image_swap") != "image_swap":
                continue
            for entry in cat.entries:
                idx = texture_pack.embed_variant(
                    entry.texture, entry.variant_image_name,
                )
                if idx is not None:
                    entry.image_index = idx

    def _build_gltf(
        self,
        model: MeshData,
        *,
        submesh_filter: set[int] | None = None,
        extra_meshes: list[tuple[MeshData, set[int] | None]] | None = None,
        animations: list["DecodedAnimation"] | None = None,
        variants: "dict | None" = None,
    ) -> GLTF2:
        transform_key = self.coordinate_transform
        if transform_key == "auto":
            transform_key = _auto_detect_transform(model.positions)
        transform = COORDINATE_TRANSFORMS[transform_key]
        quat_xform = QUATERNION_TRANSFORMS.get(
            transform_key, QUATERNION_TRANSFORMS["none"],
        )
        scale_xform = SCALE_TRANSFORMS.get(
            transform_key, SCALE_TRANSFORMS["none"],
        )

        all_submeshes = self._submeshes_or_fallback(model)

        # Filter physics-only cloth submeshes unless include_cloth=True.
        # A submesh is "cloth-only" when its assigned ``ptAppearanceMaterials``
        # entry has ``snoMaterial=null`` and only ``snoCloth`` set — visible
        # only to D4's cloth simulation. Without filtering they appear as
        # flat-white geometry overlapping the real body in Blender. We drop
        # them upfront so they're absent from materials, primitives, and
        # bone-pruning reachability all at once.
        self.last_skipped_cloth = []
        submeshes: list[Submesh] = []
        kept_orig_indices: list[int] = []
        for orig_idx, sm in enumerate(all_submeshes):
            mat = None
            if model.materials and 0 <= sm.material_index < len(model.materials):
                mat = model.materials[sm.material_index]
            is_cloth = bool(mat) and mat.is_cloth_only
            if is_cloth and not self.include_cloth:
                self.last_skipped_cloth.append((orig_idx, mat.name))
                log.info(
                    "Skipping submesh %d (cloth-only, material: %s)",
                    orig_idx, mat.name,
                )
                continue
            submeshes.append(sm)
            kept_orig_indices.append(orig_idx)

        # GUI-driven "Export Selected" filter. Indices use the original
        # ``enumerate(all_submeshes)`` numbering — same numbering the
        # submesh-list checkboxes report — so applying it after the
        # cloth filter still picks the right rows even when cloth was
        # silently skipped.
        if submesh_filter is not None:
            submeshes = [
                sm for sm, orig_idx in zip(submeshes, kept_orig_indices)
                if orig_idx in submesh_filter
            ]

        # Whether each attribute appears at all in the model
        has_normals  = self.export_normals  and bool(model.normals)
        has_tangents = self.export_tangents and bool(model.tangents)
        has_uv0      = self.export_uvs      and bool(model.uvs)
        has_uv1      = self.export_uvs      and bool(model.uvs_1)
        has_color0   = self.export_colors   and bool(model.colors)

        # Skin: include only when (1) the model has a skeleton, (2) the
        # vertex stream actually carries JOINTS_0/WEIGHTS_0, and (3) the
        # caller did not opt out via export_skin=False.
        skin_active = (
            self.export_skin
            and model.skeleton is not None
            and bool(model.joints)
            and bool(model.weights)
        )
        bone_keep_mask: list[bool] | None = None
        bone_remap: dict[int, int] | None = None
        if skin_active and self.prune_bones:
            bone_keep_mask, bone_remap = _compute_bone_pruning(
                model.skeleton, submeshes,
            )

        # Binary blob and the lists of buffer views/accessors are shared
        # by both the texture pack (image bytes) and per-primitive vertex/
        # index data. They must exist before materials are built so that
        # the texture pack can append PNG bytes during material setup.
        binary = bytearray()
        buffer_views: list[BufferView] = []
        accessors: list[Accessor] = []
        images: list[GltfImage] = []
        textures: list[GltfTexture] = []
        samplers: list[Sampler] = []

        # Texture pack: only when (1) the caller asked for textures,
        # (2) a texture directory is configured, and (3) materials were
        # resolved. Otherwise materials emit factor-only PBR.
        texture_pack: TexturePack | None = None
        if (
            self.embed_textures
            and self.texture_dir is not None
            and model.materials
        ):
            texture_pack = TexturePack(
                texture_dir=self.texture_dir,
                binary=binary, buffer_views=buffer_views,
                images=images, textures=textures, samplers=samplers,
            )

        # Materials: one per unique material_index, in the order seen.
        material_idx_to_slot: dict[int, int] = {}
        materials: list[Material] = []
        for sm in submeshes:
            if sm.material_index not in material_idx_to_slot:
                material_idx_to_slot[sm.material_index] = len(materials)
                materials.append(
                    _make_material(sm.material_index, model, texture_pack),
                )
        if not materials:
            materials.append(_make_material(0, model, texture_pack))

        primitives: list[Primitive] = []

        # COLOR_0 suppression: D4's vertex colors are blend / AO masks,
        # not standard PBR tints. Leaking them as glTF ``COLOR_0``
        # multiplies the base color and produces bright vertex-colour
        # patches on textureless materials (cloth pieces, eyes).
        #
        # Rule: in ``--with-textures`` mode, suppress COLOR_0 on every
        # primitive — even ones whose material failed to bind a base
        # colour texture (they then render as the material's
        # ``baseColorFactor`` instead of as raw vertex colours). When
        # textures are not embedded at all, keep COLOR_0 as the user's
        # crude per-vertex tint.
        suppress_all_colors = texture_pack is not None

        for sm in submeshes:
            slot = material_idx_to_slot[sm.material_index]
            prim = self._build_primitive(
                model=model, submesh=sm, transform=transform,
                binary=binary, buffer_views=buffer_views, accessors=accessors,
                has_normals=has_normals, has_tangents=has_tangents,
                has_uv0=has_uv0, has_uv1=has_uv1, has_color0=has_color0,
                skin_active=skin_active, bone_remap=bone_remap,
                total_bone_count=len(model.skeleton.bones) if model.skeleton else 0,
                suppress_colors=suppress_all_colors,
            )
            prim.material = slot
            primitives.append(prim)

        # Process each extra mesh in the same pipeline (cloth filter →
        # user filter → materials → primitives) but with skinning OFF.
        # Each one becomes its own ``Mesh`` + ``Node`` so they sit in
        # the scene without being attached to the primary's skeleton.
        extra_mesh_objects: list[Mesh] = []
        extra_total_submeshes = 0
        extra_total_vertices = 0
        extra_total_triangles = 0
        for extra_mesh, extra_filter in (extra_meshes or ()):
            ex_all = self._submeshes_or_fallback(extra_mesh)
            ex_submeshes: list[Submesh] = []
            ex_kept_orig: list[int] = []
            for orig_idx, sm in enumerate(ex_all):
                mat = None
                if (
                    extra_mesh.materials
                    and 0 <= sm.material_index < len(extra_mesh.materials)
                ):
                    mat = extra_mesh.materials[sm.material_index]
                is_cloth = bool(mat) and mat.is_cloth_only
                if is_cloth and not self.include_cloth:
                    self.last_skipped_cloth.append((orig_idx, mat.name))
                    continue
                ex_submeshes.append(sm)
                ex_kept_orig.append(orig_idx)
            if extra_filter is not None:
                ex_submeshes = [
                    sm for sm, orig_idx in zip(ex_submeshes, ex_kept_orig)
                    if orig_idx in extra_filter
                ]
            if not ex_submeshes:
                # Nothing survived the filters — emit no Mesh/Node so we
                # don't pollute the scene with empty placeholders.
                continue

            # Per-extra material index → global ``materials`` slot.
            ex_idx_to_slot: dict[int, int] = {}
            for sm in ex_submeshes:
                if sm.material_index not in ex_idx_to_slot:
                    ex_idx_to_slot[sm.material_index] = len(materials)
                    materials.append(_make_material(
                        sm.material_index, extra_mesh, texture_pack,
                    ))

            ex_primitives: list[Primitive] = []
            for sm in ex_submeshes:
                slot = ex_idx_to_slot[sm.material_index]
                prim = self._build_primitive(
                    model=extra_mesh, submesh=sm, transform=transform,
                    binary=binary, buffer_views=buffer_views,
                    accessors=accessors,
                    has_normals=self.export_normals and bool(extra_mesh.normals),
                    has_tangents=self.export_tangents and bool(extra_mesh.tangents),
                    has_uv0=self.export_uvs and bool(extra_mesh.uvs),
                    has_uv1=self.export_uvs and bool(extra_mesh.uvs_1),
                    has_color0=self.export_colors and bool(extra_mesh.colors),
                    skin_active=False,
                    bone_remap=None,
                    total_bone_count=0,
                    suppress_colors=suppress_all_colors,
                )
                prim.material = slot
                ex_primitives.append(prim)

            extra_mesh_objects.append(Mesh(
                name=extra_mesh.name or "extra",
                primitives=ex_primitives,
            ))
            extra_total_submeshes += len(ex_submeshes)
            extra_total_vertices += sum(sm.vertex_count for sm in ex_submeshes)
            extra_total_triangles += sum(sm.index_count for sm in ex_submeshes) // 3

        gltf = GLTF2()
        gltf.asset = Asset(generator="d4extract", version="2.0")
        gltf.materials = materials
        mesh_name = model.name or "model"
        primary_mesh = Mesh(name=mesh_name, primitives=primitives)
        gltf.meshes = [primary_mesh, *extra_mesh_objects]

        # Node layout:
        #   [0]                 primary mesh node (carries the skin if any)
        #   [1 .. N_bones]      bone nodes (only when skin_active)
        #   [N+1 ...]           one node per extra mesh (no skin, mesh=K)
        # Scene roots:
        #   - primary mesh node
        #   - root bone nodes (so the rig is reachable)
        #   - every extra mesh node (each is its own scene root)
        bone_index_to_node: dict[int, int] = {}
        if skin_active:
            mesh_node = Node(mesh=0, name=mesh_name)
            (
                bone_nodes, joint_indices, root_node_indices,
                bone_index_to_node,
            ) = _build_bone_nodes(
                skeleton=model.skeleton,
                base_index=1,
                transform=transform,
                quat_xform=quat_xform,
                scale_xform=scale_xform,
                bone_keep_mask=bone_keep_mask,
            )
            ibm_accessor = _build_ibm_accessor(
                skeleton=model.skeleton,
                bone_keep_mask=bone_keep_mask,
                transform=transform,
                quat_xform=quat_xform,
                scale_xform=scale_xform,
                binary=binary,
                buffer_views=buffer_views, accessors=accessors,
            )
            skin = Skin(
                joints=joint_indices,
                inverseBindMatrices=ibm_accessor,
                skeleton=root_node_indices[0] if root_node_indices else None,
                name=f"{mesh_name}_skin",
            )
            mesh_node.skin = 0
            gltf.skins = [skin]
            nodes: list[Node] = [mesh_node, *bone_nodes]
            scene_roots: list[int] = [0, *root_node_indices]
        else:
            nodes = [Node(mesh=0, name=mesh_name)]
            scene_roots = [0]

        for i, extra_mesh_obj in enumerate(extra_mesh_objects):
            node_idx = len(nodes)
            nodes.append(Node(
                mesh=1 + i,  # mesh slots after the primary
                name=extra_mesh_obj.name,
            ))
            scene_roots.append(node_idx)

        gltf.nodes = nodes
        gltf.scenes = [Scene(nodes=scene_roots, name="Scene")]

        # Animations — only when the primary mesh has a kept skeleton
        # (no skin → no bone nodes → nowhere to target). Channels on
        # bones missing from ``bone_index_to_node`` are silently skipped
        # (animations can reference pruned or appearance-specific bones
        # that aren't in this export).
        if animations and skin_active and bone_index_to_node:
            gltf_animations = self._build_animations(
                animations=animations,
                skeleton=model.skeleton,
                bone_index_to_node=bone_index_to_node,
                transform=transform,
                quat_xform=quat_xform,
                scale_xform=scale_xform,
                binary=binary,
                buffer_views=buffer_views,
                accessors=accessors,
            )
            if gltf_animations:
                gltf.animations = gltf_animations

        # Customization-variant textures are appended after every
        # material texture, so their glTF image indices are stable. This
        # back-fills each VariantEntry.image_index in place; the sidecar
        # writer reads them straight after this returns.
        self._embed_variant_textures(texture_pack, variants)

        gltf.buffers = [Buffer(byteLength=len(binary))]
        gltf.bufferViews = buffer_views
        gltf.accessors = accessors
        if images:
            gltf.images = images
        if textures:
            gltf.textures = textures
        if samplers:
            gltf.samplers = samplers
        gltf.scene = 0
        gltf.set_binary_blob(bytes(binary))

        # Stash texture-embedding stats so the CLI can include them in
        # the per-export summary without re-walking the glTF.
        if texture_pack is not None:
            self.last_embedded_bytes = texture_pack.embedded_bytes
            self.last_image_count = texture_pack.image_count
        else:
            self.last_embedded_bytes = 0
            self.last_image_count = 0

        # Geometry stats reflect the filtered (exported) submeshes, not the
        # raw mesh totals. CLI uses these for the summary line. Extras
        # are folded in so a multi-mesh export reports the full picture.
        self.last_submesh_count = len(submeshes) + extra_total_submeshes
        self.last_vertex_count = (
            sum(sm.vertex_count for sm in submeshes) + extra_total_vertices
        )
        self.last_triangle_count = (
            sum(sm.index_count for sm in submeshes) // 3 + extra_total_triangles
        )

        return gltf

    # ── internal: per-piece assembly build ────────────────────────

    def _build_assembly_gltf(
        self,
        skinned_pieces: list[tuple[MeshData, set[int] | None]],
        canonical_skeleton: Skeleton | None,
        *,
        static_extras: list[tuple[MeshData, set[int] | None]] | None = None,
        animations: list["DecodedAnimation"] | None = None,
        variants: "dict | None" = None,
    ) -> GLTF2:
        """Build a GLTF2 with one mesh node per skinned piece + shared skin.

        Scene graph:
          * one ``Node`` per skinned piece (``mesh=i``, ``skin=0``)
          * the bone-node hierarchy from ``canonical_skeleton``
          * one ``Node`` per static extra (no skin)

        All piece nodes and the bone roots sit at the scene root — the
        same skin-to-bone relationship :meth:`_build_gltf` uses for its
        single skinned mesh, just repeated N times. Blender's glTF
        importer parents every skin-referencing mesh under the Armature
        regardless of glTF node nesting, so the Outliner shows one
        Armature with N child mesh objects.
        """
        transform_key = self.coordinate_transform
        if transform_key == "auto":
            first_positions = (
                skinned_pieces[0][0].positions if skinned_pieces else []
            )
            transform_key = _auto_detect_transform(first_positions)
        transform = COORDINATE_TRANSFORMS[transform_key]
        quat_xform = QUATERNION_TRANSFORMS.get(
            transform_key, QUATERNION_TRANSFORMS["none"],
        )
        scale_xform = SCALE_TRANSFORMS.get(
            transform_key, SCALE_TRANSFORMS["none"],
        )

        self.last_skipped_cloth = []

        binary = bytearray()
        buffer_views: list[BufferView] = []
        accessors: list[Accessor] = []
        images: list[GltfImage] = []
        textures: list[GltfTexture] = []
        samplers: list[Sampler] = []

        # Texture pack: shared across every piece so a texture used by
        # multiple pieces decodes + embeds exactly once.
        any_materials = any(
            m.materials for m, _ in skinned_pieces
        ) or any(
            getattr(m, "materials", None) for m, _ in (static_extras or ())
        )
        texture_pack: TexturePack | None = None
        if (
            self.embed_textures
            and self.texture_dir is not None
            and any_materials
        ):
            texture_pack = TexturePack(
                texture_dir=self.texture_dir,
                binary=binary, buffer_views=buffer_views,
                images=images, textures=textures, samplers=samplers,
            )
        # COLOR_0 suppression — same rule as _build_gltf.
        suppress_all_colors = texture_pack is not None

        # The shared skin is active when a skeleton is present and skin
        # export wasn't disabled. Bone pruning is intentionally NOT
        # applied: each piece's JOINTS_0 addresses the unpruned
        # canonical skeleton (see assembly.py).
        skin_active = (
            self.export_skin
            and canonical_skeleton is not None
            and bool(canonical_skeleton.bones)
        )
        total_bone_count = (
            len(canonical_skeleton.bones) if canonical_skeleton else 0
        )

        materials: list[Material] = []
        stat_subs = stat_verts = stat_tris = 0

        # One Mesh per skinned piece.
        piece_meshes: list[Mesh] = []
        for piece, pfilter in skinned_pieces:
            # A piece only carries skin data when it actually has
            # JOINTS_0/WEIGHTS_0 streams.
            piece_skin = (
                skin_active and bool(piece.joints) and bool(piece.weights)
            )
            result = self._emit_filtered_mesh(
                mesh=piece, submesh_filter=pfilter, transform=transform,
                binary=binary, buffer_views=buffer_views, accessors=accessors,
                materials=materials, texture_pack=texture_pack,
                skin_active=piece_skin, total_bone_count=total_bone_count,
                suppress_colors=suppress_all_colors,
            )
            if result is None:
                continue
            mesh_obj, n_s, n_v, n_t = result
            piece_meshes.append(mesh_obj)
            stat_subs += n_s
            stat_verts += n_v
            stat_tris += n_t

        # One Mesh per static extra (weapons) — never skinned.
        extra_mesh_objects: list[Mesh] = []
        for extra_mesh, extra_filter in (static_extras or ()):
            result = self._emit_filtered_mesh(
                mesh=extra_mesh, submesh_filter=extra_filter,
                transform=transform,
                binary=binary, buffer_views=buffer_views, accessors=accessors,
                materials=materials, texture_pack=texture_pack,
                skin_active=False, total_bone_count=0,
                suppress_colors=suppress_all_colors,
            )
            if result is None:
                continue
            mesh_obj, n_s, n_v, n_t = result
            extra_mesh_objects.append(mesh_obj)
            stat_subs += n_s
            stat_verts += n_v
            stat_tris += n_t

        gltf = GLTF2()
        gltf.asset = Asset(generator="d4extract", version="2.0")
        gltf.materials = materials
        gltf.meshes = [*piece_meshes, *extra_mesh_objects]

        n_pieces = len(piece_meshes)

        # Node layout:
        #   [0 .. n_pieces-1]            skinned piece mesh nodes
        #   [n_pieces .. n_pieces+B-1]   bone nodes (when skin_active)
        #   [...]                        one node per static extra
        # Scene roots: every piece node, the root bone nodes, and every
        # extra node.
        nodes: list[Node] = []
        scene_roots: list[int] = []
        bone_index_to_node: dict[int, int] = {}

        for i, mesh_obj in enumerate(piece_meshes):
            nodes.append(Node(mesh=i, name=mesh_obj.name))
            scene_roots.append(i)

        if skin_active and n_pieces:
            (
                bone_nodes, joint_indices, root_node_indices,
                bone_index_to_node,
            ) = _build_bone_nodes(
                skeleton=canonical_skeleton,
                base_index=n_pieces,
                transform=transform,
                quat_xform=quat_xform,
                scale_xform=scale_xform,
                bone_keep_mask=None,
            )
            ibm_accessor = _build_ibm_accessor(
                skeleton=canonical_skeleton,
                bone_keep_mask=None,
                transform=transform,
                quat_xform=quat_xform,
                scale_xform=scale_xform,
                binary=binary,
                buffer_views=buffer_views, accessors=accessors,
            )
            skin = Skin(
                joints=joint_indices,
                inverseBindMatrices=ibm_accessor,
                skeleton=root_node_indices[0] if root_node_indices else None,
                name="character_skin",
            )
            gltf.skins = [skin]
            # Every piece node references the one shared skin.
            for i in range(n_pieces):
                nodes[i].skin = 0
            nodes.extend(bone_nodes)
            scene_roots.extend(root_node_indices)

        for j, mesh_obj in enumerate(extra_mesh_objects):
            node_idx = len(nodes)
            nodes.append(Node(mesh=n_pieces + j, name=mesh_obj.name))
            scene_roots.append(node_idx)

        gltf.nodes = nodes
        gltf.scenes = [Scene(nodes=scene_roots, name="Scene")]
        gltf.scene = 0

        # Animations target the shared bone nodes, so one set of
        # channels drives every piece at once.
        if animations and skin_active and bone_index_to_node:
            gltf_animations = self._build_animations(
                animations=animations,
                skeleton=canonical_skeleton,
                bone_index_to_node=bone_index_to_node,
                transform=transform,
                quat_xform=quat_xform,
                scale_xform=scale_xform,
                binary=binary,
                buffer_views=buffer_views,
                accessors=accessors,
            )
            if gltf_animations:
                gltf.animations = gltf_animations

        # Append customization-variant textures after the material
        # textures (see _build_gltf for the rationale).
        self._embed_variant_textures(texture_pack, variants)

        gltf.buffers = [Buffer(byteLength=len(binary))]
        gltf.bufferViews = buffer_views
        gltf.accessors = accessors
        if images:
            gltf.images = images
        if textures:
            gltf.textures = textures
        if samplers:
            gltf.samplers = samplers
        gltf.set_binary_blob(bytes(binary))

        if texture_pack is not None:
            self.last_embedded_bytes = texture_pack.embedded_bytes
            self.last_image_count = texture_pack.image_count
        else:
            self.last_embedded_bytes = 0
            self.last_image_count = 0
        self.last_submesh_count = stat_subs
        self.last_vertex_count = stat_verts
        self.last_triangle_count = stat_tris

        return gltf

    def _emit_filtered_mesh(
        self,
        *,
        mesh: MeshData,
        submesh_filter: set[int] | None,
        transform: Callable[[float, float, float], tuple[float, float, float]],
        binary: bytearray,
        buffer_views: list[BufferView],
        accessors: list[Accessor],
        materials: list[Material],
        texture_pack: "TexturePack | None",
        skin_active: bool,
        total_bone_count: int,
        suppress_colors: bool,
    ) -> tuple[Mesh, int, int, int] | None:
        """Build one glTF ``Mesh`` for ``mesh`` (cloth + user filtered).

        Mirrors the per-mesh pipeline ``_build_gltf`` runs for each extra
        mesh: drop cloth-only submeshes, apply the user submesh filter,
        append this mesh's materials to the shared ``materials`` list,
        and build one primitive per surviving submesh. ``skin_active``
        decides whether JOINTS_0/WEIGHTS_0 are emitted.

        Returns ``(mesh_obj, n_submeshes, n_vertices, n_triangles)`` or
        ``None`` when no submesh survives the filters (so the caller
        emits no empty Mesh/Node).
        """
        all_subs = self._submeshes_or_fallback(mesh)
        subs: list[Submesh] = []
        kept_orig: list[int] = []
        for orig_idx, sm in enumerate(all_subs):
            mat = None
            if mesh.materials and 0 <= sm.material_index < len(mesh.materials):
                mat = mesh.materials[sm.material_index]
            if mat is not None and mat.is_cloth_only and not self.include_cloth:
                self.last_skipped_cloth.append((orig_idx, mat.name))
                log.info(
                    "Skipping submesh %d (cloth-only, material: %s)",
                    orig_idx, mat.name,
                )
                continue
            subs.append(sm)
            kept_orig.append(orig_idx)

        if submesh_filter is not None:
            subs = [
                sm for sm, orig_idx in zip(subs, kept_orig)
                if orig_idx in submesh_filter
            ]
        if not subs:
            return None

        # Per-mesh material_index → shared ``materials`` slot.
        idx_to_slot: dict[int, int] = {}
        for sm in subs:
            if sm.material_index not in idx_to_slot:
                idx_to_slot[sm.material_index] = len(materials)
                materials.append(
                    _make_material(sm.material_index, mesh, texture_pack),
                )

        primitives: list[Primitive] = []
        for sm in subs:
            prim = self._build_primitive(
                model=mesh, submesh=sm, transform=transform,
                binary=binary, buffer_views=buffer_views, accessors=accessors,
                has_normals=self.export_normals and bool(mesh.normals),
                has_tangents=self.export_tangents and bool(mesh.tangents),
                has_uv0=self.export_uvs and bool(mesh.uvs),
                has_uv1=self.export_uvs and bool(mesh.uvs_1),
                has_color0=self.export_colors and bool(mesh.colors),
                skin_active=skin_active,
                bone_remap=None,
                total_bone_count=total_bone_count,
                suppress_colors=suppress_colors,
            )
            prim.material = idx_to_slot[sm.material_index]
            primitives.append(prim)

        mesh_obj = Mesh(name=mesh.name or "piece", primitives=primitives)
        n_verts = sum(sm.vertex_count for sm in subs)
        n_tris = sum(sm.index_count for sm in subs) // 3
        return mesh_obj, len(subs), n_verts, n_tris

    # ── helpers ────────────────────────────────────────────────────

    def _submeshes_or_fallback(self, model: MeshData) -> list[Submesh]:
        """Return ``model.submeshes`` or synthesise one covering the whole mesh.

        Synthetic test fixtures don't populate the submesh list — fall
        back to a single submesh referencing all vertices and indices so
        legacy tests continue to exercise a one-primitive layout.
        """
        if model.submeshes:
            return list(model.submeshes)
        if not model.indices:
            return []
        return [Submesh(
            vertex_offset=0,
            vertex_count=len(model.positions),
            index_offset=0,
            index_count=len(model.indices),
            material_index=0,
        )]

    def _build_primitive(
        self,
        *,
        model: MeshData,
        submesh: Submesh,
        transform: Callable[[float, float, float], tuple[float, float, float]],
        binary: bytearray,
        buffer_views: list[BufferView],
        accessors: list[Accessor],
        has_normals: bool,
        has_tangents: bool,
        has_uv0: bool,
        has_uv1: bool,
        has_color0: bool,
        skin_active: bool = False,
        bone_remap: dict[int, int] | None = None,
        total_bone_count: int = 0,
        suppress_colors: bool = False,
    ) -> Primitive:
        vo, vc = submesh.vertex_offset, submesh.vertex_count
        io, ic = submesh.index_offset, submesh.index_count

        positions = model.positions[vo:vo + vc]
        normals   = model.normals[vo:vo + vc]   if has_normals else []
        tangents  = model.tangents[vo:vo + vc]  if has_tangents else []
        uvs       = model.uvs[vo:vo + vc]       if has_uv0 else []
        uvs_1     = model.uvs_1[vo:vo + vc]     if has_uv1 else []
        # COLOR_0 is suppressed when the primitive's material binds a
        # baseColorTexture — see ``_build_gltf`` for rationale.
        colors    = (
            model.colors[vo:vo + vc]
            if has_color0 and not suppress_colors
            else []
        )
        joints    = model.joints[vo:vo + vc]    if skin_active else []
        weights   = model.weights[vo:vo + vc]   if skin_active else []

        indices_local = [i - vo for i in model.indices[io:io + ic]]

        # Apply coordinate transform to direction vectors and positions.
        positions_t = [transform(*p) for p in positions]
        normals_t = (
            [transform(n[0], n[1], n[2]) for n in normals]
            if normals else []
        )
        # Tangent: parser's 4-tuple is (Tx, Ty, Tz, sign), where ``sign``
        # is the bitangent handedness (±1) used by glTF as
        # ``B = sign * cross(N, T)``. Earlier code hardcoded sign=+1.0,
        # which inverts the bitangent for the ~35% of D4 vertices stored
        # with sign=-1; under normal-mapped shading that flips the
        # apparent surface normal on those vertices and produces the
        # view-dependent "flickering" / one-sided face artifacts seen
        # most clearly on skinned characters in Blender.
        #
        # Pass-through is correct for any **det = +1** position transform
        # (rotations preserve cross products, so handedness sign
        # unchanged). For a det = -1 mirror transform the bitangent must
        # flip sign — ``_tangent_w_sign_for`` returns -1 in that case so
        # the fix degrades cleanly if a mirroring preset is added later.
        tangent_w_sign = _tangent_w_sign_for(transform)

        def _tangent4(t: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
            x, y, z = transform(t[0], t[1], t[2])
            length_sq = x * x + y * y + z * z
            # glTF spec requires w to be exactly ±1; clamp the parser's
            # value defensively in case it ever leaks something else.
            w = -1.0 if t[3] < 0.0 else 1.0
            w *= tangent_w_sign
            if length_sq < 1e-12:
                # Zero-length tangents in the source are usually unused
                # degenerate slots; pick an arbitrary unit direction so
                # the validator doesn't flag "not of unit length: 0.0".
                return (1.0, 0.0, 0.0, w)
            return (x, y, z, w)

        tangents_t = [_tangent4(t) for t in tangents] if tangents else []

        attrs = Attributes()

        # POSITION (always)
        if positions_t:
            min_p = [min(p[i] for p in positions_t) for i in range(3)]
            max_p = [max(p[i] for p in positions_t) for i in range(3)]
        else:
            min_p, max_p = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        pos_data = b"".join(struct.pack("<3f", *p) for p in positions_t)
        attrs.POSITION = _push_accessor(
            binary, buffer_views, accessors,
            pos_data, FLOAT, len(positions_t), VEC3,
            target=ARRAY_BUFFER, min_=min_p, max_=max_p,
        )

        # NORMAL
        if normals_t:
            n_data = b"".join(struct.pack("<3f", *n) for n in normals_t)
            attrs.NORMAL = _push_accessor(
                binary, buffer_views, accessors,
                n_data, FLOAT, len(normals_t), VEC3,
                target=ARRAY_BUFFER,
            )

        # TANGENT (VEC4 with w handedness)
        if tangents_t:
            t_data = b"".join(struct.pack("<4f", *t) for t in tangents_t)
            attrs.TANGENT = _push_accessor(
                binary, buffer_views, accessors,
                t_data, FLOAT, len(tangents_t), VEC4,
                target=ARRAY_BUFFER,
            )

        # TEXCOORD_0 — pass through verbatim. D4 stores UVs in DirectX
        # convention (V=0 at the top of the texture) and glTF 2.0
        # § 3.7.4.1 specifies the same convention ("UV origin is the
        # upper-left corner of a texture"). An earlier version of this
        # code applied ``v = 1.0 - v`` here under the (incorrect)
        # assumption that glTF used GL-style V-up convention; that
        # produced exports that were vertically flipped relative to
        # D4Analyzer ground truth, verified empirically against vertex
        # 0 of Goatman_BossTrophy (raw V=0.6309 → flipped V=0.3691).
        # Do NOT reintroduce a flip — both U and V pass through.
        if uvs:
            uv_data = b"".join(struct.pack("<2f", u, v) for (u, v) in uvs)
            attrs.TEXCOORD_0 = _push_accessor(
                binary, buffer_views, accessors,
                uv_data, FLOAT, len(uvs), VEC2,
                target=ARRAY_BUFFER,
            )

        # TEXCOORD_1 — same convention as TEXCOORD_0; pass through.
        if uvs_1:
            uv1_data = b"".join(struct.pack("<2f", u, v) for (u, v) in uvs_1)
            attrs.TEXCOORD_1 = _push_accessor(
                binary, buffer_views, accessors,
                uv1_data, FLOAT, len(uvs_1), VEC2,
                target=ARRAY_BUFFER,
            )

        # COLOR_0
        if colors:
            c_data = b"".join(struct.pack("<4f", *c) for c in colors)
            attrs.COLOR_0 = _push_accessor(
                binary, buffer_views, accessors,
                c_data, FLOAT, len(colors), VEC4,
                target=ARRAY_BUFFER,
            )

        # JOINTS_0 + WEIGHTS_0 — only when the model has a skin and
        # the vertex stream carries blend indices/weights.
        if skin_active and joints and weights:
            palette = submesh.bone_palette
            remapped = _remap_joints(
                joints, palette, bone_remap, total_bone_count,
            )

            # Mask out unused joint slots: when ``weights[i] == 0`` the
            # joint index in slot ``i`` doesn't contribute to skinning
            # but the source bytes often hold a stale palette entry
            # (e.g. ``weights=(1, 0, 0, 0), joints=(52, 8, 74, 99)``).
            # Khronos ``gltf_validator`` flags every such pair as
            # *"Joints accessor element at index N is used with zero
            # weight but has non-zero value"* — tens of thousands of
            # warnings on a typical character mesh. Zeroing those
            # slots makes the JSON match what's actually used at draw
            # time. The original raw indices stay intact in
            # ``model.joints`` (parser data) so debugging tools can
            # still see them.
            masked = [
                tuple(jc if wc != 0.0 else 0 for jc, wc in zip(j, w))
                for j, w in zip(remapped, weights)
            ]

            joint_max = max((max(j) for j in masked), default=0)
            # u8 fits when every global index is < 256 AND the active
            # bone count fits in a byte; otherwise emit u16x4.
            use_u16 = joint_max > 0xFF
            if use_u16:
                j_data = b"".join(struct.pack("<4H", *j) for j in masked)
                attrs.JOINTS_0 = _push_accessor(
                    binary, buffer_views, accessors,
                    j_data, UNSIGNED_SHORT, len(masked), VEC4,
                    target=ARRAY_BUFFER,
                )
            else:
                j_data = b"".join(struct.pack("<4B", *j) for j in masked)
                attrs.JOINTS_0 = _push_accessor(
                    binary, buffer_views, accessors,
                    j_data, UNSIGNED_BYTE, len(masked), VEC4,
                    target=ARRAY_BUFFER,
                )
            # WEIGHTS_0 as float4 in [0, 1]; the parser already decoded
            # the UNORM4 from on-disk bytes.
            w_data = b"".join(struct.pack("<4f", *w) for w in weights)
            attrs.WEIGHTS_0 = _push_accessor(
                binary, buffer_views, accessors,
                w_data, FLOAT, len(weights), VEC4,
                target=ARRAY_BUFFER,
            )

        # Indices
        max_idx = max(indices_local) if indices_local else 0
        use_u32 = max_idx > 0xFFFE
        idx_ctype = UNSIGNED_INT if use_u32 else UNSIGNED_SHORT
        idx_fmt = "<I" if use_u32 else "<H"
        # Pad to 4-byte alignment before index data so the bufferView
        # offset stays valid for both u16 and u32.
        if len(binary) % 4:
            binary += b"\x00" * (4 - len(binary) % 4)
        idx_data = b"".join(struct.pack(idx_fmt, i) for i in indices_local)
        idx_acc_idx = _push_accessor(
            binary, buffer_views, accessors,
            idx_data, idx_ctype, len(indices_local), SCALAR,
            target=ELEMENT_ARRAY_BUFFER,
            min_=[min(indices_local)] if indices_local else [0],
            max_=[max_idx] if indices_local else [0],
        )

        prim = Primitive(attributes=attrs, indices=idx_acc_idx)
        return prim

    # ── animation helpers ──────────────────────────────────────────

    def _build_animations(
        self,
        *,
        animations: list["DecodedAnimation"],
        skeleton: Skeleton,
        bone_index_to_node: dict[int, int],
        transform: Callable[[float, float, float], tuple[float, float, float]],
        quat_xform: Callable[[tuple[float, float, float, float]], tuple[float, float, float, float]],
        scale_xform: Callable[[tuple[float, float, float]], tuple[float, float, float]],
        binary: bytearray,
        buffer_views: list[BufferView],
        accessors: list[Accessor],
    ) -> list[GltfAnimation]:
        """Pack each ``DecodedAnimation`` as a glTF ``Animation`` entry.

        Per animation:
          * Build one shared SCALAR FLOAT timestamp accessor
            ``[i / fps for i in range(frame_count)]``.
          * For each bone whose ``bone_hash`` resolves to a kept node,
            convert per-frame T/R/S into glTF space using the same
            transforms applied to the rest-pose bone TRS, and emit one
            Accessor + Sampler + Channel per non-static channel.

        Animations whose every channel is static (same value every frame
        for every bone) end up with no channels — those still produce a
        named, channel-less ``Animation`` entry so the user can confirm
        the animation was found, just inert.

        A ``DecodedAnimation`` flagged ``force_static_channels`` (the
        synthetic rest-pose animation) opts out of the rest-equality
        skip: every mapped bone gets translation/rotation/scale channels
        regardless of whether the values match rest, so Blender surfaces
        a usable "snap to bind" Action rather than an empty one.
        """
        # Bone-hash → original bone index. Built once and reused across
        # every animation passed in.
        hash_to_bone_index: dict[int, int] = {
            b.name_hash: i for i, b in enumerate(skeleton.bones)
        }

        # Coordinate-transform matrices. Every registered preset is a
        # pure axis permutation + sign flip, hence linear: probing the
        # scalar callable on the basis vectors gives a matrix ``M`` with
        # ``M[j] == transform(e_j)``, and ``arr @ M`` then reproduces the
        # transform exactly for a whole ``(n, dim)`` array in one matmul
        # — no per-frame Python call. The scalar callables stay in use
        # for the O(bones) rest-pose path via ``_trs_for_export``.
        m_pos = np.array(
            [transform(1.0, 0.0, 0.0), transform(0.0, 1.0, 0.0),
             transform(0.0, 0.0, 1.0)],
            dtype=np.float32,
        )
        m_scale = np.array(
            [scale_xform((1.0, 0.0, 0.0)), scale_xform((0.0, 1.0, 0.0)),
             scale_xform((0.0, 0.0, 1.0))],
            dtype=np.float32,
        )
        m_quat = np.array(
            [quat_xform((1.0, 0.0, 0.0, 0.0)),
             quat_xform((0.0, 1.0, 0.0, 0.0)),
             quat_xform((0.0, 0.0, 1.0, 0.0)),
             quat_xform((0.0, 0.0, 0.0, 1.0))],
            dtype=np.float32,
        )

        gltf_animations: list[GltfAnimation] = []
        skipped_static = 0
        for decoded in animations:
            n_frames = decoded.frame_count
            if n_frames <= 0:
                continue
            fps = decoded.frame_rate or 30.0
            # The synthetic rest-pose animation sets this so every bone
            # gets all three channels even when they equal rest;
            # otherwise _channel_differs_from_rest prunes them all and
            # the Action is empty / unusable in Blender.
            force_channels = getattr(decoded, "force_static_channels", False)

            channels: list[AnimationChannel] = []
            samplers: list[AnimationSampler] = []
            # Lazy: the timestamp accessor only gets pushed into the
            # binary blob the first time a channel actually needs it.
            # Without this, animations whose every curve is static (all
            # frames identical, common for unsupported-format curves
            # that fell back to rest pose) would still leak per-anim
            # timestamp bytes into the .glb. The lazy push also keeps
            # the binary clean for animations we end up dropping.
            ts_accessor: int | None = None

            def _ensure_timestamps() -> int:
                nonlocal ts_accessor
                if ts_accessor is not None:
                    return ts_accessor
                # float64 division then a float32 cast inside
                # _pack_f32 — bit-identical to the old per-frame
                # struct.pack("<f", i / fps).
                timestamps = np.arange(n_frames, dtype=np.float64) / fps
                ts_data = _pack_f32(timestamps)
                # ``bytearray.extend`` mutates in place — we can't use
                # ``binary += …`` here because Python's scope analysis
                # promotes any augmented-assignment target to a local
                # variable, which would shadow the method parameter and
                # raise UnboundLocalError on the read above.
                if len(binary) % 4:
                    binary.extend(b"\x00" * (4 - len(binary) % 4))
                ts_accessor = _push_accessor(
                    binary, buffer_views, accessors,
                    ts_data, FLOAT, n_frames, SCALAR,
                    target=None,
                    min_=[0.0], max_=[float(timestamps[-1])],
                )
                return ts_accessor

            def _add_channel(
                node_idx: int, path: str, output_accessor: int,
            ) -> None:
                sampler_idx = len(samplers)
                samplers.append(AnimationSampler(
                    input=_ensure_timestamps(),
                    output=output_accessor,
                    interpolation="LINEAR",
                ))
                channels.append(AnimationChannel(
                    sampler=sampler_idx,
                    target=AnimationChannelTarget(node=node_idx, path=path),
                ))

            for bone_anim in decoded.bone_animations:
                bone_idx = hash_to_bone_index.get(bone_anim.bone_hash)
                if bone_idx is None:
                    continue
                node_idx = bone_index_to_node.get(bone_idx)
                if node_idx is None:
                    # Bone was pruned out of the skeleton — no node to
                    # target. Skip silently; an animation curve for a
                    # pruned bone is meaningless.
                    continue

                # The bone's rest TRS in glTF space — same conversion
                # the bone NODE got via ``_trs_for_export``. Used as
                # the "should we emit this channel?" reference: a
                # constant animated value that differs from rest still
                # needs a channel, otherwise the bone silently snaps
                # back to its rest pose in Blender (the visible bug
                # for npc_crow's wings/feathers — count=1 baselines
                # held them in a "ready" pose that vanished on export).
                bone = skeleton.bones[bone_idx]
                rest_t, rest_q, rest_s = _trs_for_export(
                    bone.local_trs, transform, quat_xform, scale_xform,
                )
                rest_q = _normalise_quat(rest_q)

                # TRANSLATION — one matmul applies the axis swap to
                # every frame; _pack_f32 serialises the whole array with
                # no per-frame struct.pack call.
                t_xformed = bone_anim.translations @ m_pos
                if force_channels or _channel_differs_from_rest(t_xformed, rest_t):
                    if len(binary) % 4:
                        binary.extend(b"\x00" * (4 - len(binary) % 4))
                    t_acc = _push_accessor(
                        binary, buffer_views, accessors,
                        _pack_f32(t_xformed), FLOAT, t_xformed.shape[0], VEC3,
                        target=None,
                    )
                    _add_channel(node_idx, "translation", t_acc)

                # ROTATION — m_quat permutes/sign-flips the vector part;
                # the result is renormalised so a float32 round-trip
                # can't leave a component outside glTF's [-1, 1] range.
                r_xformed = _normalise_quat_array(bone_anim.rotations @ m_quat)
                if force_channels or _channel_differs_from_rest(r_xformed, rest_q):
                    if len(binary) % 4:
                        binary.extend(b"\x00" * (4 - len(binary) % 4))
                    r_acc = _push_accessor(
                        binary, buffer_views, accessors,
                        _pack_f32(r_xformed), FLOAT, r_xformed.shape[0], VEC4,
                        target=None,
                    )
                    _add_channel(node_idx, "rotation", r_acc)

                # SCALE
                s_xformed = bone_anim.scales @ m_scale
                if force_channels or _channel_differs_from_rest(s_xformed, rest_s):
                    if len(binary) % 4:
                        binary.extend(b"\x00" * (4 - len(binary) % 4))
                    s_acc = _push_accessor(
                        binary, buffer_views, accessors,
                        _pack_f32(s_xformed), FLOAT, s_xformed.shape[0], VEC3,
                        target=None,
                    )
                    _add_channel(node_idx, "scale", s_acc)

            if not channels:
                # glTF 2.0 §3.6.2: ``animation.channels`` and
                # ``animation.samplers`` both require ``minItems: 1``.
                # An Animation with empty arrays makes Blender bail
                # with "couldn't parse gltf, check that the file is
                # valid" — drop the entry entirely. Common cause: every
                # decoded curve was constant (rest-pose fallback for
                # bones with unsupported curve formats), so every
                # channel got static-skipped above.
                skipped_static += 1
                continue

            # Name: include the permutation index when there's any
            # ambiguity (multiple permutations from the same source) —
            # easier to spot in Blender's NLA editor.
            base_name = decoded.name or "animation"
            if decoded.permutation_index:
                anim_name = f"{base_name}_p{decoded.permutation_index}"
            else:
                anim_name = base_name
            gltf_animations.append(GltfAnimation(
                name=anim_name,
                channels=channels,
                samplers=samplers,
            ))

        if skipped_static:
            log.info(
                "Skipped %d animation(s) with no varying channels "
                "(every bone curve was static / rest-pose fallback)",
                skipped_static,
            )

        return gltf_animations


# ─── Skin helpers ────────────────────────────────────────────────────

def _compute_bone_pruning(
    skeleton: Skeleton, submeshes: list[Submesh],
) -> tuple[list[bool], dict[int, int]]:
    """Build the active-bone mask + remap table for ``--prune-bones``.

    Active = (every bone referenced from a submesh's ``bone_palette``)
    UNION (every ancestor on the chain to a root).  Walking ancestors
    keeps parent-child transforms valid even when the parent itself
    isn't directly weighted.

    Returns ``(keep_mask, remap)`` where ``keep_mask[i]`` is True when
    bone ``i`` survives, and ``remap[old_global_index] -> new_compact_index``
    keeps storage-order so the hierarchy stays valid (a parent's
    compact index is always smaller than its child's).
    """
    weighted: set[int] = set()
    for sm in submeshes:
        for g in sm.bone_palette:
            if 0 <= g < len(skeleton.bones):
                weighted.add(g)
    active = set(weighted)
    for w in weighted:
        cur = w
        while True:
            parent = skeleton.bones[cur].parent_index
            if parent < 0 or parent >= len(skeleton.bones):
                break
            if parent in active:
                break
            active.add(parent)
            cur = parent
    keep_mask = [i in active for i in range(len(skeleton.bones))]
    remap: dict[int, int] = {}
    for old in sorted(active):
        remap[old] = len(remap)
    return keep_mask, remap


def _trs_for_export(
    trs: BoneTransform,
    transform: Callable[[float, float, float], tuple[float, float, float]],
    quat_xform: Callable[[tuple[float, float, float, float]], tuple[float, float, float, float]],
    scale_xform: Callable[[tuple[float, float, float]], tuple[float, float, float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float, float], tuple[float, float, float]]:
    """Apply the configured axis swap to one bone TRS.

    ``transform`` flips the position component (and is used for vertex
    positions).  ``quat_xform`` and ``scale_xform`` are the matching
    quaternion / scale companions registered above.

    The quaternion is **renormalised after the axis swap** so each
    component lands in ``[-1, 1]`` and the magnitude is exactly 1.
    Without this, float32 round-trip from D4's payload bytes can
    leave a component slightly above 1 (e.g. ``1.000000238...``),
    which Khronos ``gltf_validator`` flags as
    ``"rotation/N: Value … is out of range"``. The axis swap itself
    is sign-flip + permutation — it preserves magnitude exactly —
    but the input quat may already be off-unit because the source
    is float32.
    """
    t = transform(*trs.wp)
    q = quat_xform(trs.q)
    q = _normalise_quat(q)
    s = scale_xform(trs.scale)
    return t, q, s


def _normalise_quat(
    q: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Force a quaternion to unit length; identity on near-zero input."""
    qx, qy, qz, qw = q
    mag = (qx * qx + qy * qy + qz * qz + qw * qw) ** 0.5
    if mag < 1e-9:
        return (0.0, 0.0, 0.0, 1.0)
    inv = 1.0 / mag
    return (qx * inv, qy * inv, qz * inv, qw * inv)


def _normalise_quat_array(quats: np.ndarray) -> np.ndarray:
    """Vectorised :func:`_normalise_quat` — unit-normalise each row.

    Rows whose magnitude is below ``1e-9`` collapse to the identity
    quaternion ``(0, 0, 0, 1)``, exactly as the scalar version does;
    every other row is divided by its own length so the animation
    output stays inside glTF's required ``[-1, 1]`` component range.
    """
    quats = np.asarray(quats, dtype=np.float32)
    mag = np.sqrt((quats * quats).sum(axis=1))
    out = np.empty_like(quats)
    safe = mag > 1e-9
    out[safe] = quats[safe] / mag[safe][:, None]
    out[~safe] = (0.0, 0.0, 0.0, 1.0)
    return out


def _pack_f32(arr: np.ndarray) -> bytes:
    """Serialise a numeric array as tightly-packed little-endian float32.

    The one allocation that replaces the old per-frame ``struct.pack``
    loop: a contiguous ``<f4`` view of ``arr`` (casting if needed) is
    handed straight to ``tobytes()``. Callers feed already-float32
    arrays, so the cast is usually free.
    """
    return np.ascontiguousarray(arr, dtype="<f4").tobytes()


def _values_vary(
    values: list[tuple[float, ...]], *, eps: float = 1e-6,
) -> bool:
    """Whether any per-frame value differs from the first by more than ``eps``.

    Used to decide whether an animation channel is worth emitting:
    constant channels just inflate the .glb without doing anything the
    rest pose doesn't already cover. Empty / single-frame inputs count
    as static (one value can't vary).
    """
    if len(values) < 2:
        return False
    first = values[0]
    for v in values[1:]:
        for a, b in zip(first, v):
            if abs(a - b) > eps:
                return True
    return False


def _channel_differs_from_rest(
    values: np.ndarray,
    rest: tuple[float, ...] | np.ndarray,
    *, eps: float = 1e-6,
) -> bool:
    """Whether any frame differs from the bone's rest value by > ``eps``.

    The right "should we emit this channel?" predicate. Decoder curves
    with ``count == 1`` return a constant ``baseline`` repeated across
    every frame — that baseline can be either equal to the bone's rest
    pose (chest "Neutral" idle) OR a custom absolute that overrides rest
    (dogLarge loading-screen pose; complex bird/character bones
    positioned at a "ready" stance). Using ``_values_vary`` would skip
    both cases as static; that's wrong for the override case because the
    bone would silently revert to rest in glTF rather than holding the
    override value the engine intends. Comparing against ``rest``
    instead emits the channel iff the bone actually needs the override.

    Vectorised: ``values`` is the ``(n_frames, dim)`` channel array and
    ``rest`` the ``(dim,)`` rest value; the whole frames x components
    comparison is one ``np.any`` over a broadcast difference.
    """
    values = np.asarray(values, dtype=np.float32)
    if values.shape[0] == 0:
        return False
    rest = np.asarray(rest, dtype=np.float32)
    return bool(np.any(np.abs(values - rest) > eps))


def _build_bone_nodes(
    *,
    skeleton: Skeleton,
    base_index: int,
    transform: Callable[[float, float, float], tuple[float, float, float]],
    quat_xform: Callable[[tuple[float, float, float, float]], tuple[float, float, float, float]],
    scale_xform: Callable[[tuple[float, float, float]], tuple[float, float, float]],
    bone_keep_mask: list[bool] | None,
) -> tuple[list[Node], list[int], list[int], dict[int, int]]:
    """Emit one ``Node`` per kept bone, populated with TRS + child links.

    ``base_index`` is the glTF node index that the FIRST bone will get
    (typically 1, after the mesh node at 0).  Returns
    ``(bone_nodes, joint_indices, root_indices, new_index_of)``:

      * ``bone_nodes`` — list ordered by surviving-bone storage order;
        each node's ``children`` list contains glTF node indices.
      * ``joint_indices`` — node indices used to populate ``Skin.joints``
        (one per surviving bone, in compact order).
      * ``root_indices`` — bone-node indices whose parent is missing from
        the active set (or -1).  These are added to the scene root so
        the rig is reachable.
      * ``new_index_of`` — original bone index → glTF node index. Used
        by the animation builder to wire bone-hash channels to the
        right ``Node``.
    """
    keep = bone_keep_mask if bone_keep_mask is not None else [True] * len(skeleton.bones)
    # Map original bone index -> glTF node index.
    new_index_of: dict[int, int] = {}
    bone_nodes: list[Node] = []
    for orig_i, b in enumerate(skeleton.bones):
        if not keep[orig_i]:
            continue
        node_idx = base_index + len(bone_nodes)
        new_index_of[orig_i] = node_idx
        t, q, s = _trs_for_export(
            b.local_trs, transform, quat_xform, scale_xform,
        )
        bone_nodes.append(Node(
            name=b.name,
            translation=list(t),
            rotation=list(q),
            scale=list(s),
        ))

    root_indices: list[int] = []
    for orig_i, b in enumerate(skeleton.bones):
        if orig_i not in new_index_of:
            continue
        my_idx = new_index_of[orig_i]
        parent = b.parent_index
        if parent < 0 or parent not in new_index_of:
            root_indices.append(my_idx)
            continue
        parent_node = bone_nodes[new_index_of[parent] - base_index]
        if parent_node.children is None:
            parent_node.children = []
        parent_node.children.append(my_idx)

    joint_indices = [new_index_of[i] for i in sorted(new_index_of)]
    return bone_nodes, joint_indices, root_indices, new_index_of


def _build_ibm_accessor(
    *,
    skeleton: Skeleton,
    bone_keep_mask: list[bool] | None,
    transform: Callable[[float, float, float], tuple[float, float, float]],
    quat_xform: Callable[[tuple[float, float, float, float]], tuple[float, float, float, float]],
    scale_xform: Callable[[tuple[float, float, float]], tuple[float, float, float]],
    binary: bytearray,
    buffer_views: list[BufferView],
    accessors: list[Accessor],
) -> int:
    """Append one MAT4 per surviving bone as the inverse bind matrix.

    The on-disk ``transformSkinningInv`` is *already* the inverse — we
    must compose it as a TRS matrix and write it without inverting.
    """
    keep = bone_keep_mask if bone_keep_mask is not None else [True] * len(skeleton.bones)
    # Pad to 4-byte alignment before float MAT4 data.
    if len(binary) % 4:
        binary += b"\x00" * (4 - len(binary) % 4)
    blob = bytearray()
    count = 0
    for i, b in enumerate(skeleton.bones):
        if not keep[i]:
            continue
        t, q, s = _trs_for_export(
            b.inv_bind_trs, transform, quat_xform, scale_xform,
        )
        m = _compose_trs_matrix(t, q, s)
        # glTF MAT4 is column-major float32.
        for col in range(4):
            for row in range(4):
                blob += struct.pack("<f", m[row][col])
        count += 1
    return _push_accessor(
        binary, buffer_views, accessors,
        bytes(blob), FLOAT, count, MAT4, target=None,
    )


def _compose_trs_matrix(
    t: tuple[float, float, float],
    q: tuple[float, float, float, float],
    s: tuple[float, float, float],
) -> list[list[float]]:
    """Build a 4x4 row-major matrix from translation/quaternion/scale."""
    qx, qy, qz, qw = q
    # Standard quaternion → 3x3 rotation matrix.
    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz
    r00 = 1.0 - 2.0 * (yy + zz)
    r01 = 2.0 * (xy - wz)
    r02 = 2.0 * (xz + wy)
    r10 = 2.0 * (xy + wz)
    r11 = 1.0 - 2.0 * (xx + zz)
    r12 = 2.0 * (yz - wx)
    r20 = 2.0 * (xz - wy)
    r21 = 2.0 * (yz + wx)
    r22 = 1.0 - 2.0 * (xx + yy)
    sx, sy, sz = s
    return [
        [r00 * sx, r01 * sy, r02 * sz, t[0]],
        [r10 * sx, r11 * sy, r12 * sz, t[1]],
        [r20 * sx, r21 * sy, r22 * sz, t[2]],
        [0.0,      0.0,      0.0,      1.0],
    ]


def _remap_joints(
    joints: list[tuple[int, int, int, int]],
    palette: tuple[int, ...],
    bone_remap: dict[int, int] | None,
    total_bone_count: int,
) -> list[tuple[int, int, int, int]]:
    """Remap raw JOINTS_0 byte values to glTF joint indices.

    Two stages:
      1. ``palette[byte] -> global bone index`` (per-segment palette
         lookup).  When ``palette`` is empty, treat the byte as already
         a global index — that is the path Azmodan currently takes
         until its primary parse path is fixed.
      2. When ``bone_remap`` is set (``--prune-bones``), apply
         ``global -> compact`` to land on the trimmed joints list.

    Out-of-range values fall back to ``0`` rather than crashing — a
    weight of 0 typically accompanies an unused slot, so misrouting a
    spurious byte to bone 0 is harmless in practice.
    """
    out: list[tuple[int, int, int, int]] = []
    n_pal = len(palette)
    for j in joints:
        new = []
        for b in j:
            if n_pal:
                gi = palette[b] if 0 <= b < n_pal else 0
            else:
                gi = b if b < total_bone_count else 0
            if bone_remap is not None:
                gi = bone_remap.get(gi, 0)
            new.append(gi)
        out.append((new[0], new[1], new[2], new[3]))
    return out


def _push_accessor(
    binary: bytearray,
    buffer_views: list[BufferView],
    accessors: list[Accessor],
    data: bytes,
    component_type: int,
    count: int,
    type_: str,
    *,
    target: int | None = None,
    min_: list[float] | None = None,
    max_: list[float] | None = None,
) -> int:
    """Append ``data`` to ``binary`` and emit a bufferView + accessor.

    Returns the index of the new accessor.
    """
    offset = len(binary)
    binary += data
    bv_idx = len(buffer_views)
    buffer_views.append(BufferView(
        buffer=0, byteOffset=offset, byteLength=len(data), target=target,
    ))
    acc_idx = len(accessors)
    accessors.append(Accessor(
        bufferView=bv_idx, byteOffset=0,
        componentType=component_type, count=count, type=type_,
        min=min_, max=max_,
    ))
    return acc_idx


class TexturePack:
    """Embeds decoded .tex pixels as glTF Images and binds them to materials.

    A single pack is shared across all materials in one export so common
    textures (e.g. the Goatman body color used by both Body_mat and
    fur_mat) decode and embed exactly once. Decoded PIL images are
    cached by payload path; the resulting glTF ``Texture`` index is
    cached by ``(role, sno_id)`` (or by the rough+metal SNO pair for
    the combined metallicRoughness texture) so duplicate materials
    point at the same image.

    Embedded image bytes go into the same binary buffer that holds
    vertex/index data — every buffer view stays inside ``buffer 0``.
    Bytes are 4-byte aligned before each PNG to keep
    bufferView.byteOffset valid for any future readers.
    """

    def __init__(
        self,
        texture_dir: Path,
        binary: bytearray,
        buffer_views: list[BufferView],
        images: list[GltfImage],
        textures: list[GltfTexture],
        samplers: list[Sampler],
    ):
        self.texture_dir = Path(texture_dir)
        self.binary = binary
        self.buffer_views = buffer_views
        self.images = images
        self.textures = textures
        self.samplers = samplers

        # Default sampler — linear filtering, repeat wrap. glTF clients
        # treat ``sampler=None`` as "use default" but emitting an
        # explicit one keeps the wrap mode predictable across viewers.
        if not self.samplers:
            # pygltflib's Sampler dataclass doesn't accept ``name`` (the
            # glTF spec allows it but the binding leaves it off).
            self.samplers.append(Sampler(
                magFilter=9729,           # LINEAR
                minFilter=9987,           # LINEAR_MIPMAP_LINEAR
                wrapS=10497, wrapT=10497, # REPEAT
            ))
        self._sampler_idx = 0

        # Caches keyed differently for plain vs combined textures
        self._tex_index_cache: dict[tuple, int] = {}
        self._pil_cache: dict[Path, "object | None"] = {}
        self.embedded_bytes = 0
        self.image_count = 0
        # Track per-material byte counts for the CLI summary.
        self._current_material_bytes = 0

    # ── public per-material API ───────────────────────────────────

    def begin_material(self) -> None:
        self._current_material_bytes = 0

    def end_material(self) -> int:
        n = self._current_material_bytes
        self._current_material_bytes = 0
        return n

    def texture_for_role(
        self,
        role: str,
        textures: list[TextureRef],
    ) -> int | None:
        """Embed (and cache) the texture for one PBR slot. Returns glTF index.

        Returns ``None`` if the role is missing from this material's
        texture list, the .tex payload isn't on disk, or decode fails —
        all three cases the exporter falls back to factor-only PBR for
        that channel.
        """
        ref = _first_ref_with_role(textures, role)
        if ref is None:
            return None
        return self._get_or_create_simple(ref, role)

    def base_color_with_alpha(
        self,
        textures: list[TextureRef],
    ) -> tuple[int | None, int | None]:
        """Embed BASE_COLOR and report its alpha-channel minimum.

        Returns ``(texture_index, min_alpha)`` where:

        - ``texture_index`` is the glTF Texture index, or ``None`` if
          the material has no BASE_COLOR or decoding failed.
        - ``min_alpha`` is the minimum value across the decoded image's
          alpha channel (0–255), or ``None`` for images that don't
          carry alpha (RGB/L mode). The exporter uses this to choose
          between ``alphaMode="OPAQUE"`` (no alpha or min_alpha == 255)
          and ``alphaMode="MASK"`` (min_alpha < 255 → punch-through).
        """
        ref = _first_ref_with_role(textures, "BASE_COLOR")
        if ref is None:
            return None, None
        idx = self._get_or_create_simple(ref, "BASE_COLOR")
        if idx is None:
            return None, None
        # The PIL image is cached by payload path during decode — pick
        # it back up here so we don't pay decode cost twice.
        path = texture_payload_path(self.texture_dir, ref.path)
        img = self._pil_cache.get(path)
        if img is None:
            return idx, None
        return idx, image_min_alpha(img)

    def metallic_roughness_texture(
        self,
        textures: list[TextureRef],
    ) -> int | None:
        """Pack ROUGHNESS (G) and METALLIC (B) into one glTF texture."""
        rough = _first_ref_with_role(textures, "ROUGHNESS")
        metal = _first_ref_with_role(textures, "METALLIC")
        if rough is None and metal is None:
            return None

        cache_key = ("MR", rough.sno_id if rough else 0, metal.sno_id if metal else 0)
        cached = self._tex_index_cache.get(cache_key)
        if cached is not None:
            return cached

        rough_img = self._decode(rough) if rough is not None else None
        metal_img = self._decode(metal) if metal is not None else None
        if rough_img is None and metal_img is None:
            return None

        try:
            combined = combine_metallic_roughness(
                roughness_img=rough_img, metallic_img=metal_img,
            )
        except TextureDecodeError as exc:
            log.warning("MR combine failed: %s", exc)
            return None

        png = to_png_bytes(combined)
        name = f"MR_{(rough.sno_id if rough else 0)}_{(metal.sno_id if metal else 0)}"
        idx = self._embed_png(png, name)
        self._tex_index_cache[cache_key] = idx
        return idx

    def hair_base_color(
        self, d4_mat: D4Material,
    ) -> tuple[int | None, str]:
        """Build a glTF baseColorTexture for a Hero_Hair material.

        Hero_Hair textures come in two flavours that look identical at
        the slot level (both are slot 19 BASE_COLOR) but encode their
        data very differently. The decoded **image mode** is the clean
        structural signal that distinguishes them — the codec choice
        the artist made directly reflects which sub-pattern they were
        authoring for:

        * **Head hair** (``mode == "RGBA"`` — sourced from BC1 fmt 10
          / 46 / 47): dense overlapping hair-card geometry in multiple
          layers. The RGB is near-white (intended to be tinted by the
          runtime ``HairColor`` preset uniform) and alpha is the
          per-strand mask. **Caller should use ``alphaMode="MASK"``** —
          BLEND on this style produces severe depth-sort artefacts in
          Blender / Eevee / most game engines because layered alpha
          geometry needs back-to-front sorting that real-time
          renderers can't do reliably.
        * **Facial hair / stubble** (``mode == "L"`` — sourced from
          BC4 fmt 41): single sparse layer over skin. Channel value
          is per-pixel density (0 = skin, 1 = hair). RGB comes from
          the material's ``base_color_factor``. Smooth alpha gradient
          is preserved end-to-end. **Caller should use
          ``alphaMode="BLEND"``** — single layer means no depth-sort
          issue, and BLEND keeps the soft skin-blending edges.

        Returns ``(texture_index, sub_case)`` where ``sub_case`` is:

        * ``"rgba"`` — head-hair-style; caller picks MASK
        * ``"tinted_mask"`` — facial-hair-style; caller picks BLEND
        * ``"none"`` — no BASE_COLOR or decode failed; caller skips
          texture binding entirely
        """
        from PIL import Image
        ref = _first_ref_with_role(d4_mat.textures, "BASE_COLOR")
        if ref is None:
            return None, "none"
        # Cache per material + texture so two materials sharing the
        # same source texture reuse the embedded result.
        cache_key = ("HAIR_BC", ref.sno_id, d4_mat.sno_id)
        cached = self._tex_index_cache.get(cache_key)
        if cached is not None:
            sub_case = self._tex_index_cache.get(("HAIR_KIND", ref.sno_id, d4_mat.sno_id))
            return cached, sub_case or "rgba"

        img = self._decode(ref)
        if img is None:
            return None, "none"

        if img.mode == "L":
            # Single-channel density mask — wrap with the material's
            # tint as RGB. Using base_color_factor.rgb (clamped to
            # 0..1, scaled to 0..255) gives stubble its dark hue or
            # whatever the artist authored.
            r, g, b, _ = d4_mat.base_color_factor
            tint = (
                int(round(max(0.0, min(1.0, r)) * 255)),
                int(round(max(0.0, min(1.0, g)) * 255)),
                int(round(max(0.0, min(1.0, b)) * 255)),
            )
            rgb = Image.new("RGB", img.size, tint)
            r_ch, g_ch, b_ch = rgb.split()
            img_rgba = Image.merge("RGBA", (r_ch, g_ch, b_ch, img))
            png = to_png_bytes(img_rgba)
            name = f"HAIR_BC_{ref.sno_id}_tinted_{d4_mat.sno_id}"
            sub_case = "tinted_mask"
        else:
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            png = to_png_bytes(img)
            name = f"HAIR_BC_{ref.sno_id}"
            sub_case = "rgba"

        idx = self._embed_png(png, name)
        self._tex_index_cache[cache_key] = idx
        self._tex_index_cache[("HAIR_KIND", ref.sno_id, d4_mat.sno_id)] = sub_case
        return idx, sub_case

    def embed_variant(self, ref: TextureRef, image_name: str) -> int | None:
        """Embed a customization-variant texture; return its glTF image index.

        Unlike the per-role helpers this is keyed by the caller-supplied
        ``image_name`` (``variant_<kind>_<id>``) — the stable string the
        Blender addon looks the datablock up by. Returns the index into
        the glTF ``images`` array, or ``None`` when the .tex payload is
        missing or decoding failed (the caller then records
        ``image_index = -1`` and the addon skips that swap).

        NORMAL-role variants get the same Z-reconstruction the per-role
        path applies; every other role embeds the decoded image as-is.
        """
        img = self._decode(ref)
        if img is None:
            return None
        if ref.role == "NORMAL":
            try:
                img = reconstruct_normal_z(img)
            except Exception as exc:  # numpy or Pillow error
                log.warning(
                    "variant normal reconstruction failed for %s: %s",
                    ref.path, exc,
                )
        png = to_png_bytes(img)
        self._embed_png(png, image_name)
        # _embed_png appends one entry to self.images — its index is the
        # last slot. (It also appends a Texture, unused by variants.)
        return len(self.images) - 1

    # ── internal helpers ──────────────────────────────────────────

    def _get_or_create_simple(self, ref: TextureRef, role: str) -> int | None:
        cache_key = (role, ref.sno_id)
        cached = self._tex_index_cache.get(cache_key)
        if cached is not None:
            return cached

        img = self._decode(ref)
        if img is None:
            return None

        # Roles that need post-processing
        if role == "NORMAL":
            try:
                img = reconstruct_normal_z(img)
            except Exception as exc:  # numpy or Pillow error
                log.warning("normal reconstruction failed for %s: %s",
                            ref.path, exc)
                # fall back to the raw BC5 RGB — still gives X/Y, Z=0

        png = to_png_bytes(img)
        name = f"{role}_{ref.sno_id}"
        idx = self._embed_png(png, name)
        self._tex_index_cache[cache_key] = idx
        return idx

    def _decode(self, ref: TextureRef):
        """Decode a TextureRef into a PIL image, cached by payload path."""
        path = texture_payload_path(self.texture_dir, ref.path)
        if path in self._pil_cache:
            return self._pil_cache[path]
        try:
            img = decode_tex(path, ref.width, ref.height, ref.format)
        except TextureDecodeError as exc:
            log.warning("texture decode skipped: %s", exc)
            img = None
        self._pil_cache[path] = img
        return img

    def _embed_png(self, png: bytes, name: str) -> int:
        # 4-byte align before appending PNG bytes.
        if len(self.binary) % 4:
            self.binary += b"\x00" * (4 - len(self.binary) % 4)
        bv_offset = len(self.binary)
        self.binary += png

        bv_idx = len(self.buffer_views)
        self.buffer_views.append(BufferView(
            buffer=0, byteOffset=bv_offset, byteLength=len(png),
        ))
        img_idx = len(self.images)
        self.images.append(GltfImage(
            bufferView=bv_idx, mimeType="image/png", name=name,
        ))
        tex_idx = len(self.textures)
        self.textures.append(GltfTexture(
            source=img_idx, sampler=self._sampler_idx, name=name,
        ))
        self.embedded_bytes += len(png)
        self._current_material_bytes += len(png)
        self.image_count += 1
        return tex_idx


def _first_ref_with_role(
    textures: list[TextureRef], role: str,
) -> TextureRef | None:
    for t in textures:
        if t.role == role:
            return t
    return None


# Shader-map keywords that imply *some* form of alpha handling. The full
# semantics are shader-specific (alphatest vs blend vs ghost vs decal),
# but for first-pass glTF export we promote anything that mentions one
# of these to ``alphaMode = BLEND`` so the result at least samples
# transparency rather than producing flat opaque blobs. Specific shaders
# that need richer handling (Hero_Hair below) get their own branch.
_ALPHA_HINT_KEYWORDS = (
    "hair", "transparent", "alpha", "blend", "foliage", "decal",
)


def _shader_alpha_keyword(shader_map: str | None) -> str | None:
    """Return the first matching alpha-hint keyword, or ``None``."""
    if not shader_map:
        return None
    lc = shader_map.lower()
    for kw in _ALPHA_HINT_KEYWORDS:
        if kw in lc:
            return kw
    return None


def _is_hero_hair(shader_map: str | None) -> bool:
    return bool(shader_map) and shader_map.lower() == "hero_hair"


def _make_material(
    material_index: int, model: MeshData,
    texture_pack: "TexturePack | None" = None,
) -> Material:
    """Build the glTF ``Material`` for one ``submesh.material_index``.

    Two paths:

    1. ``model.materials`` is populated (CLI invoked with ``--d4data-path``):
       use the resolved :class:`D4Material` to set name, baseColorFactor
       (from the BASE_COLOR texture's ``rgbavalAvgColor``), emissiveFactor
       (clamped from ``ptRunTimeMaterialValues``), and an ``extras``
       dict carrying the full texture inventory so a downstream Blender
       importer can resolve the actual `.tex` files when CASC extraction
       is wired up.

    2. ``model.materials`` is ``None`` or the index is out of range
       (no d4data on hand): fall back to the historical
       ``Material_<idx>`` stub with neutral PBR factors.

    Indices that overflow the resolved roster (defensive — should not
    happen because the engine guarantees ``nMaterialIndex <
    len(ptAppearanceMaterials)``) also fall back to the stub.
    """
    d4_mat: D4Material | None = None
    if model.materials and 0 <= material_index < len(model.materials):
        d4_mat = model.materials[material_index]

    if d4_mat is None:
        return Material(
            name=f"Material_{material_index}",
            pbrMetallicRoughness=PbrMetallicRoughness(
                baseColorFactor=[0.8, 0.8, 0.8, 1.0],
                metallicFactor=0.0,
                roughnessFactor=0.5,
            ),
        )

    extras: dict = {
        "d4_sno_id": d4_mat.sno_id,
        "d4_shader_map": d4_mat.shader_map,
        "d4_textures": [
            {
                "role": t.role,
                "slot": t.slot,
                "sno_id": t.sno_id,
                "path": t.path,
                "width": t.width,
                "height": t.height,
                "format": t.format,
                "avg_rgba": list(t.avg_rgba),
            }
            for t in d4_mat.textures
        ],
    }
    pbr = PbrMetallicRoughness(
        baseColorFactor=list(d4_mat.base_color_factor),
        metallicFactor=d4_mat.metallic_factor,
        roughnessFactor=d4_mat.roughness_factor,
    )
    mat = Material(
        name=d4_mat.name,
        pbrMetallicRoughness=pbr,
        emissiveFactor=list(d4_mat.emissive_factor),
        extras=extras,
    )

    if texture_pack is not None:
        texture_pack.begin_material()

        is_hair = _is_hero_hair(d4_mat.shader_map)
        alpha_kw = _shader_alpha_keyword(d4_mat.shader_map)

        if is_hair:
            # Hero_Hair has two visually-distinct sub-patterns. The
            # decoded image mode tells us which one we're dealing with:
            #   - RGBA  → head hair (dense layered cards) → MASK
            #   - L     → facial hair (single sparse layer) → BLEND
            # See ``TexturePack.hair_base_color`` for the rationale.
            bc_idx, hair_kind = texture_pack.hair_base_color(d4_mat)
            if bc_idx is not None:
                pbr.baseColorTexture = TextureInfo(index=bc_idx)
                pbr.baseColorFactor = [1.0, 1.0, 1.0, 1.0]
            mat.doubleSided = True
            if hair_kind == "rgba":
                # Head hair: layered alpha geometry → MASK. cutoff=0.5
                # is the spec default; for BC1 punch-through alpha
                # (which is what fmt 10/46/47 encode) the choice of
                # cutoff in (0, 1) is moot because every pixel is
                # already 0 or 255 — verified empirically on
                # rogF_H07: cutoffs 0.3/0.4/0.5/0.6 all keep the same
                # 2 113 795 pixels. Use 0.5 for predictability.
                mat.alphaMode = "MASK"
                mat.alphaCutoff = 0.5
                extras["d4_alpha_treatment"] = "hero_hair_layered"
            elif hair_kind == "tinted_mask":
                # Facial hair / stubble: single layer with smooth alpha
                # gradient → BLEND so the soft edges blend naturally
                # into the underlying skin shader. No depth-sort issue
                # because the geometry is one card on top of skin, not
                # multiple overlapping cards.
                mat.alphaMode = "BLEND"
                mat.alphaCutoff = None
                extras["d4_alpha_treatment"] = "hero_hair_blend"
            else:
                # No BASE_COLOR; fall back to BLEND on the factor
                # alone (rare — would only fire if hair material
                # somehow lost its texture reference).
                mat.alphaMode = "BLEND"
                mat.alphaCutoff = None
                extras["d4_alpha_treatment"] = "hero_hair_no_texture"
            # Hair is non-metallic with moderate roughness; the JSON
            # values (1.0/1.0) reflect raw shader defaults that the
            # Hero_Hair runtime reinterprets — override here so glTF
            # PBR shaders give a sensible result.
            pbr.metallicFactor = 0.0
            pbr.roughnessFactor = 0.6
            # Note slot 96/97 — anisotropic mask & strand noise for
            # the runtime hair shader. glTF PBR can't represent them.
            for role in ("MASK_PRIMARY", "NOISE_PROCEDURAL"):
                if any(t.role == role for t in d4_mat.textures):
                    log.info(
                        "Hero_Hair %r: dropping %s texture (no glTF "
                        "PBR equivalent for hair anisotropy)",
                        d4_mat.name, role,
                    )
        else:
            bc_idx, bc_min_alpha = texture_pack.base_color_with_alpha(
                d4_mat.textures,
            )
            if bc_idx is not None:
                pbr.baseColorTexture = TextureInfo(index=bc_idx)
                pbr.baseColorFactor = [1.0, 1.0, 1.0, 1.0]
                # Non-hair material whose BASE_COLOR carries genuine
                # alpha (D4 BC1 punch-through used for tatter holes,
                # eyelashes, fringes, etc.). Use ``alphaMode=BLEND``
                # rather than ``MASK`` here.
                #
                # Why: the previous ``MASK + alphaCutoff=0.5`` setting
                # is well-known to interact badly with mip-mapped alpha
                # textures in Blender Eevee — the trilinear sampler
                # blends alpha across mip levels and the blended values
                # cross the cutoff differently with camera distance,
                # producing view-dependent flicker exactly at the
                # edges of every transparent region (the lilith body
                # tatter / UV-island-edge artifact). See Khronos's
                # AlphaBlendModeTest sample README for the failure-mode
                # description and lisyarus's "Exploring ways to mipmap
                # alpha-tested textures" for the deep dive.
                #
                # BLEND uses smooth alpha (no cutoff threshold) so
                # mipmap blending only produces softer edges with
                # distance instead of toggling pixels on and off. For
                # single-layer character cloth this is closer to D4's
                # in-engine alpha-to-coverage rendering anyway. Hair
                # cards stay on the dedicated MASK path above (crisp
                # punch-through is the right look for layered hair).
                if bc_min_alpha is not None and bc_min_alpha < 255:
                    mat.alphaMode = "BLEND"
                    mat.alphaCutoff = None
                    mat.doubleSided = True
                    extras["d4_alpha_treatment"] = "blend_punch_through_alpha"
                extras["d4_base_color_min_alpha"] = bc_min_alpha

            # Other shader_maps that mention transparency-ish keywords
            # — promote to BLEND (overriding the alpha-detection's
            # MASK if present). This is a coarse first-pass treatment;
            # specific handling per shader can come later. ``alphaCutoff``
            # is ignored under BLEND but we clear it for cleanliness so
            # the emitted JSON doesn't carry stale MASK metadata.
            if alpha_kw is not None:
                mat.alphaMode = "BLEND"
                mat.alphaCutoff = None
                mat.doubleSided = True
                extras["d4_alpha_treatment"] = f"shader_keyword:{alpha_kw}"
                log.info(
                    "Material %r uses shader_map=%r (matched %r) — "
                    "applying alphaMode=BLEND as default treatment",
                    d4_mat.name, d4_mat.shader_map, alpha_kw,
                )

        # Wire normal/AO/emissive uniformly for hair and non-hair —
        # those slot semantics still apply where present.
        n_idx = texture_pack.texture_for_role("NORMAL", d4_mat.textures)
        if n_idx is not None:
            mat.normalTexture = NormalMaterialTexture(index=n_idx, scale=1.0)

        if not is_hair:
            mr_idx = texture_pack.metallic_roughness_texture(d4_mat.textures)
            if mr_idx is not None:
                pbr.metallicRoughnessTexture = TextureInfo(index=mr_idx)

            ao_idx = texture_pack.texture_for_role("AO", d4_mat.textures)
            if ao_idx is not None:
                mat.occlusionTexture = OcclusionTextureInfo(
                    index=ao_idx, strength=1.0,
                )

            em_idx = texture_pack.texture_for_role("EMISSIVE", d4_mat.textures)
            if em_idx is not None:
                mat.emissiveTexture = TextureInfo(index=em_idx)

        extras["d4_embedded_bytes"] = texture_pack.end_material()

    return mat


def _validate_gltf(gltf: GLTF2) -> None:
    """Run internal validation checks on the built glTF before saving."""
    blob = gltf.binary_blob()
    if blob is None:
        raise GltfExportError("Binary blob is None")

    buf_len = gltf.buffers[0].byteLength
    if len(blob) < buf_len:
        raise GltfExportError(
            f"Binary blob ({len(blob)}) shorter than declared buffer ({buf_len})"
        )

    for i, bv in enumerate(gltf.bufferViews):
        end = bv.byteOffset + bv.byteLength
        if end > len(blob):
            raise GltfExportError(
                f"BufferView[{i}] extends past buffer: offset={bv.byteOffset}, "
                f"length={bv.byteLength}, buffer_size={len(blob)}"
            )

    for i, acc in enumerate(gltf.accessors):
        bv = gltf.bufferViews[acc.bufferView]
        elem_size = _component_byte_size(acc.componentType) * _type_element_count(acc.type)
        acc_end = acc.byteOffset + acc.count * elem_size
        if acc_end > bv.byteLength:
            raise GltfExportError(
                f"Accessor[{i}] extends past BufferView[{acc.bufferView}]: "
                f"accessor needs {acc_end}, bufferView has {bv.byteLength}"
            )

    if gltf.accessors:
        pos_acc = gltf.accessors[0]
        if pos_acc.max is None or pos_acc.min is None:
            raise GltfExportError("First (POSITION) accessor missing min/max bounds")

    n_mats = len(gltf.materials)
    for mesh in gltf.meshes:
        for prim in mesh.primitives:
            if prim.material is not None and prim.material >= n_mats:
                raise GltfExportError(
                    f"Primitive references material {prim.material}, "
                    f"but only {n_mats} materials exist"
                )


def _component_byte_size(component_type: int) -> int:
    return {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}[component_type]


def _type_element_count(accessor_type: str) -> int:
    return {
        "SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
        "MAT2": 4, "MAT3": 9, "MAT4": 16,
    }[accessor_type]


def validate_glb_external(glb_path: Path) -> tuple[bool, str]:
    """Run the Khronos glTF validator on a .glb file if available.

    Returns ``(passed, message)`` — ``passed`` is True when the validator
    is missing or reports no errors.

    Uses ``-o`` to capture the JSON validation report on stdout (rather
    than the validator's default of writing a sibling ``.report.json``
    file alongside every .glb). Parses error/warning/info counts and
    surfaces the first few errors verbatim so the user knows why a
    file failed.
    """
    import json
    import shutil
    import subprocess

    validator = shutil.which("gltf_validator")
    if validator is None:
        return True, (
            "Khronos glTF validator not found. "
            "Install the binary from "
            "https://github.com/KhronosGroup/glTF-Validator/releases "
            "and ensure ``gltf_validator`` is on PATH."
        )

    try:
        result = subprocess.run(
            [validator, "-o", str(glb_path)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "Validator timed out"
    except OSError as e:
        return False, f"Could not run validator: {e}"

    # The validator prints the JSON report to stdout (with -o); the
    # exit code is non-zero if any errors were found.
    report = None
    if result.stdout:
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            report = None

    issues = (report or {}).get("issues", {}) if report else {}
    n_err = issues.get("numErrors", 0)
    n_warn = issues.get("numWarnings", 0)
    n_info = issues.get("numInfos", 0)
    n_hint = issues.get("numHints", 0)

    summary = (
        f"errors={n_err}  warnings={n_warn}  infos={n_info}  hints={n_hint}"
    )
    if result.returncode == 0 and n_err == 0:
        return True, f"PASS: {summary}"

    # Surface the first few error messages so users know what tripped
    # the validator without re-running it manually.
    msgs = issues.get("messages", []) if report else []
    err_lines: list[str] = []
    for m in msgs:
        if m.get("severity") != 0:  # 0=error, 1=warn, 2=info, 3=hint
            continue
        err_lines.append(
            f"    {m.get('pointer', '?')}: {m.get('message', '')}"
        )
        if len(err_lines) >= 5:
            break
    if len(msgs) > 5 and n_err > 5:
        err_lines.append(f"    ... and {n_err - len(err_lines)} more errors")

    detail = "\n".join(err_lines) if err_lines else result.stderr.strip()
    return False, f"FAIL: {summary}\n{detail}"
