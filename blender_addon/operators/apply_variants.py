"""``D4_OT_apply_variants`` — apply the selected customization variants.

Three mechanics, one button:

* **skin** is ``parametric_hsv`` — the chosen ``PersonaSkinColor``'s
  Hue / Saturation / Value / Darken floats are written into the
  ``d4_skin_hsv`` + ``d4_skin_darken`` node chain (inserted on demand by
  :func:`material_builder.setup_skin_tone_chain`) on every skin material.
* **hair_color** is ``parametric_color`` — the chosen
  ``HairColorDefinition``'s primary RGBA tint is written into the
  ``d4_hair_tint`` Multiply node (inserted on demand by
  :func:`material_builder.setup_hair_color_chain`) on every hair material.
* **eyes / makeup / material / markings** are ``image_swap`` — the
  variant image datablock is reassigned onto the role's texture node.
"""

from __future__ import annotations

import re

import bpy

from ..core import material_builder
from ..core import sidecar as sc
from ..core import variant_catalog as vc
from ..core.material_builder import NODE_ROLE_PROP

# Roles whose swapped image must be sampled as raw data, not sRGB.
_NONCOLOR_ROLES = frozenset({
    "NORMAL", "ROUGHNESS", "METALLIC", "AO", "MARKINGS",
    "DYE_MASK", "DYE_RAMP", "DYE_MASK_2", "SKIN_MASK",
})

# Variant roles with no PBR socket of their own — apply makes a labelled,
# unconnected node for them on demand.
_LAZY_ROLES = frozenset({"MAKEUP", "MARKINGS"})

# Image-swap categories, in apply order. Skin is handled separately.
_IMAGE_KINDS = ("eyes", "makeup", "material", "markings")


