"""``IMPORT_SCENE_OT_d4`` — import a d4extract .glb and tune it."""

from __future__ import annotations

import tempfile
from pathlib import Path

import bpy
from bpy_extras.io_utils import ImportHelper

from ..core import alpha_fixup, material_builder
from ..core import sidecar as sc
from ..core import variant_catalog as vc
from ..preferences import get_prefs


class IMPORT_SCENE_OT_d4(bpy.types.Operator, ImportHelper):
    """Import a Diablo IV model exported by d4extract (.glb)"""

    bl_idname = "import_scene.d4"
    bl_label = "Import D4 glTF"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".glb"
    filter_glob: bpy.props.StringProperty(default="*.glb", options={"HIDDEN"})

    # These default from the scene props but can be overridden per-import.
    auto_setup_materials: bpy.props.BoolProperty(
        name="Auto-setup materials",
        description="Rebuild PBR materials right after import",
        default=True,
    )
    force_dithered: bpy.props.BoolProperty(
        name="Force dithered alpha",
        description="Set imported materials to the DITHERED render method",
        default=True,
    )
    swap_armor_skin: bpy.props.BoolProperty(
        name="Swap armor_skin_mat -> body skin",
        description=(
            "Replace exposed-skin placeholder materials with the body "
            "skin material so they inherit skin-tone changes"
        ),
        default=True,
    )
    disable_bone_shape: bpy.props.BoolProperty(
        name="Disable bone shape",
        description=(
            "Pass disable_bone_shape=True to the glTF importer so bones "
            "display as default octahedrals instead of the importer's "
            "heuristic widgets (Blender 4.4+)"
        ),
        default=True,
    )
    materials_sidecar_override: bpy.props.StringProperty(
        name="Materials sidecar (override)",
        description=(
            "Optional path to a .materials.json sidecar — leave empty "
            "to use the file sitting next to the .glb"
        ),
        subtype="FILE_PATH",
        default="",
    )

    def invoke(self, context, event):
        # Seed the operator toggles from the panel's current settings.
        props = context.scene.d4_props
        self.auto_setup_materials = props.auto_setup_materials
        self.force_dithered = props.force_dithered
        self.swap_armor_skin = props.swap_armor_skin
        self.disable_bone_shape = props.disable_bone_shape
        self.materials_sidecar_override = props.materials_sidecar_override
        return ImportHelper.invoke(self, context, event)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "auto_setup_materials")
        layout.prop(self, "force_dithered")
        layout.prop(self, "swap_armor_skin")
        layout.prop(self, "disable_bone_shape")
        layout.prop(self, "materials_sidecar_override")

    def execute(self, context):
        glb_path = Path(self.filepath)
        if not glb_path.is_file():
            self.report({"ERROR"}, f"File not found: {glb_path}")
            return {"CANCELLED"}

        before = set(bpy.data.objects)
        import_kwargs: dict = {"filepath": str(glb_path)}
        if self.disable_bone_shape:
            # disable_bone_shape was added to the glTF importer in
            # Blender 4.4 — feature-detect rather than assume it exists,
            # so the addon stays usable on older 4.x releases.
            gltf_props = bpy.ops.import_scene.gltf.get_rna_type().properties
            if "disable_bone_shape" in gltf_props.keys():
                import_kwargs["disable_bone_shape"] = True
            else:
                self.report(
                    {"INFO"},
                    "disable_bone_shape is unavailable on this Blender "
                    "version — importing without it",
                )
        try:
            bpy.ops.import_scene.gltf(**import_kwargs)
        except RuntimeError as exc:
            self.report({"ERROR"}, f"glTF import failed: {exc}")
            return {"CANCELLED"}

        new_objects = [o for o in bpy.data.objects if o not in before]
        if not new_objects:
            self.report({"WARNING"}, "Import produced no objects")
            return {"CANCELLED"}

        meshes = [o for o in new_objects if o.type == "MESH"]
        armatures = [o for o in new_objects if o.type == "ARMATURE"]

        # Stamp the sidecar / glb path so the panel and apply operator
        # can find them later. Prefer the armature; fall back to meshes.
        # A non-empty override field wins over the next-to-the-glb file.
        override = (self.materials_sidecar_override or "").strip()
        sidecar = sc.load_sidecar(glb_path, override=override or None)
        searched = (
            Path(override) if override else sc.sidecar_path_for(glb_path)
        )
        side_path = str(searched)
        stamp_targets = armatures or meshes
        for obj in stamp_targets:
            obj[vc.PROP_SIDECAR_PATH] = side_path
            obj[vc.PROP_GLB_PATH] = str(glb_path)

        if armatures:
            context.scene.d4_props.target_armature = armatures[0]

        # Pull the embedded customization-variant images into datablocks
        # named ``variant_<kind>_<id>`` so the dropdowns can swap to them.
        variants_loaded = 0
        if sidecar is not None and sidecar.variants:
            variants_loaded = _load_variant_images(glb_path, sidecar)

        # Replace armor_skin_mat placeholders with the body skin
        # material *before* PBR auto-setup, so the body skin gets one
        # setup pass that every reassigned exposed-skin slot then shares.
        swap_count, swap_reason = 0, None
        if sidecar is not None and self.swap_armor_skin:
            swap_count, swap_reason = material_builder.swap_armor_skin_materials(
                new_objects, sidecar,
            )

        warnings: list[str] = []
        if self.auto_setup_materials:
            if sidecar is None:
                warnings.append(
                    f"no .materials.json sidecar at {searched} — materials "
                    f"not set up. Re-export with the sidecar enabled "
                    f"(it is on by default), or point the override field "
                    f"at the file."
                )
            else:
                for mesh in meshes:
                    warnings.extend(
                        material_builder.setup_object_materials(
                            mesh, sidecar, strip_color0=True,
                        )
                    )
                _apply_backface_culling(context, meshes)

        dithered = kept = 0
        if self.force_dithered:
            dithered, kept = alpha_fixup.apply_dithered(
                _collect_materials(new_objects)
            )

        vc.invalidate()  # variant lists changed — drop the enum cache
        msg = (
            f"Imported {len(meshes)} mesh(es), {len(armatures)} armature(s); "
            f"{variants_loaded} variant image(s) loaded"
        )
        if dithered:
            msg += f"; {dithered} dithered, {kept} kept blended"
        if swap_count > 0:
            msg += f"; {swap_count} armor_skin_mat slot(s) -> body skin"
        self.report({"INFO"}, msg)
        if swap_reason:
            self.report({"INFO"}, swap_reason)
        for w in warnings[:8]:
            self.report({"WARNING"}, w)
        return {"FINISHED"}


