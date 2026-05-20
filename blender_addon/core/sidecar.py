"""Reading the d4extract ``.materials.json`` sidecar and the .glb itself.

This module is deliberately ``bpy``-free so its parsing can be unit
tested headlessly. The Blender-facing work — turning the parsed data
into image datablocks and node graphs — lives in the operators and the
other ``core`` modules.

The sidecar schema is produced by
``d4extract/src/d4extract/formats/material_parser.py`` and (for the
``variants`` block) ``formats/variants.py``.

**Variant-block compatibility.** Current exports write each variant
category as a typed block — ``{"kind":
"image_swap"|"parametric_hsv"|"parametric_color", "entries": [...]}``.
Pre-schema-change exports wrote each category as a flat array of
image-swap entries. :func:`_parse_variant_category` detects the
flat-array shape and treats it as ``image_swap`` implicitly, so old
``.glb`` sidecars keep importing cleanly; an unknown ``kind`` likewise
falls back to ``image_swap``.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# glb chunk type tags (little-endian uint32 of the 4 ASCII bytes).
_GLB_MAGIC = 0x46546C67          # "glTF"
_CHUNK_JSON = 0x4E4F534A         # "JSON"
_CHUNK_BIN = 0x004E4942          # "BIN\0"


# ─── Parsed sidecar models ───────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SidecarTexture:
    """One texture binding inside a sidecar material.

    ``avg_rgba`` is the texture payload's pre-decoded average colour
    (4-tuple of floats in [0, 1]), the same value the exporter records
    on its :class:`d4extract.formats.material_parser.TextureRef`. It
    powers texture-flavour classification on the addon side (e.g.
    "near-white head hair vs. near-black facial hair" for the hair
    tint chain). Defaults to opaque white so legacy sidecars or
    hand-built fixtures that omit the field stay safe.
    """

    role: str
    slot: int
    sno_id: int
    path: str
    width: int
    height: int
    format: int
    avg_rgba: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)


@dataclass(frozen=True, slots=True)
class SidecarMaterial:
    """One resolved material — matches a glTF material by ``name``."""

    name: str
    sno_id: int
    shader_map: str
    base_color_factor: tuple[float, float, float, float]
    emissive_factor: tuple[float, float, float]
    metallic_factor: float
    roughness_factor: float
    is_cloth_only: bool
    textures: tuple[SidecarTexture, ...]

    def texture_for_role(self, role: str) -> SidecarTexture | None:
        for t in self.textures:
            if t.role == role:
                return t
        return None


@dataclass(frozen=True, slots=True)
class SidecarVariant:
    """One customization-variant option for a dropdown."""

    kind: str
    id: str
    display_name: str
    applies_to_material_idx: int
    target_role: str
    image_index: int
    image_name: str
    source_path: str
    usable_by: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class SidecarSkinTone:
    """One parametric skin-tone option (``parametric_hsv`` category)."""

    id: str
    display_name: str
    ui_color: tuple[float, float, float, float]
    ebase: int
    hue: float
    saturation: float
    value: float
    darken: float


@dataclass(frozen=True, slots=True)
class SidecarHairColor:
    """One parametric hair-colour option (``parametric_color`` category).

    ``ui_color`` is the primary RGBA tint (``rgbaColors[0]``) — the
    dropdown swatch and the MVP tint colour. ``secondary_color`` /
    ``tertiary_color`` / ``colors2`` are carried through for the
    deferred duotone work; the MVP apply path uses ``ui_color`` only.
    """

    id: str
    display_name: str
    sort_order: int
    ui_color: tuple[float, float, float, float]
    secondary_color: tuple[float, float, float, float]
    tertiary_color: tuple[float, float, float, float]
    colors2: tuple[tuple[float, float, float, float], ...]
    influence: float
    usable_by: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SidecarVariantCategory:
    """One dropdown category — a ``kind`` plus its parsed entries.

    ``image_entries`` is populated for ``image_swap`` categories;
    ``skin_entries`` + ``applies_to_materials`` for ``parametric_hsv``;
    ``hair_entries`` + ``applies_to_materials`` for ``parametric_color``.
    """

    kind: str
    image_entries: tuple[SidecarVariant, ...] = ()
    skin_entries: tuple[SidecarSkinTone, ...] = ()
    hair_entries: tuple[SidecarHairColor, ...] = ()
    applies_to_materials: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Sidecar:
    """A fully parsed ``<model>.materials.json``."""

    path: str
    materials: tuple[SidecarMaterial, ...]
    variants: dict[str, SidecarVariantCategory]

    def material_by_name(self, name: str) -> SidecarMaterial | None:
        for m in self.materials:
            if m.name == name:
                return m
        return None

    def body_skin_materials(self) -> tuple[SidecarMaterial, ...]:
        """Body skin materials — ``hero_opaque_skin`` shader with SKIN_MASK.

        D4's head and body skin both use the ``hero_opaque_skin`` shader;
        only the **body** carries a ``SKIN_MASK`` texture (slot 145), so
        SKIN_MASK presence is the head/body discriminator. Normally one
        entry; more than one means a multi-region character (the caller
        picks the first and logs).
        """
        return tuple(
            m for m in self.materials
            if m.shader_map.lower().startswith("hero_opaque_skin")
            and m.texture_for_role("SKIN_MASK") is not None
        )


# ─── Sidecar loading (cached by path + mtime) ────────────────────────

# Module-level cache so the N-panel's enum callbacks — which fire on
# every redraw — do not re-read and re-parse the JSON each frame.
_SIDECAR_CACHE: dict[str, tuple[float, Sidecar]] = {}


def sidecar_path_for(glb_path: str | Path) -> Path:
    """``foo.glb`` → ``foo.materials.json`` (alongside the .glb)."""
    return Path(glb_path).with_suffix(".materials.json")


def load_sidecar(
    glb_path: str | Path, override: str | Path | None = None,
) -> Sidecar | None:
    """Load and parse the sidecar for ``glb_path``.

    ``override`` — when set to a non-empty path, the sidecar is loaded
    from there instead of from next to the .glb, and its JSON shape is
    validated (see :func:`_looks_like_sidecar`) so pointing the field
    at the wrong file fails cleanly — ``None`` — rather than yielding a
    silently empty material set. When ``override`` is ``None`` or empty
    the sidecar is read from ``<glb>.materials.json``.

    Returns ``None`` when no sidecar exists or it cannot be parsed.
    Results are cached on the sidecar's ``(path, mtime)``.
    """
    if override:
        return load_sidecar_file(override, require_shape=True)
    return load_sidecar_file(sidecar_path_for(glb_path))


def load_sidecar_file(
    sidecar_path: str | Path, require_shape: bool = False,
) -> Sidecar | None:
    """Load and parse a ``.materials.json`` file directly.

    Used when the path is already known (e.g. the ``_d4_sidecar_path``
    custom property an import stamps onto the armature). Same
    ``(path, mtime)`` caching as :func:`load_sidecar`.

    ``require_shape`` — when ``True``, the parsed JSON must look like a
    materials sidecar (a ``materials`` list); otherwise ``None`` is
    returned. Used for the user-supplied override path.
    """
    side = Path(sidecar_path)
    if not side.is_file():
        return None
    key = str(side)
    try:
        mtime = side.stat().st_mtime
    except OSError:
        return None

    cached = _SIDECAR_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        with open(side, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    if require_shape and not _looks_like_sidecar(data):
        return None

    parsed = _parse_sidecar(key, data)
    _SIDECAR_CACHE[key] = (mtime, parsed)
    return parsed


def _looks_like_sidecar(data: Any) -> bool:
    """Whether ``data`` has the shape of a d4extract materials sidecar.

    The minimum contract is a JSON object with a ``materials`` array;
    the ``variants`` block is optional.
    """
    return isinstance(data, dict) and isinstance(data.get("materials"), list)


def _parse_sidecar(path: str, data: dict[str, Any]) -> Sidecar:
    materials = tuple(
        _parse_material(m) for m in data.get("materials", [])
    )
    variants: dict[str, SidecarVariantCategory] = {}
    for kind, block in (data.get("variants") or {}).items():
        variants[kind] = _parse_variant_category(kind, block)
    return Sidecar(path=path, materials=materials, variants=variants)


def _parse_variant_category(kind: str, block: Any) -> SidecarVariantCategory:
    """Parse one variant category, tolerating the legacy flat-array shape."""
    # Legacy: pre-schema-change exports wrote a bare list of image-swap
    # entries. Treat that as an ``image_swap`` category.
    if isinstance(block, list):
        return SidecarVariantCategory(
            kind="image_swap",
            image_entries=tuple(_parse_variant(kind, e) for e in block),
        )
    if isinstance(block, dict):
        cat_kind = block.get("kind", "image_swap")
        entries = block.get("entries", []) or []
        if cat_kind == "parametric_hsv":
            return SidecarVariantCategory(
                kind="parametric_hsv",
                skin_entries=tuple(_parse_skin_tone(e) for e in entries),
                applies_to_materials=tuple(
                    int(i) for i in block.get("applies_to_materials", [])
                ),
            )
        if cat_kind == "parametric_color":
            return SidecarVariantCategory(
                kind="parametric_color",
                hair_entries=tuple(_parse_hair_color(e) for e in entries),
                applies_to_materials=tuple(
                    int(i) for i in block.get("applies_to_materials", [])
                ),
            )
        return SidecarVariantCategory(
            kind="image_swap",
            image_entries=tuple(_parse_variant(kind, e) for e in entries),
        )
    return SidecarVariantCategory(kind="image_swap")


def _rgba(value: Any, default: tuple[float, float, float, float]
          ) -> tuple[float, float, float, float]:
    """Coerce a JSON list into a 4-float RGBA tuple, defensively."""
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return default
    return (
        float(value[0]), float(value[1]), float(value[2]),
        float(value[3]) if len(value) > 3 else 1.0,
    )


def _parse_hair_color(e: dict[str, Any]) -> SidecarHairColor:
    """Parse one ``parametric_color`` entry into a :class:`SidecarHairColor`.

    Validates field types and converts every nested list to a tuple so
    the frozen-slots record stays hashable and immutable.
    """
    colors2_raw = e.get("colors2") or []
    colors2 = tuple(
        _rgba(c, (0.0, 0.0, 0.0, 1.0))
        for c in colors2_raw
        if isinstance(c, (list, tuple))
    )
    usable = e.get("usable_by")
    return SidecarHairColor(
        id=str(e.get("id", "")),
        display_name=str(e.get("display_name", e.get("id", ""))),
        sort_order=int(e.get("sort_order", 0)),
        ui_color=_rgba(e.get("ui_color"), (1.0, 1.0, 1.0, 1.0)),
        secondary_color=_rgba(e.get("secondary_color"), (1.0, 1.0, 1.0, 1.0)),
        tertiary_color=_rgba(e.get("tertiary_color"), (1.0, 1.0, 1.0, 1.0)),
        colors2=colors2,
        influence=float(e.get("influence", 1.0)),
        usable_by=(
            tuple(int(v) for v in usable)
            if isinstance(usable, list) else ()
        ),
    )


def _parse_skin_tone(e: dict[str, Any]) -> SidecarSkinTone:
    col = e.get("ui_color") or [1.0, 1.0, 1.0, 1.0]
    return SidecarSkinTone(
        id=str(e.get("id", "")),
        display_name=str(e.get("display_name", e.get("id", ""))),
        ui_color=(
            float(col[0]), float(col[1]), float(col[2]),
            float(col[3]) if len(col) > 3 else 1.0,
        ),
        ebase=int(e.get("ebase", 0)),
        hue=float(e.get("hue", 0.0)),
        saturation=float(e.get("saturation", 0.0)),
        value=float(e.get("value", 0.0)),
        darken=float(e.get("darken", 1.0)),
    )


def _avg_rgba(
    raw: Any, default: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0),
) -> tuple[float, float, float, float]:
    """Coerce a JSON ``avg_rgba`` value (list or tuple) to a 4-float tuple.

    Missing / malformed values fall back to opaque white — safe because
    the consumers (e.g. :func:`material_builder._hair_base_color_is_dark`)
    treat ``1.0`` as the "no special handling" case.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return default
    try:
        return (
            float(raw[0]), float(raw[1]), float(raw[2]),
            float(raw[3]) if len(raw) > 3 else 1.0,
        )
    except (TypeError, ValueError):
        return default


