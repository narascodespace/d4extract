"""Scene-level property group for the D4Extract importer N-panel."""

from __future__ import annotations

import bpy

from .core import variant_catalog


def _variant_items(kind: str):
    """Build an EnumProperty ``items`` callback for one variant kind.

    The callback delegates to :func:`variant_catalog.items_for`, which
    returns a *cached* list — required so Blender does not garbage
    collect the item strings between redraws.
    """

    def callback(self, context):
        return variant_catalog.items_for(context, kind)

    return callback


def _armature_poll(self, obj: bpy.types.Object) -> bool:
    return obj.type == "ARMATURE"


class D4Props(bpy.types.PropertyGroup):
    """State for the D4 Tools panel, bound to ``Scene.d4_props``."""

    # ── Import options ────────────────────────────────────────────
    auto_setup_materials: bpy.props.BoolProperty(
        name="Auto-setup materials",
        description=(
            "Rebuild PBR materials and strip COLOR_0 vertex colours "
            "automatically right after import"
        ),
        default=True,
    )
    force_dithered: bpy.props.BoolProperty(
        name="Force dithered alpha",
        description=(
            "Set every imported material's surface render method to "
            "DITHERED (facial hair is left BLENDED)"
        ),
        default=True,
    )
    swap_armor_skin: bpy.props.BoolProperty(
        name="Swap armor_skin_mat -> body skin",
        description=(
            "Replace exposed-skin placeholder materials with the body "
            "skin material at import time, so they inherit skin tone "
            "changes automatically (recommended for character imports; "
            "no effect on monster/weapon-only imports)"
        ),
        default=True,
    )
    disable_bone_shape: bpy.props.BoolProperty(
        name="Disable bone shape",
        description=(
            "Pass disable_bone_shape=True to the glTF importer so bones "
            "display as default octahedrals instead of the importer's "
            "heuristic widgets (Blender 4.4+; ignored on older versions)"
        ),
        default=True,
    )
    materials_sidecar_override: bpy.props.StringProperty(
        name="Materials sidecar (override)",
        description=(
            "Optional path to a .materials.json sidecar. Leave empty to "
            "use the file sitting next to the imported .glb"
        ),
        subtype="FILE_PATH",
        default="",
    )
    show_sidecar_override: bpy.props.BoolProperty(
        name="Materials sidecar (override)",
        description="Expand the materials-sidecar override field",
        default=False,
    )

    # ── Variant target + dropdowns ────────────────────────────────
    target_armature: bpy.props.PointerProperty(
        name="Target",
        description="Armature whose materials the variant swaps apply to",
        type=bpy.types.Object,
        poll=_armature_poll,
    )
    variant_skin: bpy.props.EnumProperty(
        name="Skin",
        description=(
            "Parametric skin tone (PersonaSkinColor) — drives both face "
            "and body skin materials via an HSV + darken node chain"
        ),
        items=_variant_items("skin"),
    )
    variant_material: bpy.props.EnumProperty(
        name="Material",
        description="Full-material swap",
        items=_variant_items("material"),
    )
    variant_hair_color: bpy.props.EnumProperty(
        name="Hair Color",
        description=(
            "Parametric hair colour (HairColorDefinition) — multiplies a "
            "chosen RGBA tint onto every hero_hair / hair_pbr_igc material"
        ),
        items=_variant_items("hair_color"),
    )


def register() -> None:
    bpy.utils.register_class(D4Props)
    bpy.types.Scene.d4_props = bpy.props.PointerProperty(type=D4Props)


def unregister() -> None:
    # Free the skin-swatch preview collection before the classes go.
    variant_catalog.release_previews()
    del bpy.types.Scene.d4_props
    bpy.utils.unregister_class(D4Props)
