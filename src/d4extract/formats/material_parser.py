"""Material/texture metadata resolution for .app models.

Walks the d4data JSON dumps to turn ``SubObject.nMaterialIndex`` values
into named, tinted materials with full texture inventories. The chain
is::

    .app.json → ptAppearanceMaterials[]
              → AppearanceMaterial.ptSOAs[0].snoMaterial
              → .mat.json → tUberMaterial.ptMatTexList[]
              → MaterialTexture.snoTex → .tex.json
              → dwWidth/dwHeight/eTexFormat/rgbavalAvgColor

No binary `.mat`/`.tex` payloads are read. Texture pixels live in CASC;
this module gives the exporter enough to populate slot inventories,
material names, baseColorFactor (from `rgbavalAvgColor`), and
emissiveFactor (from `ptRunTimeMaterialValues`) so a downstream
Blender importer can resolve images later when CASC extraction is
wired up.

See `docs/material_format_spec.md` for the format details and the
slot-to-role mapping used by ``SLOT_ROLES``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Slot index → semantic role. See material_format_spec.md §3.2.
# Slots not in this table are exported as ``"SLOT_<n>"`` so unknown
# slots round-trip through the texture inventory.
SLOT_ROLES: dict[int, str] = {
    1:   "BASE_COLOR",        # creature/world albedo
    3:   "NORMAL",
    11:  "BASE_COLOR",        # layered terrain shader, layer 0 color
    13:  "BASE_COLOR",        # layered terrain shader, layer 1 color
    19:  "BASE_COLOR",        # character albedo (dye-system)
    47:  "NORMAL",            # layered terrain shader, layer 0 normal
    48:  "NORMAL",            # layered terrain shader, layer 1 normal
    54:  "DYE_MASK",
    56:  "DYE_RAMP",
    62:  "ROUGHNESS",
    63:  "METALLIC",
    81:  "AO",
    86:  "EMISSIVE",
    96:  "MASK_PRIMARY",
    97:  "NOISE_PROCEDURAL",
    104: "TRANSLUCENCY",
    108: "DYE_MASK_2",
    112: "ROUGHNESS",         # layered terrain shader, layer 0 roughness
    113: "ROUGHNESS",         # layered terrain shader, layer 1 roughness
    145: "SKIN_MASK",
    212: "DETAIL_NORMAL",
    213: "DETAIL_NORMAL",
    214: "DETAIL_NORMAL",
    218: "DETAIL_ROUGHNESS",
    219: "DETAIL_ROUGHNESS",
    220: "DETAIL_ROUGHNESS",
}

# Slots 11/13/47/48/112/113 are the per-layer outputs of the
# ``scene_def_global_layered_*`` shader used by environment statics
# (rocks, terrain props). At runtime the engine blends layer 0 and
# layer 1 via a vertex/height mask. glTF has no native concept of a
# layered material, so the exporter keeps things simple: it picks the
# *first* texture matching a role (so layer 0 always wins for
# BASE_COLOR / NORMAL / ROUGHNESS) and discards layer 1. The unused
# layer is still recorded in the materials sidecar JSON for downstream
# Blender importers that want to reconstruct the blend.

# Roles considered "albedo" candidates when seeding `baseColorFactor`.
# Order matters: slot 1 (creatures) wins over slot 19 (character)
# only when both are present, but in practice only one is present per
# material — we just take the first.
_BASE_COLOR_ROLES = ("BASE_COLOR",)


class MaterialResolutionError(Exception):
    """Raised when the d4data JSON tree cannot be walked."""


@dataclass
class TextureRef:
    """A single texture binding inside a material."""

    slot: int
    role: str
    sno_id: int
    path: str           # e.g. "base/meta/Texture/<name>.tex"
    width: int
    height: int
    format: int         # eTexFormat — see material_format_spec.md §4.3
    avg_rgba: tuple[float, float, float, float]


@dataclass
class Material:
    """A resolved material slot for one ``ptAppearanceMaterials`` entry."""

    name: str
    sno_id: int
    shader_map: str
    textures: list[TextureRef] = field(default_factory=list)
    base_color_factor: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    emissive_factor: tuple[float, float, float] = (0.0, 0.0, 0.0)
    metallic_factor: float = 1.0     # always 1.0 — metallic is texture-driven
    roughness_factor: float = 1.0    # always 1.0 — roughness is texture-driven
    # True when the AppearanceMaterial entry has ``snoMaterial=null`` and
    # only ``snoCloth`` is set — i.e. the submeshes that use it are
    # physics-simulation cards (cloth flaps, capes, skirts) with no
    # visible material data attached. The exporter drops these from the
    # glTF by default — see ``GltfExporter(include_cloth=False)``.
    is_cloth_only: bool = False


# ─── Public API ──────────────────────────────────────────────────────

def load_materials(model_stem: str, d4data_path: Path) -> list[Material]:
    """Resolve ``ptAppearanceMaterials`` for one model.

    ``d4data_path`` must point at the ``json/`` directory of a
    `DiabloTools/d4data <https://github.com/DiabloTools/d4data>`_
    checkout (so that ``base/meta/Appearance/<stem>.app.json`` resolves).

    Returns one :class:`Material` per entry in
    ``ptAppearanceMaterials``, matching the index space used by
    ``SubObject.nMaterialIndex``. If a material file is missing the
    entry is still emitted as a stub carrying just the SNO name —
    callers can rely on ``len(result) == len(ptAppearanceMaterials)``.

    Raises :class:`MaterialResolutionError` if the appearance JSON
    itself cannot be located; any deeper failures are swallowed and
    the corresponding material is emitted as a stub.
    """
    d4data_path = Path(d4data_path)
    app_path = d4data_path / "base" / "meta" / "Appearance" / f"{model_stem}.app.json"
    if not app_path.is_file():
        raise MaterialResolutionError(
            f"Appearance JSON not found: {app_path}\n"
            f"Check that --d4data-path points at the d4data 'json/' directory "
            f"and that the model stem matches a file under "
            f"base/meta/Appearance/."
        )

    app_data = _read_json(app_path)
    pt_app_mats = app_data.get("ptAppearanceMaterials") or []

    materials: list[Material] = []
    for idx, entry in enumerate(pt_app_mats):
        materials.append(_resolve_appearance_material(entry, idx, d4data_path))
    return materials


# ─── Internal helpers ────────────────────────────────────────────────

def _resolve_appearance_material(
    entry: dict[str, Any], index: int, d4data_path: Path,
) -> Material:
    """Resolve one ``AppearanceMaterial`` → ``Material``.

    A ptSOAs entry can carry several mutually-exclusive references —
    ``snoMaterial`` for visible PBR materials, ``snoCloth`` for physics
    simulation, ``snoEffectGroup`` for runtime FX. When ``snoMaterial``
    is ``null`` but any ``snoCloth`` is set across the persona variants,
    the slot is a **cloth-only** entry — its submeshes are simulation
    cards with no visible material data. Mark them so the exporter can
    skip them by default.
    """
    soas = entry.get("ptSOAs") or []
    cloth_only = any(
        isinstance(s.get("snoCloth"), dict)
        for s in soas
        if not isinstance(s.get("snoMaterial"), dict)
    ) and not any(
        isinstance(s.get("snoMaterial"), dict) for s in soas
    )

    # When the slot is a cloth simulation proxy (no snoMaterial, just a
    # snoCloth reference) the snoCloth.name is the meaningful label —
    # ``Cloth_lilith_skirt_mat``, ``mount_horse13_tail_sim``, etc. —
    # which beats the generic ``Material_{index}`` fallback.
    cloth_label = _first_cloth_name(soas) if cloth_only else None

    if not soas:
        return _stub_material(index, name=f"Material_{index}", sno_id=0,
                              is_cloth_only=cloth_only)

    # Persona 0 is always present in the sampled data; future work could
    # surface other personas as separate variants. For now we collapse
    # to the first SOA.
    soa = soas[0]
    sno_mat = soa.get("snoMaterial")
    if not isinstance(sno_mat, dict):
        return _stub_material(
            index,
            name=cloth_label or f"Material_{index}",
            sno_id=0,
            is_cloth_only=cloth_only,
        )

    mat_name = sno_mat.get("name") or f"Material_{index}"
    mat_sno_id = int(sno_mat.get("__raw__") or 0)
    mat_rel_path = sno_mat.get("__targetFileName__") or ""

    if not mat_rel_path:
        return _stub_material(index, name=mat_name, sno_id=mat_sno_id)

    mat_full = d4data_path / f"{mat_rel_path}.json"
    if not mat_full.is_file():
        # Material referenced but JSON not present — emit a name-only stub
        return _stub_material(index, name=mat_name, sno_id=mat_sno_id)

    try:
        mat_data = _read_json(mat_full)
    except (OSError, json.JSONDecodeError):
        return _stub_material(index, name=mat_name, sno_id=mat_sno_id)

    return _build_material(mat_name, mat_sno_id, mat_data, d4data_path)


def _build_material(
    name: str, sno_id: int, mat_data: dict[str, Any], d4data_path: Path,
) -> Material:
    """Walk a ``MaterialDefinition`` JSON into a :class:`Material`."""
    uber = mat_data.get("tUberMaterial") or {}

    shader_map = ""
    sm = uber.get("snoShaderMap")
    if isinstance(sm, dict):
        shader_map = sm.get("name") or ""

    textures: list[TextureRef] = []
    for tex_entry in uber.get("ptMatTexList") or []:
        tref = _resolve_texture_entry(tex_entry, d4data_path)
        if tref is not None:
            textures.append(tref)

    runtime_blocks = uber.get("ptRunTimeMaterialValues") or []
    base_color_factor = _pick_base_color(textures)
    if base_color_factor == (1.0, 1.0, 1.0, 1.0) and not textures:
        # Textureless materials (e.g. NPC_Eye shader on Lilith) compute
        # albedo analytically from a runtime vector. Surface a meaningful
        # tint so the glTF doesn't fall back to flat white. ``Iris_InnerColor_Eye``
        # is the iris tint; ``color`` is a generic diffuse multiplier (often
        # 1,1,1,1 but worth checking as a secondary).
        base_color_factor = _runtime_color_fallback(runtime_blocks)
    emissive_factor = _emissive_from_runtime(
        runtime_blocks,
        has_emissive_texture=any(t.role == "EMISSIVE" for t in textures),
    )

    return Material(
        name=name,
        sno_id=sno_id,
        shader_map=shader_map,
        textures=textures,
        base_color_factor=base_color_factor,
        emissive_factor=emissive_factor,
    )


def _resolve_texture_entry(
    entry: dict[str, Any], d4data_path: Path,
) -> TextureRef | None:
    """Read one ``MaterialTextureEntry`` → :class:`TextureRef`.

    Returns ``None`` for null/procedural slots so the inventory
    only carries actual texture bindings.
    """
    slot = int(entry.get("eShaderTex", -1))
    mat_tex = entry.get("tMatTex") or {}
    sno_tex = mat_tex.get("snoTex")
    if not isinstance(sno_tex, dict):
        return None

    role = SLOT_ROLES.get(slot, f"SLOT_{slot}")
    tex_name = sno_tex.get("name") or ""
    tex_path = sno_tex.get("__targetFileName__") or ""
    tex_id = int(sno_tex.get("__raw__") or 0)

    width = height = 0
    fmt = 0
    avg = (1.0, 1.0, 1.0, 1.0)

    tex_full = d4data_path / f"{tex_path}.json"
    if tex_full.is_file():
        try:
            tex_data = _read_json(tex_full)
            width = int(tex_data.get("dwWidth") or 0)
            height = int(tex_data.get("dwHeight") or 0)
            fmt = int(tex_data.get("eTexFormat") or 0)
            color = tex_data.get("rgbavalAvgColor") or {}
            avg = (
                float(color.get("r", 1.0)),
                float(color.get("g", 1.0)),
                float(color.get("b", 1.0)),
                float(color.get("a", 1.0)),
            )
        except (OSError, json.JSONDecodeError, ValueError):
            pass  # fall through with placeholder values

    return TextureRef(
        slot=slot, role=role, sno_id=tex_id,
        path=tex_path, width=width, height=height,
        format=fmt, avg_rgba=avg,
    )


def _pick_base_color(textures: list[TextureRef]) -> tuple[float, float, float, float]:
    """Take the first ``BASE_COLOR`` texture's average linear RGBA.

    Slots 1 (creature) and 19 (character) are mutually exclusive in
    every sampled material, but we prefer the first match either way.
    Falls back to opaque white when no albedo texture is bound — that
    keeps the glTF baseColorFactor neutral so the texture (when
    eventually wired up) is not double-tinted.
    """
    for t in textures:
        if t.role in _BASE_COLOR_ROLES:
            return t.avg_rgba
    return (1.0, 1.0, 1.0, 1.0)


def _runtime_color_fallback(
    runtime_blocks: list[dict[str, Any]],
) -> tuple[float, float, float, float]:
    """Pull a base-color tint from ``ptRunTimeMaterialValues`` when no
    BASE_COLOR texture is bound.

    Some materials (notably the ``NPC_Eye`` shader used by Lilith's eyes)
    have an empty ``ptMatTexList`` because the shader computes albedo
    analytically from runtime vectors. Surfacing one of those vectors as
    glTF ``baseColorFactor`` gives the eye a yellowy iris tint instead
    of pure white.

    Preference order (first match wins):

    1. ``Iris_InnerColor_Eye`` — eye-specific iris tint, typically the
       most representative single vector for an eye material.
    2. ``color`` — generic diffuse multiplier; usually ``(1, 1, 1, 1)``
       but worth checking in case a material authored a real value.

    Falls back to opaque white if neither key is present or both equal
    the white default.
    """
    PREFERRED = ("Iris_InnerColor_Eye", "color")
    for key in PREFERRED:
        for runtime in runtime_blocks:
            for entry in runtime.get("arMaterialVectorValues") or []:
                value_obj = entry.get("tValue") or {}
                sno_mv = value_obj.get("snoMaterialValue")
                if not isinstance(sno_mv, dict):
                    continue
                if sno_mv.get("name") != key:
                    continue
                rgba = value_obj.get("value") or {}
                t = (
                    max(0.0, min(1.0, float(rgba.get("x", 1.0)))),
                    max(0.0, min(1.0, float(rgba.get("y", 1.0)))),
                    max(0.0, min(1.0, float(rgba.get("z", 1.0)))),
                    max(0.0, min(1.0, float(rgba.get("w", 1.0)))),
                )
                # Skip the no-op (1,1,1,1) — keep looking for a real tint.
                if t != (1.0, 1.0, 1.0, 1.0):
                    return t
    return (1.0, 1.0, 1.0, 1.0)


def _emissive_from_runtime(
    runtime_blocks: list[dict[str, Any]],
    *, has_emissive_texture: bool,
) -> tuple[float, float, float]:
    """Compute glTF ``emissiveFactor`` from ``ptRunTimeMaterialValues``.

    Reads ``emissive color`` (vec4) and the scalars
    ``emissive multiplier`` / ``Emissive Texture Multiplier`` and
    returns ``rgb * mult * tex_mult`` clamped to [0, 1]. When the
    material does not bind an EMISSIVE texture we return zero so the
    glTF doesn't paint the whole primitive with a uniform glow (glTF's
    emissive without a texture is a flat factor).
    """
    if not has_emissive_texture:
        return (0.0, 0.0, 0.0)

    color = (1.0, 1.0, 1.0)
    multiplier = 1.0
    tex_mult = 1.0

    for runtime in runtime_blocks:
        for vec_entry in runtime.get("arMaterialVectorValues") or []:
            value_obj = vec_entry.get("tValue") or {}
            sno_mv = value_obj.get("snoMaterialValue")
            if not isinstance(sno_mv, dict):
                continue
            if sno_mv.get("name") == "emissive color":
                rgba = value_obj.get("value") or {}
                color = (
                    float(rgba.get("x", 1.0)),
                    float(rgba.get("y", 1.0)),
                    float(rgba.get("z", 1.0)),
                )

        for scalar_entry in runtime.get("arMaterialScalarValues") or []:
            value_obj = scalar_entry.get("tValue") or {}
            sno_mv = value_obj.get("snoMaterialValue")
            if not isinstance(sno_mv, dict):
                continue
            name = sno_mv.get("name")
            value = float(value_obj.get("value", 0.0))
            if name == "emissive multiplier":
                multiplier = value
            elif name == "Emissive Texture Multiplier":
                tex_mult = value

    return (
        max(0.0, min(1.0, color[0] * multiplier * tex_mult)),
        max(0.0, min(1.0, color[1] * multiplier * tex_mult)),
        max(0.0, min(1.0, color[2] * multiplier * tex_mult)),
    )


def _first_cloth_name(soas: list[dict[str, Any]]) -> str | None:
    """Return the first non-null ``snoCloth.name`` across persona SOAs.

    Used to pick a meaningful display name for cloth-only slots where
    ``snoMaterial`` is null (no PBR material attached).
    """
    for s in soas:
        cloth = s.get("snoCloth")
        if isinstance(cloth, dict):
            name = cloth.get("name")
            if name:
                return name
    return None


def _stub_material(
    index: int, *, name: str, sno_id: int, is_cloth_only: bool = False,
) -> Material:
    """Placeholder when a material entry can't be fully resolved."""
    return Material(
        name=name or f"Material_{index}",
        sno_id=sno_id,
        shader_map="",
        textures=[],
        base_color_factor=(1.0, 1.0, 1.0, 1.0),
        emissive_factor=(0.0, 0.0, 0.0),
        is_cloth_only=is_cloth_only,
    )


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ─── Sidecar serialisation ───────────────────────────────────────────

