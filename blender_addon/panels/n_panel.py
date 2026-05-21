"""``D4_PT_main`` — the "D4 Tools" tab in the 3D View N-panel."""

from __future__ import annotations

import bpy


class D4_PT_main(bpy.types.Panel):
    """Import, material tooling and customization variants for D4 models."""

    bl_label = "D4 Tools"
    bl_idname = "D4_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "D4 Tools"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.d4_props

        # ── Import ────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Import", icon="IMPORT")
        box.operator("import_scene.d4", text="Import D4 glTF...", icon="IMPORT")
        box.prop(props, "auto_setup_materials")
        box.prop(props, "force_dithered")
        box.prop(props, "swap_armor_skin")
        box.prop(props, "disable_bone_shape")

        # Collapsible override for a sidecar that does not live next to
        # the .glb.
        header = box.row()
        header.prop(
            props, "show_sidecar_override",
            icon="TRIA_DOWN" if props.show_sidecar_override else "TRIA_RIGHT",
            text="Materials sidecar (override)", emboss=False,
        )
        if props.show_sidecar_override:
            box.prop(props, "materials_sidecar_override", text="")

        # ── Material Tools ────────────────────────────────────────
        box = layout.box()
        box.label(text="Material Tools", icon="MATERIAL")
        box.operator("d4.setup_materials", icon="NODE_MATERIAL")
        box.operator("d4.set_dithered", icon="IMAGE_ALPHA")

        # ── Character Variants ────────────────────────────────────
        box = layout.box()
        box.label(text="Character Variants", icon="OUTLINER_OB_ARMATURE")
        box.prop(props, "target_armature")

        col = box.column(align=True)
        col.prop(props, "variant_skin")
        # variant_material is still defined on the PropertyGroup for the
        # deferred full-material swap, but stays hidden until it ships.
        col.prop(props, "variant_hair_color")

        row = box.row()
        row.scale_y = 1.3
        row.operator("d4.apply_variants", icon="CHECKMARK")