class D4_OT_apply_variants(bpy.types.Operator):
    """Apply the selected Skin / Eyes / Makeup / Hair Color / Markings variants"""

    bl_idname = "d4.apply_variants"
    bl_label = "Apply Variants"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        arm = vc.resolve_target_armature(context)
        if arm is None:
            self.report({"ERROR"}, "No target armature — set one in the panel")
            return {"CANCELLED"}

        side_path = arm.get(vc.PROP_SIDECAR_PATH)
        sidecar = sc.load_sidecar_file(side_path) if side_path else None
        if sidecar is None:
            self.report({"ERROR"}, f"No sidecar for armature '{arm.name}'")
            return {"CANCELLED"}

        props = context.scene.d4_props
        materials = _materials_under_armature(arm)
        applied = 0
        warnings: list[str] = []

        # ── skin: parametric HSV ──────────────────────────────────
        skin_pick = props.variant_skin
        if skin_pick and skin_pick != vc.NONE_ID:
            n, w = self._apply_skin(sidecar, materials, skin_pick)
            applied += n
            warnings.extend(w)

        # ── hair_color: parametric RGBA tint ──────────────────────
        hair_pick = props.variant_hair_color
        if hair_pick and hair_pick != vc.NONE_ID:
            n, w = self._apply_hair_color(sidecar, materials, hair_pick)
            applied += n
            warnings.extend(w)

        # ── eyes / makeup / material / markings: image swap ───────
        image_picks = {
            "eyes": props.variant_eyes,
            "makeup": props.variant_makeup,
            "material": props.variant_material,
            "markings": props.variant_markings,
        }
        for kind in _IMAGE_KINDS:
            pick = image_picks[kind]
            if not pick or pick == vc.NONE_ID:
                continue
            cat = sidecar.variants.get(kind)
            entry = _find_image_entry(cat, pick)
            if entry is None:
                warnings.append(f"{kind}: '{pick}' not found in sidecar")
                continue
            if entry.image_index < 0:
                warnings.append(
                    f"{kind}: '{entry.display_name}' texture was not embedded"
                )
                continue
            image = bpy.data.images.get(entry.image_name)
            if image is None:
                warnings.append(
                    f"{kind}: image '{entry.image_name}' not loaded "
                    f"(re-import the .glb)"
                )
                continue
            ok, why = _swap_one(sidecar, materials, entry, image)
            if ok:
                applied += 1
            else:
                warnings.append(f"{kind}: {why}")

        if applied == 0 and not warnings:
            self.report({"INFO"}, "No variants selected")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Applied {applied} variant update(s)")
        for w in warnings[:8]:
            self.report({"WARNING"}, w)
        return {"FINISHED"}

    def _apply_skin(
        self,
        sidecar: sc.Sidecar,
        materials: list[bpy.types.Material],
        skin_id: str,
    ) -> tuple[int, list[str]]:
        """Drive the HSV+darken chain for the chosen skin tone."""
        cat = sidecar.variants.get("skin")
        if cat is None or cat.kind != "parametric_hsv":
            return 0, ["skin: sidecar has no parametric skin block"]
        tone = next((t for t in cat.skin_entries if t.id == skin_id), None)
        if tone is None:
            return 0, [f"skin: '{skin_id}' not found in sidecar"]

        # Map each skin-material base name to its sidecar material (used
        # for the black-placeholder check). Several sidecar entries can
        # share a name — the three ``armor_skin_mat`` cutouts — and
        # Blender's glTF importer splits same-named materials into
        # distinct datablocks (``armor_skin_mat`` / ``.001`` / ``.002``).
        # So drive *every* Blender material whose base name is a skin
        # target, not just the first match, or two of the three exposed-
        # skin patches would stay black.
        targets: dict[str, sc.SidecarMaterial] = {}
        for idx in cat.applies_to_materials:
            if 0 <= idx < len(sidecar.materials):
                sm = sidecar.materials[idx]
                targets.setdefault(_base_name(sm.name), sm)
        if not targets:
            return 0, ["skin: no applicable skin materials in sidecar"]

        warnings: list[str] = []
        updated = 0
        matched: set[str] = set()
        for mat in materials:
            base = _base_name(mat.name)
            sm = targets.get(base)
            if sm is None:
                continue
            matched.add(base)
            # Pass the sidecar material so the chain can detect a
            # black-placeholder BASE_COLOR (armor_skin_mat / black.tex)
            # and feed mid-grey instead of black-from-black.
            chain = material_builder.setup_skin_tone_chain(mat, sm)
            if chain is None:
                warnings.append(f"skin: '{mat.name}' has no Principled BSDF")
                continue
            hsv, darken = chain
            # flHue is a [0,1] hue-rotation fraction; Blender's
            # HueSaturation Hue socket is neutral at 0.5, so 0.5 + hue
            # rotates by exactly `hue`. Saturation/Value sockets are
            # multipliers (1.0 = unchanged) — the floats are deltas.
            hsv.inputs["Hue"].default_value = (0.5 + tone.hue) % 1.0
            hsv.inputs["Saturation"].default_value = max(0.0, 1.0 + tone.saturation)
            hsv.inputs["Value"].default_value = max(0.0, 1.0 + tone.value)
            hsv.inputs["Fac"].default_value = 1.0
            # flDarken is a brightness multiplier (1.0 = unchanged).
            d = max(0.0, tone.darken)
            darken.inputs[7].default_value = (d, d, d, 1.0)
            updated += 1

        for missing in sorted(set(targets) - matched):
            warnings.append(f"skin: material '{missing}' not found in scene")

        if updated:
            self.report(
                {"INFO"},
                f"Skin '{tone.display_name}': {updated} material(s) updated",
            )
        return updated, warnings

    def _apply_hair_color(
        self,
        sidecar: sc.Sidecar,
        materials: list[bpy.types.Material],
        hair_id: str,
    ) -> tuple[int, list[str]]:
        """Drive the RGBA-tint chain for the chosen hair colour."""
        cat = sidecar.variants.get("hair_color")
        if cat is None or cat.kind != "parametric_color":
            return 0, ["hair_color: sidecar has no parametric hair-colour block"]
        entry = next(
            (e for e in cat.hair_entries if e.id == hair_id), None,
        )
        if entry is None:
            return 0, [f"hair_color: '{hair_id}' not found in sidecar"]

        # Map each hair-material base name to its sidecar material. As
        # with skin, Blender's importer can split same-named materials
        # into ``.001`` datablocks — drive *every* match by base name.
        targets: dict[str, sc.SidecarMaterial] = {}
        for idx in cat.applies_to_materials:
            if 0 <= idx < len(sidecar.materials):
                sm = sidecar.materials[idx]
                targets.setdefault(_base_name(sm.name), sm)
        if not targets:
            return 0, ["hair_color: no applicable hair materials in sidecar"]

        warnings: list[str] = []
        updated = 0
        matched: set[str] = set()
        # flHairColorInfluence drives the Multiply Fac — clamp to [0, 1].
        fac = max(0.0, min(1.0, entry.influence))
        for mat in materials:
            base = _base_name(mat.name)
            sm = targets.get(base)
            if sm is None:
                continue
            matched.add(base)
            tint = material_builder.setup_hair_color_chain(mat, sm)
            if tint is None:
                warnings.append(f"hair_color: '{mat.name}' has no Principled BSDF")
                continue
            # ShaderNodeMixRGB: Color2 is the tint, Fac the blend amount.
            tint.inputs["Color2"].default_value = entry.ui_color
            tint.inputs["Fac"].default_value = fac
            updated += 1

        for missing in sorted(set(targets) - matched):
            warnings.append(f"hair_color: material '{missing}' not found in scene")

        if updated:
            self.report(
                {"INFO"},
                f"Hair '{entry.display_name}': {updated} material(s) updated",
            )
        return updated, warnings


