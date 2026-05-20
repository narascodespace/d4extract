"""Building (and caching) the variant dropdown enum item lists.

Blender's :class:`bpy.props.EnumProperty` with a dynamic ``items``
callback has a notorious requirement: the Python list the callback
returns must be **kept alive** by the addon. If a fresh list is built on
every redraw the strings get garbage-collected and the UI shows
corrupted labels or loses the selection. This module owns a module-level
cache so each ``(sidecar, kind)`` pair returns the *same* list object
until the sidecar actually changes.

The **skin** category is ``parametric_hsv`` — its items carry a colour
swatch icon generated via :mod:`bpy.utils.previews`. Because the dump's
``rgbaUIDisplayColor`` is white for every skin entry, the swatch is
*computed*: the entry's own HSV + darken transform applied to a
reference skin tone (see :func:`swatch_rgb`). The **hair_color**
category is ``parametric_color`` — its items carry a swatch drawn
straight from each entry's primary RGBA tint. All other categories are
``image_swap`` and render as plain text items.

See https://docs.blender.org/api/current/bpy.props.html#bpy.props.EnumProperty
"""

from __future__ import annotations

import colorsys

import bpy
import bpy.utils.previews

from . import sidecar as sc

# The sentinel always sits at the top of every dropdown.
NONE_ID = "__none__"
_NONE_ITEM = (NONE_ID, "None", "Leave this slot unchanged")
# 5-tuple form for parametric dropdowns (which use per-item icons).
_NONE_ITEM_ICON = (NONE_ID, "None", "Leave this slot unchanged", 0, 0)

# Custom property names stamped onto the imported armature.
PROP_SIDECAR_PATH = "_d4_sidecar_path"
PROP_GLB_PATH = "_d4_glb_path"

# Reference mid skin tone. Each PersonaSkinColor's HSV+darken transform
# is applied to it to render a representative dropdown swatch.
_REF_SKIN = (0.76, 0.57, 0.46)

# Cache: (sidecar_path, kind) -> (sidecar_object, items_list).
_ENUM_CACHE: dict[tuple[str, str], tuple[object, list]] = {}

# A single shared fallback list (kept alive for the GC reason).
_FALLBACK_ITEMS: list[tuple] = [_NONE_ITEM]

# Lazily-created preview collection for the skin colour swatches.
_PREVIEWS: bpy.utils.previews.ImagePreviewCollection | None = None


# ─── Swatch previews ─────────────────────────────────────────────────

def swatch_rgb(
    hue: float, saturation: float, value: float, darken: float,
) -> tuple[float, float, float]:
    """The colour the HSV+darken chain produces on reference skin.

    Mirrors :func:`material_builder.setup_skin_tone_chain` exactly: hue
    rotated by ``hue``, saturation/value scaled by ``1 + delta``, then
    the whole colour multiplied by ``darken``.
    """
    h, s, v = colorsys.rgb_to_hsv(*_REF_SKIN)
    h = (h + hue) % 1.0
    s = max(0.0, min(1.0, s * (1.0 + saturation)))
    v = max(0.0, min(1.0, v * (1.0 + value)))
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    d = max(0.0, darken)
    return (min(1.0, r * d), min(1.0, g * d), min(1.0, b * d))


def _swatch_icon_id(key: str, rgb: tuple[float, float, float]) -> int:
    """Get-or-create a solid-colour preview icon; ``0`` on any failure."""
    global _PREVIEWS
    try:
        if _PREVIEWS is None:
            _PREVIEWS = bpy.utils.previews.new()
        prev = _PREVIEWS.get(key)
        if prev is None:
            prev = _PREVIEWS.new(key)
            size = 16
            prev.icon_size = (size, size)
            r, g, b = rgb
            prev.icon_pixels_float = [r, g, b, 1.0] * (size * size)
        return prev.icon_id
    except Exception:
        # Procedural previews are finicky across builds — degrade to a
        # plain (icon-less) dropdown rather than break the panel.
        return 0


def release_previews() -> None:
    """Free the swatch preview collection (call on addon unregister)."""
    global _PREVIEWS
    if _PREVIEWS is not None:
        try:
            bpy.utils.previews.remove(_PREVIEWS)
        except Exception:
            pass
        _PREVIEWS = None
    _ENUM_CACHE.clear()


# ─── Target resolution ───────────────────────────────────────────────