def _parse_material(m: dict[str, Any]) -> SidecarMaterial:
    bcf = m.get("base_color_factor") or [1.0, 1.0, 1.0, 1.0]
    emf = m.get("emissive_factor") or [0.0, 0.0, 0.0]
    textures = tuple(
        SidecarTexture(
            role=t.get("role", ""),
            slot=int(t.get("slot", -1)),
            sno_id=int(t.get("sno_id", 0)),
            path=t.get("path", ""),
            width=int(t.get("width", 0)),
            height=int(t.get("height", 0)),
            format=int(t.get("format", 0)),
            avg_rgba=_avg_rgba(t.get("avg_rgba")),
        )
        for t in m.get("textures", [])
    )
    return SidecarMaterial(
        name=m.get("name", ""),
        sno_id=int(m.get("sno_id", 0)),
        shader_map=m.get("shader_map", ""),
        base_color_factor=(
            float(bcf[0]), float(bcf[1]), float(bcf[2]),
            float(bcf[3]) if len(bcf) > 3 else 1.0,
        ),
        emissive_factor=(float(emf[0]), float(emf[1]), float(emf[2])),
        metallic_factor=float(m.get("metallic_factor", 1.0)),
        roughness_factor=float(m.get("roughness_factor", 1.0)),
        is_cloth_only=bool(m.get("is_cloth_only", False)),
        textures=textures,
    )


