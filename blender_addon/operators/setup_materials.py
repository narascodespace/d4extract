"""``D4_OT_setup_materials`` — rebuild PBR graphs from the sidecar."""

from __future__ import annotations

import bpy

from ..core import material_builder
from ..core import sidecar as sc
from ..core import variant_catalog as vc
from ..preferences import get_prefs


def _resolve_sidecar_for_object(obj: bpy.types.Object) -> sc.Sidecar | None:
    """Find the sidecar for ``obj`` — its own stamp, or an ancestor's."""
    node: bpy.types.Object | None = obj
    while node is not None:
        side_path = node.get(vc.PROP_SIDECAR_PATH)
        if side_path:
            return sc.load_sidecar_file(side_path)
        node = node.parent
    return None


class D4_OT_setup_materials(bpy.types.Operator):
    """Rebuild PBR materials for the selected objects from their d4extract sidecar"""

    bl_idname = "d4.setup_materials"
    bl_label = "Setup PBR Materials"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        objects = context.selected_objects or list(context.scene.objects)
        meshes = [o for o in objects if o.type == "MESH"]
        if not meshes:
            self.report({"WARNING"}, "No mesh objects to set up")
            return {"CANCELLED"}

        prefs = get_prefs(context)
        backface = bool(prefs.apply_backface_culling) if prefs else False

        done = 0
        warnings: list[str] = []
        for mesh in meshes:
            sidecar = _resolve_sidecar_for_object(mesh)
            if sidecar is None:
                warnings.append(f"{mesh.name}: no d4extract sidecar found")
                continue
            warnings.extend(
                material_builder.setup_object_materials(
                    mesh, sidecar, strip_color0=True,
                )
            )
            if backface:
                for slot in mesh.material_slots:
                    if slot.material is not None:
                        slot.material.use_backface_culling = True
            done += 1

        if done == 0:
            self.report({"WARNING"}, "; ".join(warnings[:3]) or "Nothing set up")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Set up PBR materials on {done} object(s)")
        for w in warnings[:8]:
            self.report({"WARNING"}, w)
        return {"FINISHED"}