def materials_to_sidecar(
    materials: list[Material],
    variants: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Build the dict written to ``<model>.materials.json``.

    ``variants`` — an optional customization-variant block as produced
    by :func:`d4extract.formats.variants.variants_to_sidecar_block`.
    When ``None`` (the default) the ``variants`` key is omitted entirely
    so existing sidecar consumers round-trip unchanged. When supplied it
    is embedded verbatim under the top-level ``variants`` key; each
    entry's ``image_index`` indexes the glTF ``images`` array (``-1``
    when the variant texture was not embedded). See ``variants.py`` for
    the schema and the ``variant_<kind>_<id>`` image-naming convention.
    """
    out: dict[str, Any] = {
        "materials": [
            {
                "name": m.name,
                "sno_id": m.sno_id,
                "shader_map": m.shader_map,
                "base_color_factor": list(m.base_color_factor),
                "emissive_factor": list(m.emissive_factor),
                "metallic_factor": m.metallic_factor,
                "roughness_factor": m.roughness_factor,
                "is_cloth_only": m.is_cloth_only,
                "textures": [
                    {
                        "slot": t.slot,
                        "role": t.role,
                        "sno_id": t.sno_id,
                        "path": t.path,
                        "width": t.width,
                        "height": t.height,
                        "format": t.format,
                        "avg_rgba": list(t.avg_rgba),
                    }
                    for t in m.textures
                ],
            }
            for m in materials
        ],
    }
    if variants is not None:
        out["variants"] = variants
    return out
