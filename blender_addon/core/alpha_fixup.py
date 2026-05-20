"""Surface render method ("dithered alpha") handling.

D4 character meshes layer alpha-tested geometry (hair cards, eyelashes,
cloth tatters). In EEVEE-Next the ``DITHERED`` surface render method
renders that geometry without the back-to-front sort that ``BLENDED``
needs — which real-time renderers cannot do reliably for overlapping
cards. The one exception is facial-hair / stubble (``hero_hair_blend``),
a single soft-edged layer over skin that genuinely wants ``BLENDED``.

``surface_render_method`` is a Blender 4.2+ property. ``blend_method``
(pre-4.2) is intentionally never touched — referencing it raises on 5.1.
"""

from __future__ import annotations

import bpy

# ``mat["d4_alpha_treatment"]`` values (imported from the glTF material
# extras) that must stay BLENDED rather than being forced to DITHERED.
_KEEP_BLENDED_TREATMENTS = frozenset({"hero_hair_blend"})


def _supports_render_method(mat: bpy.types.Material) -> bool:
    return hasattr(mat, "surface_render_method")


def is_facial_hair_blend(mat: bpy.types.Material) -> bool:
    """Whether ``mat`` is a soft-blended facial-hair material.

    Detected via the ``d4_alpha_treatment`` custom property carried over
    from the glTF material extras by d4extract's exporter.
    """
    treatment = mat.get("d4_alpha_treatment")
    return treatment in _KEEP_BLENDED_TREATMENTS


def set_material_dithered(mat: bpy.types.Material) -> None:
    """Set ``surface_render_method`` to ``'DITHERED'`` on ``mat``.

    Idempotent. Requires Blender 4.2+ — raises :class:`RuntimeError`
    otherwise so the caller can surface a clear message.
    """
    if not _supports_render_method(mat):
        raise RuntimeError(
            "This addon requires Blender 4.2+ (surface_render_method)."
        )
    mat.surface_render_method = "DITHERED"


def set_material_blended(mat: bpy.types.Material) -> None:
    """Set ``surface_render_method`` to ``'BLENDED'`` on ``mat``."""
    if not _supports_render_method(mat):
        raise RuntimeError(
            "This addon requires Blender 4.2+ (surface_render_method)."
        )
    mat.surface_render_method = "BLENDED"


def apply_dithered(
    materials: list[bpy.types.Material],
) -> tuple[int, int]:
    """Force ``'DITHERED'`` across ``materials``, sparing facial hair.

    Returns ``(dithered_count, kept_blended_count)``. Facial-hair
    (``hero_hair_blend``) materials are explicitly set to ``'BLENDED'``
    so a prior run cannot leave them dithered.
    """
    dithered = 0
    kept = 0
    seen: set[str] = set()
    for mat in materials:
        if mat is None or mat.name in seen:
            continue
        seen.add(mat.name)
        if is_facial_hair_blend(mat):
            set_material_blended(mat)
            kept += 1
        else:
            set_material_dithered(mat)
            dithered += 1
    return dithered, kept