def _collect_materials(objects: list[bpy.types.Object]) -> list[bpy.types.Material]:
    """Every unique material across ``objects``' mesh slots."""
    seen: set[str] = set()
    out: list[bpy.types.Material] = []
    for obj in objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is not None and mat.name not in seen:
                seen.add(mat.name)
                out.append(mat)
    return out


def _apply_backface_culling(
    context: bpy.types.Context, meshes: list[bpy.types.Object],
) -> None:
    prefs = get_prefs(context)
    if prefs is None or not prefs.apply_backface_culling:
        return
    for mat in _collect_materials(meshes):
        mat.use_backface_culling = True


def _load_variant_images(glb_path: Path, sidecar: sc.Sidecar) -> int:
    """Extract embedded variant PNGs from the glb into image datablocks.

    Blender's glTF importer skips images no material references — every
    variant texture is exactly that. So the bytes are pulled straight
    out of the glb buffer here and loaded as ``variant_<kind>_<id>``.
    Returns the number of images now available.
    """
    try:
        gltf_json, bin_blob = sc.read_glb(glb_path)
    except (ValueError, OSError):
        return 0

    loaded = 0
    # Only image-swap categories carry embedded textures; parametric
    # (skin HSV) categories have none.
    for cat in sidecar.variants.values():
        if cat.kind != "image_swap":
            continue
        for entry in cat.image_entries:
            if entry.image_index < 0:
                continue  # texture was not embedded — nothing to load
            name = entry.image_name
            if name in bpy.data.images:
                loaded += 1
                continue
            png = sc.image_png_bytes(gltf_json, bin_blob, entry.image_index)
            if png is None:
                continue
            if _png_to_image(png, name) is not None:
                loaded += 1
    return loaded


def _png_to_image(png_bytes: bytes, name: str) -> bpy.types.Image | None:
    """Load raw PNG bytes into a packed Blender image named ``name``."""
    tmp = Path(tempfile.gettempdir()) / f"d4_{name}.png"
    try:
        tmp.write_bytes(png_bytes)
        img = bpy.data.images.load(str(tmp), check_existing=False)
    except (RuntimeError, OSError):
        return None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    img.name = name
    try:
        img.pack()  # embed pixels — the temp file is already gone
    except RuntimeError:
        pass
    return img


def menu_func_import(self, context) -> None:
    """File > Import entry."""
    self.layout.operator(IMPORT_SCENE_OT_d4.bl_idname, text="Diablo IV (d4extract .glb)")