def resolve_target_armature(
    context: bpy.types.Context,
) -> bpy.types.Object | None:
    """Find the armature the variant operations should act on."""
    props = getattr(context.scene, "d4_props", None)
    if props is not None and props.target_armature is not None:
        if props.target_armature.type == "ARMATURE":
            return props.target_armature

    active = context.active_object
    if active is not None:
        if active.type == "ARMATURE":
            return active
        if active.parent is not None and active.parent.type == "ARMATURE":
            return active.parent

    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and obj.get(PROP_SIDECAR_PATH):
            return obj
    return None


def sidecar_for_context(context: bpy.types.Context) -> sc.Sidecar | None:
    """Load the sidecar attached to the resolved target armature."""
    arm = resolve_target_armature(context)
    if arm is None:
        return None
    side_path = arm.get(PROP_SIDECAR_PATH)
    if not side_path:
        return None
    return sc.load_sidecar_file(side_path)


# ─── Enum item building ──────────────────────────────────────────────

def items_for(
    context: bpy.types.Context, kind: str,
) -> list[tuple]:
    """Return the EnumProperty items for one variant ``kind``.

    Always returns a list whose first entry is the ``__none__``
    sentinel. The list object is stable across redraws (see the module
    docstring). Parametric items (skin ``parametric_hsv`` / hair colour
    ``parametric_color``) are 5-tuples with a colour-swatch icon;
    image-swap items are 3-tuples.
    """
    side = sidecar_for_context(context)
    if side is None:
        return _FALLBACK_ITEMS

    cached = _ENUM_CACHE.get((side.path, kind))
    if cached is not None and cached[0] is side:
        return cached[1]

    cat = side.variants.get(kind)
    if cat is not None and cat.kind == "parametric_hsv":
        items = _parametric_items(cat)
    elif cat is not None and cat.kind == "parametric_color":
        items = _parametric_color_items(cat)
    else:
        items = _image_swap_items(cat)

    _ENUM_CACHE[(side.path, kind)] = (side, items)
    return items


def _swatch_for(tone: sc.SidecarSkinTone) -> tuple[float, float, float]:
    """Pick the dropdown swatch colour for ``tone``.

    The player palette ships a real ``rgbaUIDisplayColor`` per tone, so
    that is used directly. Only when it is degenerate (the all-white NPC
    entries) does the swatch fall back to a computed HSV preview.
    """
    r, g, b, _ = tone.ui_color
    if not (r > 0.95 and g > 0.95 and b > 0.95):
        return (r, g, b)
    return swatch_rgb(tone.hue, tone.saturation, tone.value, tone.darken)


def _parametric_items(cat: sc.SidecarVariantCategory) -> list[tuple]:
    items: list[tuple] = [_NONE_ITEM_ICON]
    for idx, tone in enumerate(cat.skin_entries):
        rgb = _swatch_for(tone)
        icon = _swatch_icon_id(tone.id, rgb)
        desc = (
            f"hue {tone.hue:+.2f}  sat {tone.saturation:+.2f}  "
            f"val {tone.value:+.2f}  darken {tone.darken:.2f}"
        )
        items.append((tone.id, tone.display_name, desc, icon, idx + 1))
    return items


def _parametric_color_items(cat: sc.SidecarVariantCategory) -> list[tuple]:
    """Build the ``parametric_color`` (hair colour) dropdown items.

    The swatch icon is the entry's primary ``ui_color`` (``rgbaColors[0]``).
    Duotone work (Phase 2) could render a split-swatch from the
    secondary colour; the MVP uses the primary tint only.
    """
    items: list[tuple] = [_NONE_ITEM_ICON]
    for idx, hc in enumerate(cat.hair_entries):
        rgb = hc.ui_color[:3]
        icon = _swatch_icon_id(f"hair_{hc.id}", rgb)
        desc = (
            f"primary {hc.ui_color[:3]}  "
            f"secondary {hc.secondary_color[:3]}  "
            f"influence {hc.influence:.2f}"
        )
        items.append((hc.id, hc.display_name, desc, icon, idx + 1))
    return items


def _image_swap_items(cat: sc.SidecarVariantCategory | None) -> list[tuple]:
    items: list[tuple] = [_NONE_ITEM]
    if cat is None:
        return items
    for entry in cat.image_entries:
        if entry.image_index < 0:
            desc = f"{entry.source_path} (texture not embedded — swap skipped)"
        else:
            desc = entry.source_path
        items.append((entry.id, entry.display_name, desc))
    return items


def invalidate() -> None:
    """Drop the enum-item cache (called on re-import / addon reload)."""
    _ENUM_CACHE.clear()
