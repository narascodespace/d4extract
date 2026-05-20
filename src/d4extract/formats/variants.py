"""Customization-variant discovery for the materials sidecar.

The Blender import addon exposes five dropdowns — Skin, Eyes, Makeup,
Hair Color, Markings — populated from a ``variants`` block in
``<model>.materials.json``. This module discovers what content is
available for a given character model.

**Schema.** Each category is a *typed* block — ``{"kind": ...,
"entries": [...]}`` — so categories with fundamentally different
mechanics can coexist:

* ``image_swap`` — entries name a texture; the addon swaps the image
  datablock on a node. Used by **makeup** and **markings** (and the
  still-empty **eyes** / **material**).
* ``parametric_hsv`` — entries carry four HSV shader floats; the addon
  drives a Hue/Saturation/Value + Darken node chain. Used by **skin**.
  This block also carries ``applies_to_materials`` — every skin material
  index the chain is inserted into.
* ``parametric_color`` — entries carry RGBA tint tuples + a blend
  influence; the addon drives a Multiply node. Used by **hair_color**.
  Also carries ``applies_to_materials`` — every hair material index.

How each category is enumerated:

* **skin** — the parametric ``PersonaSkinColor`` palette
  (:mod:`d4extract.formats.skin_tones`). One global list, no class
  gating; drives both face and body skin materials. (Face-model swapping
  P00–P03 was removed — it is a separate axis, deferred to a future
  "Face" dropdown.)
* **makeup** — ``base/meta/Makeup/*.mak.json`` overlay textures.
* **markings** — ``base/meta/MarkingShape/*.msh.json`` mask textures,
  class-filtered and capped.
* **hair_color** — the parametric ``HairColorDefinition`` palette
  (:mod:`d4extract.formats.hair_colors`). One global 31-entry list,
  drives every ``hero_hair`` / ``hair_pbr_igc`` material via an RGBA
  multiply tint.
* **eyes** — emitted as an empty ``image_swap`` block; see the prior
  phase's notes for why it is not enumerable yet.
* **material** — kept as an empty ``image_swap`` placeholder for the
  deferred full-material-swap feature. It no longer has a panel row
  (the Hair Color dropdown took its slot); the schema still emits the
  empty block so older-addon / older-sidecar pairings keep loading.

Image-swap :class:`VariantEntry` records carry a :class:`TextureRef` the
glTF exporter embeds; the exporter back-fills ``image_index``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from d4extract.formats.material_parser import (
    Material,
    MaterialResolutionError,
    TextureRef,
    load_materials,
)
from d4extract.formats.hair_colors import (
    DEFAULT_CACHE_PATH as HAIR_CACHE_PATH,
    HairColor,
    HairColorError,
    load_hair_colors,
)
from d4extract.formats.skin_tones import (
    DEFAULT_CACHE_PATH,
    SkinTone,
    SkinToneError,
    load_skin_tones,
)

# The dropdown categories ``discover_variants`` always returns. ``material``
# is retained as an empty placeholder (no panel row) for the deferred
# full-material-swap feature; ``hair_color`` took its panel slot.
VARIANT_KINDS: tuple[str, ...] = (
    "skin", "eyes", "makeup", "material", "hair_color", "markings",
)

# Category ``kind`` discriminators (also used in the sidecar JSON).
KIND_IMAGE_SWAP = "image_swap"
KIND_PARAMETRIC_HSV = "parametric_hsv"
KIND_PARAMETRIC_COLOR = "parametric_color"

# Markings have hundreds of definitions per class; embedding every one
# would bloat the glb past anything usable. Cap the per-class list.
_MARKINGS_CAP = 32

# Class three-letter prefix → eClassRestriction index.
_CLASS_PREFIX_TO_INDEX: dict[str, int] = {
    "sor": 0, "dru": 1, "bar": 2, "rog": 3,
    "nec": 4, "spi": 5, "pal": 6, "war": 7,
}

# Substrings that disqualify a skin-shaded material from being the
# "head skin" the makeup / markings overlays target.
_NON_FACE_SKIN_TOKENS = ("teeth", "tongue", "eye", "lash", "brow")


@dataclass(slots=True)
class VariantEntry:
    """One ``image_swap`` option — makeup / markings / (eyes/material).

    ``image_index`` starts at ``-1`` and is back-filled by the glTF
    exporter once the variant texture is embedded.
    """

    kind: str
    id: str
    display_name: str
    applies_to_material_idx: int
    target_role: str
    texture: TextureRef
    usable_by: tuple[int, ...] | None = None
    image_index: int = -1

    @property
    def variant_image_name(self) -> str:
        """Stable glTF ``image.name`` the addon looks the texture up by."""
        return f"variant_{self.kind}_{self.id}"


@dataclass(slots=True)
class VariantCategory:
    """One dropdown category — a ``kind`` plus its entries.

    ``entries`` holds :class:`VariantEntry` for ``image_swap`` kinds,
    :class:`~d4extract.formats.skin_tones.SkinTone` for
    ``parametric_hsv``, and :class:`~d4extract.formats.hair_colors.HairColor`
    for ``parametric_color``. ``applies_to_materials`` is meaningful only
    for the parametric kinds — the sidecar ``materials`` indices the
    HSV / tint chain is inserted into.
    """

    kind: str
    entries: list = field(default_factory=list)
    applies_to_materials: list[int] = field(default_factory=list)


# ─── Public API ──────────────────────────────────────────────────────

def discover_variants(
    model_stem: str,
    d4data_path: Path,
    materials: list[Material] | None = None,
) -> dict[str, VariantCategory]:
    """Discover customization variants for ``model_stem``.

    Returns a dict keyed by every entry of :data:`VARIANT_KINDS`; each
    value is a :class:`VariantCategory`. A category that cannot be
    enumerated yields an empty block (still typed).

    Never raises — any per-category failure falls back to an empty
    category.
    """
    d4data_path = Path(d4data_path)
    result: dict[str, VariantCategory] = {
        k: VariantCategory(kind=KIND_IMAGE_SWAP) for k in VARIANT_KINDS
    }

    try:
        if materials is None:
            materials = load_materials(model_stem, d4data_path)
    except MaterialResolutionError:
        return result

    # ── skin: parametric HSV, applied to every skin material ──────
    try:
        skin_cat = _discover_skin(materials, d4data_path)
        if skin_cat is not None:
            result["skin"] = skin_cat
    except Exception:  # discovery is best-effort — never fail the export
        pass

    # ── hair_color: parametric RGBA tint, applied to every hair material ─
    try:
        hair_cat = _discover_hair_color(materials, d4data_path)
        if hair_cat is not None:
            result["hair_color"] = hair_cat
    except Exception:  # discovery is best-effort — never fail the export
        pass

    # ── makeup / markings: overlay textures targeting the head skin ─
    head_idx = _head_skin_material_index(materials)
    if head_idx is not None:
        class_index = _class_index_for_stem(model_stem)
        try:
            result["makeup"] = VariantCategory(
                kind=KIND_IMAGE_SWAP,
                entries=_discover_makeup(d4data_path, head_idx),
            )
        except Exception:
            pass
        try:
            result["markings"] = VariantCategory(
                kind=KIND_IMAGE_SWAP,
                entries=_discover_markings(d4data_path, head_idx, class_index),
            )
        except Exception:
            pass

    # eyes / material stay empty image_swap blocks (see module docstring).
    # hair_color also stays empty when the model has no hair materials.
    return result


def variants_to_sidecar_block(
    variants: dict[str, VariantCategory],
) -> dict[str, dict[str, Any]]:
    """Serialise discovered variants to the JSON ``variants`` block.

    Plain-dict output so :func:`material_parser.materials_to_sidecar`
    can embed it without importing this module.
    """
    return {kind: _category_to_dict(cat) for kind, cat in variants.items()}


def all_variant_textures(
    variants: dict[str, VariantCategory],
) -> list[TextureRef]:
    """Every distinct ``image_swap`` :class:`TextureRef`, deduped by path.

    Used by ``extract-textures-for`` to pull variant payloads from CASC.
    Parametric (skin) categories carry no textures and are skipped.
    """
    seen: set[str] = set()
    out: list[TextureRef] = []
    for cat in variants.values():
        if cat.kind != KIND_IMAGE_SWAP:
            continue
        for e in cat.entries:
            if e.texture.path and e.texture.path not in seen:
                seen.add(e.texture.path)
                out.append(e.texture)
    return out


# ─── Skin discovery (parametric HSV) ─────────────────────────────────

def _discover_skin(
    materials: list[Material],
    d4data_path: Path,
    cache_path: Path | None = None,
) -> VariantCategory | None:
    """Return the parametric-HSV skin variant block.

    Loads the ``PersonaSkinColor`` palette and records the indices of
    every skin material the HSV chain should drive (face *and* body).
    Returns ``None`` when the model has no skin materials.
    """
    skin_indices = _skin_material_indices(materials)
    if not skin_indices:
        return None

    # Prefer a live read from the d4data dump; fall back to the
    # checked-in cache when the dump is unavailable.
    try:
        tones = load_skin_tones(d4data_path=d4data_path, cache_path=None)
    except (FileNotFoundError, SkinToneError):
        tones = load_skin_tones(d4data_path=None, cache_path=DEFAULT_CACHE_PATH)
    if not tones:
        return None

    return VariantCategory(
        kind=KIND_PARAMETRIC_HSV,
        entries=list(tones),
        applies_to_materials=skin_indices,
    )


# "skin" as a whole underscore-delimited token in a shader name.
# Substring matching is too loose: ``..._skinEnabled_...`` (a mesh-
# skinning feature flag on VFX shaders) carries "skin" but is not a
# skin surface. ``hero_opaque_skin`` / ``chr_opaque_skin_*`` do match.
_SKIN_SHADER_RE = re.compile(r"(?:^|_)skin(?:_|$)")


def is_skin_material(mat: Material) -> bool:
    """Whether the parametric skin-tone HSV chain should drive ``mat``.

    Matches:

    * ``shader_map`` starts with ``hero_opaque_skin`` (case-insensitive)
    * ``shader_map`` has ``skin`` as a whole ``_``-delimited token
    * the material has a ``SKIN_MASK`` texture (slot 145)
    * ``name == "armor_skin_mat"`` — the exposed-skin cutouts that ship
      with armor pieces (bare arms / midriff); in-game the customization
      runtime tints them with the body skin tone
    * ``name`` contains ``_skin_`` — catches body / arm / etc. variants
      of the armor_skin_mat pattern on other characters

    Excludes:

    * ``name`` containing ``cloth`` — avoids false positives on
      ``cloth_skin_blend`` shaders
    * ``shader_map`` containing ``nocustomization`` — ``base_teethtongue_mat``
      uses ``chr_opaque_skin_noCustomization``, whose name carries
      "skin" but which is explicitly *not* customizable. Excluding it
      keeps teeth / tongue untinted. (See the run report — whether
      teeth should follow skin tone is a deliberate open question.)

    The token-boundary shader match (rather than a bare substring) is
    deliberate: it keeps ``skinEnabled``-style feature-flag shaders
    (e.g. the ``spiM_stor196_goldBlade`` VFX material) out of the skin
    set. See the run report.
    """
    name = (mat.name or "").lower()
    shader = (mat.shader_map or "").lower()
    if "cloth" in name:
        return False
    if "nocustomization" in shader:
        return False
    if shader.startswith("hero_opaque_skin"):
        return True
    if _SKIN_SHADER_RE.search(shader):
        return True
    if any(t.role == "SKIN_MASK" for t in mat.textures):
        return True
    if name == "armor_skin_mat":
        return True
    if "_skin_" in name:
        return True
    return False


def _skin_material_indices(materials: list[Material]) -> list[int]:
    return [i for i, m in enumerate(materials) if is_skin_material(m)]


# ─── Hair-colour discovery (parametric RGBA tint) ────────────────────


def is_hair_material(mat: Material) -> bool:
    """Whether the parametric hair-colour tint should drive ``mat``.

    Detection has two prongs:

    1. ``shader_map`` is a hair shader — any ``hero_hair*`` variant
       (plain ``hero_hair`` for stubble / head hair / mustaches,
       ``hero_hair_2uv`` for full beards, plus any future suffix)
       or exact ``hair_pbr_igc`` (the eyelash / fine-hair shader,
       not a ``hero_hair`` variant).

    2. **Material name ends in ``_Hair_mat``** — catches helmet-
       attached hair that uses a non-``hero_hair*`` shader, e.g.
       ``PalM_stor165_HLM_Hair_mat`` (``hero_armorHair`` shader).
       Confirmed by a full d4data audit (65 ``*_Hair_mat.mat.json``
       files): standard armor materials (``*_BOD_mat``,
       ``*_HLM_mat``, ``*_TRS_mat`` etc.) never end in
       ``_Hair_mat``, so this is a narrow safety net. Adjacent
       hair-named suffixes like ``*_hairpin_mat`` /
       ``*_hairlong_mat`` are deliberately not matched — they need
       the underscore before ``hair``.

    Excluding ``hero_opaque*`` / non-hair shaders is automatic — the
    prefix is ``hero_hair`` (with the underscore baked in via the
    variant suffix), which does not match ``hero_opaque`` etc.
    Bartuc-style unique-set helmets (``bartuc_<class>M_HLM_mat``,
    ``hero_opaque_alphatest``) carry no ``Hair`` in their name and
    so stay excluded — matching their in-game behaviour of being
    fixed gear-design rather than tinted customization.

    The addon-side alpha-wire predicate in
    ``blender_addon/core/material_builder.py`` uses an equivalent
    inline check; keep both in sync if the shader set grows.
    """
    shader = (mat.shader_map or "").lower()
    if shader.startswith("hero_hair") or shader == "hair_pbr_igc":
        return True
    name = (mat.name or "").lower()
    return name.endswith("_hair_mat")


def _hair_material_indices(materials: list[Material]) -> list[int]:
    return [i for i, m in enumerate(materials) if is_hair_material(m)]


def _discover_hair_color(
    materials: list[Material],
    d4data_path: Path,
    cache_path: Path | None = None,
) -> VariantCategory | None:
    """Return the parametric-RGBA hair-colour variant block.

    Loads the ``HairColorDefinition`` palette and records the indices of
    every hair material the tint should drive. Returns ``None`` when the
    model has no hair materials.
    """
    hair_indices = _hair_material_indices(materials)
    if not hair_indices:
        return None

    # Prefer a live read from the d4data dump; fall back to the
    # checked-in cache when the dump is unavailable.
    try:
        colors = load_hair_colors(d4data_path=d4data_path, cache_path=None)
    except (FileNotFoundError, HairColorError):
        colors = load_hair_colors(d4data_path=None, cache_path=HAIR_CACHE_PATH)
    if not colors:
        return None

    return VariantCategory(
        kind=KIND_PARAMETRIC_COLOR,
        entries=list(colors),
        applies_to_materials=hair_indices,
    )


# ─── Makeup discovery ────────────────────────────────────────────────

def _discover_makeup(d4data_path: Path, skin_idx: int) -> list[VariantEntry]:
    """``Makeup/*.mak.json`` → MAKEUP overlay textures."""
    makeup_dir = d4data_path / "base" / "meta" / "Makeup"
    if not makeup_dir.is_dir():
        return []

    entries: list[VariantEntry] = []
    for mak_path in sorted(makeup_dir.glob("*.mak.json")):
        if "Bad Data" in mak_path.name:
            continue
        data = _read_json(mak_path)
        if data is None:
            continue
        tref = _texture_ref_from_sno(
            data.get("snoMakeup"), d4data_path, slot=-1, role="MAKEUP",
        )
        if tref is None:
            continue
        stem = mak_path.name[: -len(".mak.json")]
        entries.append(VariantEntry(
            kind="makeup",
            id=_safe_id(stem),
            display_name=f"Makeup {stem}",
            applies_to_material_idx=skin_idx,
            target_role="MAKEUP",
            texture=tref,
            usable_by=_usable_by(data),
        ))
    return entries


# ─── Markings discovery ──────────────────────────────────────────────

def _discover_markings(
    d4data_path: Path,
    skin_idx: int,
    class_index: int | None,
) -> list[VariantEntry]:
    """``MarkingShape/*.msh.json`` → MARKINGS face-mask textures."""
    msh_dir = d4data_path / "base" / "meta" / "MarkingShape"
    if not msh_dir.is_dir():
        return []

    entries: list[VariantEntry] = []
    for msh_path in sorted(msh_dir.glob("*.msh.json")):
        if len(entries) >= _MARKINGS_CAP:
            break
        if "Bad Data" in msh_path.name:
            continue
        data = _read_json(msh_path)
        if data is None:
            continue
        restriction = data.get("eClassRestriction")
        if not _class_allows(restriction, class_index):
            continue
        tref = _texture_ref_from_sno(
            data.get("snoMaskFace"), d4data_path, slot=-1, role="MARKINGS",
        ) or _texture_ref_from_sno(
            data.get("snoMaskBody"), d4data_path, slot=-1, role="MARKINGS",
        )
        if tref is None:
            continue
        stem = msh_path.name[: -len(".msh.json")]
        entries.append(VariantEntry(
            kind="markings",
            id=_safe_id(stem),
            display_name=_clean_marking_name(stem),
            applies_to_material_idx=skin_idx,
            target_role="MARKINGS",
            texture=tref,
            usable_by=None,
        ))
    return entries


# ─── Internal helpers ────────────────────────────────────────────────

def _head_skin_material_index(materials: list[Material] | None) -> int | None:
    """Index of the head-skin material makeup / markings overlays target."""
    if not materials:
        return None
    fallback: int | None = None
    for idx, mat in enumerate(materials):
        if not any(t.role == "BASE_COLOR" for t in mat.textures):
            continue
        shader = (mat.shader_map or "").lower()
        name = (mat.name or "").lower()
        if "skin" not in shader:
            continue
        if any(tok in name for tok in _NON_FACE_SKIN_TOKENS):
            if fallback is None:
                fallback = idx
            continue
        return idx
    return fallback


def _class_index_for_stem(model_stem: str) -> int | None:
    """Map ``barM_P00`` → Barbarian's class index (2)."""
    return _CLASS_PREFIX_TO_INDEX.get(model_stem[:3].lower())


def _class_allows(restriction: Any, class_index: int | None) -> bool:
    """Whether a marking's ``eClassRestriction`` admits this class."""
    if restriction is None or class_index is None:
        return True
    try:
        r = int(restriction)
    except (TypeError, ValueError):
        return True
    if r < 0 or r > 7:
        return True
    return r == class_index


def _texture_ref_from_sno(
    sno: Any, d4data_path: Path, *, slot: int, role: str,
) -> TextureRef | None:
    """Build a :class:`TextureRef` from a ``DT_SNO`` texture reference."""
    if not isinstance(sno, dict):
        return None
    rel = sno.get("__targetFileName__") or ""
    if not rel:
        return None
    path = rel if rel.endswith(".tex") else f"{rel}.tex"
    sno_id = int(sno.get("__raw__") or 0)

    width = height = 0
    fmt = 0
    avg = (1.0, 1.0, 1.0, 1.0)
    tex_data = _read_json(d4data_path / f"{path}.json")
    if tex_data is not None:
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
    return TextureRef(
        slot=slot, role=role, sno_id=sno_id, path=path,
        width=width, height=height, format=fmt, avg_rgba=avg,
    )


def _usable_by(data: dict[str, Any]) -> tuple[int, ...] | None:
    raw = data.get("fUsableByClass")
    if isinstance(raw, list) and len(raw) == 8:
        try:
            return tuple(int(v) for v in raw)
        except (TypeError, ValueError):
            return None
    return None


def _safe_id(stem: str) -> str:
    """Sanitise a definition stem into a stable, name-safe variant id."""
    return re.sub(r"[^0-9A-Za-z_]+", "_", stem.strip()) or "x"


def _clean_marking_name(stem: str) -> str:
    """Turn ``bodyMarking_bar001_stor`` into ``Body Marking Bar001 Stor``."""
    s = re.sub(r"[_]+", " ", stem)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    return s.strip().title()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ─── Sidecar serialisation ───────────────────────────────────────────

def _category_to_dict(cat: VariantCategory) -> dict[str, Any]:
    if cat.kind == KIND_PARAMETRIC_HSV:
        return {
            "kind": cat.kind,
            "entries": [_skin_tone_to_dict(t) for t in cat.entries],
            "applies_to_materials": list(cat.applies_to_materials),
        }
    if cat.kind == KIND_PARAMETRIC_COLOR:
        return {
            "kind": cat.kind,
            "entries": [_hair_color_to_dict(h) for h in cat.entries],
            "applies_to_materials": list(cat.applies_to_materials),
        }
    return {
        "kind": KIND_IMAGE_SWAP,
        "entries": [_variant_to_dict(e) for e in cat.entries],
    }


def _hair_color_to_dict(hc: HairColor) -> dict[str, Any]:
    """Serialise a :class:`HairColor` to the ``parametric_color`` entry shape.

    ``ui_color`` is ``rgbaColors[0]`` — the dropdown swatch and the MVP
    tint colour. ``secondary_color`` / ``tertiary_color`` /  ``colors2``
    are plumbed through for the deferred duotone work even though the
    MVP apply path uses only ``ui_color``.
    """
    return {
        "id": hc.id,
        "display_name": hc.display_name,
        "sort_order": hc.sort_order,
        "ui_color": list(hc.rgba_colors[0]),
        "secondary_color": list(hc.rgba_colors[1]),
        "tertiary_color": list(hc.rgba_colors[2]),
        "colors2": [list(c) for c in hc.rgba_colors2],
        "influence": hc.influence,
        "usable_by": list(hc.usable_by),
    }


def _skin_tone_to_dict(t: SkinTone) -> dict[str, Any]:
    return {
        "id": t.id,
        "display_name": t.label,
        "ui_color": list(t.ui_color),
        "ebase": t.ebase,
        "hue": t.hue,
        "saturation": t.saturation,
        "value": t.value,
        "darken": t.darken,
    }


def _variant_to_dict(e: VariantEntry) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": e.id,
        "display_name": e.display_name,
        "applies_to_material_idx": e.applies_to_material_idx,
        "target_role": e.target_role,
        "image_index": e.image_index,
        "image_name": e.variant_image_name,
        "source_path": e.texture.path,
    }
    if e.usable_by is not None:
        d["usable_by"] = list(e.usable_by)
    return d
