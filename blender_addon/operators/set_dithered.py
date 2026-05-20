"""``D4_OT_set_dithered`` — force the DITHERED surface render method."""

from __future__ import annotations

import bpy

from ..core import alpha_fixup


class D4_OT_set_dithered(bpy.types.Operator):
    """Set every material to the DITHERED render method (facial hair stays BLENDED)"""

    bl_idname = "d4.set_dithered"
    bl_label = "Set All -> Dithered"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        # Selected objects' materials, or the whole scene when nothing
        # is selected.
        objects = context.selected_objects or list(context.scene.objects)
        materials: list[bpy.types.Material] = []
        seen: set[str] = set()
        for obj in objects:
            for slot in getattr(obj, "material_slots", ()):
                mat = slot.material
                if mat is not None and mat.name not in seen:
                    seen.add(mat.name)
                    materials.append(mat)

        if not materials:
            self.report({"WARNING"}, "No materials found")
            return {"CANCELLED"}

        try:
            dithered, kept = alpha_fixup.apply_dithered(materials)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            f"{dithered} material(s) -> DITHERED, {kept} kept BLENDED",
        )
        return {"FINISHED"}