def _parse_variant(kind: str, e: dict[str, Any]) -> SidecarVariant:
    usable = e.get("usable_by")
    return SidecarVariant(
        kind=kind,
        id=str(e.get("id", "")),
        display_name=str(e.get("display_name", e.get("id", ""))),
        applies_to_material_idx=int(e.get("applies_to_material_idx", -1)),
        target_role=str(e.get("target_role", "")),
        image_index=int(e.get("image_index", -1)),
        image_name=str(e.get("image_name", f"variant_{kind}_{e.get('id', '')}")),
        source_path=str(e.get("source_path", "")),
        usable_by=tuple(int(v) for v in usable) if isinstance(usable, list) else None,
    )


def invalidate_cache() -> None:
    """Drop the parsed-sidecar cache (used on reload / re-import)."""
    _SIDECAR_CACHE.clear()


# ─── .glb container parsing ──────────────────────────────────────────

def read_glb(glb_path: str | Path) -> tuple[dict[str, Any], bytes]:
    """Parse a binary glTF into ``(json, bin_blob)``.

    Raises :class:`ValueError` if the file is not a valid glb. The BIN
    chunk is returned as raw bytes; an empty ``bytes`` when absent.
    """
    raw = Path(glb_path).read_bytes()
    if len(raw) < 12:
        raise ValueError(f"{glb_path}: too short to be a glb")
    magic, version, total = struct.unpack_from("<III", raw, 0)
    if magic != _GLB_MAGIC:
        raise ValueError(f"{glb_path}: not a glb (bad magic)")

    gltf_json: dict[str, Any] | None = None
    bin_blob = b""
    offset = 12
    while offset + 8 <= len(raw):
        chunk_len, chunk_type = struct.unpack_from("<II", raw, offset)
        offset += 8
        chunk = raw[offset:offset + chunk_len]
        offset += chunk_len
        if chunk_type == _CHUNK_JSON:
            gltf_json = json.loads(chunk.decode("utf-8"))
        elif chunk_type == _CHUNK_BIN:
            bin_blob = chunk

    if gltf_json is None:
        raise ValueError(f"{glb_path}: no JSON chunk")
    return gltf_json, bin_blob


def image_png_bytes(
    gltf_json: dict[str, Any], bin_blob: bytes, image_index: int,
) -> bytes | None:
    """Slice the PNG bytes for ``images[image_index]`` out of the buffer.

    Returns ``None`` when the index is out of range or the image is not
    a bufferView-backed PNG (e.g. an external-URI image).
    """
    images = gltf_json.get("images") or []
    if not (0 <= image_index < len(images)):
        return None
    img = images[image_index]
    bv_index = img.get("bufferView")
    if bv_index is None:
        return None
    views = gltf_json.get("bufferViews") or []
    if not (0 <= bv_index < len(views)):
        return None
    bv = views[bv_index]
    start = int(bv.get("byteOffset", 0))
    length = int(bv.get("byteLength", 0))
    if start + length > len(bin_blob):
        return None
    return bin_blob[start:start + length]