# ─── Internal helpers ────────────────────────────────────────────────

def _find_image_entry(
    cat: sc.SidecarVariantCategory | None, variant_id: str,
) -> sc.SidecarVariant | None:
    if cat is None:
        return None
    for entry in cat.image_entries:
        if entry.id == variant_id:
            return entry
    return None


def _swap_one(
    sidecar: sc.Sidecar,
    materials: list[bpy.types.Material],
    entry: sc.SidecarVariant,
    image: bpy.types.Image,
) -> tuple[bool, str]:
    """Reassign ``image`` onto the node driving ``entry.target_role``."""
    idx = entry.applies_to_material_idx
    if not (0 <= idx < len(sidecar.materials)):
        return False, f"material index {idx} out of range"
    target_name = _base_name(sidecar.materials[idx].name)

    mat = next(
        (m for m in materials if _base_name(m.name) == target_name), None,
    )
    if mat is None:
        return False, f"material '{target_name}' not found in scene"

    node = _find_or_make_role_node(mat, entry.target_role)
    if node is None:
        return False, (
            f"no '{entry.target_role}' texture node — "
            f"run Setup PBR Materials first"
        )

    try:
        image.colorspace_settings.name = _colorspace_for_role(entry.target_role)
    except (TypeError, RuntimeError):
        pass
    node.image = image
    return True, ""


def _find_or_make_role_node(
    mat: bpy.types.Material, role: str,
) -> bpy.types.ShaderNodeTexImage | None:
    """Find the ``ShaderNodeTexImage`` tagged with ``role`` on ``mat``."""
    if not mat.use_nodes or mat.node_tree is None:
        return None
    nt = mat.node_tree
    for node in nt.nodes:
        if node.type == "TEX_IMAGE" and node.get(NODE_ROLE_PROP) == role:
            return node

    if role in _LAZY_ROLES:
        node = nt.nodes.new("ShaderNodeTexImage")
        node.name = f"d4_{role.lower()}"
        node.label = f"d4_{role.lower()}"
        node[NODE_ROLE_PROP] = role
        node.location = (-1100, 350 if role == "MAKEUP" else 30)
        return node
    return None


def _materials_under_armature(
    arm: bpy.types.Object,
) -> list[bpy.types.Material]:
    """Unique materials on meshes deformed by / parented to ``arm``."""
    meshes = [
        o for o in bpy.data.objects
        if o.type == "MESH"
        and (o.parent is arm or o.find_armature() is arm)
    ]
    if not meshes:
        meshes = [o for o in bpy.data.objects if o.type == "MESH"]

    seen: set[str] = set()
    out: list[bpy.types.Material] = []
    for mesh in meshes:
        for slot in mesh.material_slots:
            mat = slot.material
            if mat is not None and mat.name not in seen:
                seen.add(mat.name)
                out.append(mat)
    return out


def _colorspace_for_role(role: str) -> str:
    return "Non-Color" if role in _NONCOLOR_ROLES else "sRGB"


def _base_name(name: str) -> str:
    """Strip Blender's ``.001`` dedup suffix."""
    return re.sub(r"\.\d{3}$", "", name)
