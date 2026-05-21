"""Decode CASC-extracted ``.tex`` payloads into PIL images.

The ``.tex`` payload is **mip 0 only**, stored as raw block-compressed
bytes with no header. Width/height/format come from the meta JSON
(already on the :class:`d4extract.formats.material_parser.TextureRef`).
We synthesise a DDS header around the bytes and let Pillow 12+ decode
it natively — no native dependencies.

Format mapping (verified by extracting per-model payloads and checking
sizes against ``width × height × bpp``, plus byte-pattern inspection
where multiple codecs share the same eTexFormat):

| eTexFormat | Codec  | bpp | Sample slot                              |
|------------|--------|-----|------------------------------------------|
| 9          | BC4    | 0.5 | SKIN_MASK / mask                         |
| 10         | BC1    | 0.5 | BASE_COLOR luminance (dye-system, hair)  |
| 41         | BC4    | 0.5 | ROUGHNESS / METALLIC / AO                |
| 42         | BC5    | 1.0 | NORMAL (XY; Z reconstructed below)       |
| 46         | BC1    | 0.5 | BASE_COLOR / EMISSIVE / TRANSLUCENCY     |
| 47         | BC1    | 0.5 | BASE_COLOR with 1-bit alpha (cloth)      |
| 49         | BC1/3  | mix | character & terrain colour; see notes    |
| 50         | BC7    | 1.0 | Gradient ramps, body markings, FX swatch |

Format 10 is the character-albedo variant: a single-channel luminance
map that the engine multiplies with a ``DyeRamp`` (slot 56) lookup at
runtime to get the final RGB color. Without dye wiring it decodes to
a grayscale image, which a downstream Blender importer can recolor.

Format 47 was originally guessed as BC2 (alpha channel) but the
extracted Goatman cloth color payload is exactly 0.5 bpp — i.e. BC1
with 1-bit punch-through alpha. (BC1 supports an alpha channel via a
two-color encoding; BC2 would have been 1.0 bpp.)

Format 49 is dual-codec: the same ``eTexFormat`` value is used for
two physical encodings, and the payload's byte count is the only
reliable disambiguator (see :func:`_resolve_codec` and
``docs/material_format_spec.md``):

- ``BC1`` (0.5 bpp) for character-body Colour textures — verified
  against ``S02_Boss_*_Color`` (1024² → 524 288 bytes, classic BC1
  endpoint+index byte pattern) and ``Goatman_Brute_Cloth_Frac_color``
  (1024×512 → 262 144 bytes). All cases sampled have
  ``dwMipMapLevelMin = 1`` in the meta.
- ``BC3`` (1.0 bpp) for character Emissive / Translucency and the
  layered terrain shader's BASE_COLOR slots — verified against
  ``S02_Boss_*_Emissive`` / ``_Translucency`` (512² → 262 144 bytes)
  and the original Tega_Terrain swatch. All cases sampled have
  ``dwMipMapLevelMin >= 2`` and the leading bytes look like BC3
  (alpha endpoints + 6-byte alpha index block).

Format 50 is BC7 — verified against ``bodyMarking_HED_bar023_stor``
(1024² → 1 048 576 bytes = 1 bpp, blocks lead with the BC7 mode 0
sentinel byte ``0x01``). Pillow decodes BC7 via the DDS ``DX10``
extended header rather than a 4-character FourCC, so the dispatch
table records the DXGI format ID and the wrapper picks the right
header variant.
"""

from __future__ import annotations

import io
import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

log = logging.getLogger(__name__)


# ─── Format table ────────────────────────────────────────────────────

@dataclass(frozen=True)
class TexCodec:
    """One row of the eTexFormat → BC-codec mapping.

    ``fourcc`` is the legacy 4-byte DDS FourCC used by the ``DDS``
    pixel format block — set to ``b"DX10"`` for codecs that need the
    DX10 extended header (currently only BC7).

    ``dxgi_format`` is the DXGI numeric ID used by the DX10 extended
    header, or ``0`` for codecs that are fully addressable via the
    legacy FourCC.
    """
    fourcc: bytes
    bytes_per_pixel: float
    name: str
    dxgi_format: int = 0


_DXGI_BC7_UNORM      = 98
_DXGI_BC7_UNORM_SRGB = 99


# Single-codec rows.
TEX_FORMATS: dict[int, TexCodec] = {
    9:  TexCodec(b"ATI1", 0.5, "BC4_UNORM (mask)"),
    # eTexFormat 10 was originally classified as BC4 single-channel
    # because we'd only seen it on character-body dye-luminance textures
    # where BC4 produces visually-equivalent results. Empirical re-check
    # against ``rogF_H07_Color`` (a hair texture) shows fmt 10 is actually
    # BC1 with mixed 4-color and 3-color+alpha blocks — Pillow's BC4
    # decode discards the alpha and the RGB endpoints, which is wrong
    # for any material that wants the alpha channel (Hero_Hair shader).
    # BC1 decode also works for body-luminance textures because the
    # encoder uses 4-color blocks throughout (alpha=255 everywhere) and
    # the resulting RGB averages to the same grayscale value the BC4
    # decode produced. So switching to BC1 is safe across both uses.
    10: TexCodec(b"DXT1", 0.5, "BC1_UNORM_SRGB (with alpha)"),
    41: TexCodec(b"ATI1", 0.5, "BC4_UNORM"),
    42: TexCodec(b"ATI2", 1.0, "BC5_UNORM (normal XY)"),
    46: TexCodec(b"DXT1", 0.5, "BC1_UNORM_SRGB"),
    47: TexCodec(b"DXT1", 0.5, "BC1_UNORM_SRGB (with alpha)"),
    # eTexFormat 49 is dual-codec — see :func:`_resolve_codec` for the
    # disambiguation. The dispatch-table row records the BC3 variant
    # (matching the original verified-against-terrain assumption); the
    # resolver downgrades to BC1 when the payload is exactly half the
    # BC3 size.
    49: TexCodec(b"DXT5", 1.0, "BC3_UNORM_SRGB (layered terrain)"),
    # eTexFormat 50 is BC7. Pillow requires the DDS DX10 extended
    # header for BC7, so the FourCC slot carries ``DX10`` and the
    # codec name is matched against ``dxgi_format`` instead. The
    # alpha channel is real (BC7 carries 8-bit alpha), so we keep it
    # for downstream glTF alpha-mode classification.
    50: TexCodec(b"DX10", 1.0, "BC7_UNORM_SRGB", _DXGI_BC7_UNORM_SRGB),
}


# eTexFormat 49 doubles as BC1 (character-body Colour textures) and
# BC3 (Emissive / Translucency / terrain). Pre-computing the BC1
# fallback row once means :func:`_resolve_codec` doesn't have to
# allocate on every call.
_FMT49_BC1_FALLBACK = TexCodec(
    b"DXT1", 0.5, "BC1_UNORM_SRGB (character body, fmt-49 variant)",
)


# Formats whose BC3 alpha channel encodes engine data (height /
# blend mask) rather than pixel transparency. ``decode_tex`` strips
# the alpha for these so glTF doesn't mark the material as
# alpha-tested.
_OPAQUE_BC3_FORMATS: frozenset[int] = frozenset({49})


def _resolve_codec(
    fmt: int, width: int, height: int, payload_size: int,
) -> TexCodec | None:
    """Pick the codec for a given ``eTexFormat`` and payload size.

    Most formats are 1:1. ``eTexFormat = 49`` is the exception: the
    same enum value is used for character-body Colour textures (BC1,
    0.5 bpp) and for Emissive / Translucency / terrain (BC3, 1.0 bpp).
    Inspecting the leading block bytes of cached payloads shows both
    encodings really do co-exist under fmt 49, so we disambiguate by
    payload size — BC1 is exactly half the BC3 byte count for any
    given dimensions, and the chance of a BC3-encoded payload landing
    on the BC1 size by accident is zero.
    """
    codec = TEX_FORMATS.get(fmt)
    if codec is None:
        return None
    if fmt != 49:
        return codec
    bc3_expected = width * height        # 1.0 bpp
    bc1_expected = bc3_expected // 2      # 0.5 bpp
    if payload_size == bc1_expected:
        return _FMT49_BC1_FALLBACK
    return codec


class TextureDecodeError(Exception):
    """Raised when a .tex payload cannot be decoded."""


# ─── DDS wrapping ────────────────────────────────────────────────────

# DDS_HEADER flag bits (from MS docs)
_DDSD_CAPS         = 0x00000001
_DDSD_HEIGHT       = 0x00000002
_DDSD_WIDTH        = 0x00000004
_DDSD_PIXELFORMAT  = 0x00001000
_DDSD_LINEARSIZE   = 0x00080000
_DDSCAPS_TEXTURE   = 0x00001000
_DDPF_FOURCC       = 0x00000004


def _build_dds_header(
    width: int, height: int, fourcc: bytes, payload_size: int,
    *, dxgi_format: int = 0,
) -> bytes:
    """Return a DDS header (magic + DDS_HEADER, plus DX10 ext if needed).

    Layout matches MSDN: 4-byte 'DDS ' magic, then 124-byte ``DDS_HEADER``
    consisting of 7 leading DWORDs, 11 reserved DWORDs, a 32-byte
    ``DDS_PIXELFORMAT``, then 5 trailing DWORDs (caps + reserved2). When
    ``fourcc == b"DX10"`` we append the 20-byte ``DDS_HEADER_DXT10``
    structure carrying the DXGI format ID — required for BC7 (Pillow
    only accepts BC7 via the DX10 path).
    """
    if len(fourcc) != 4:
        raise ValueError(f"FourCC must be 4 bytes, got {fourcc!r}")
    flags = _DDSD_CAPS | _DDSD_HEIGHT | _DDSD_WIDTH | _DDSD_PIXELFORMAT | _DDSD_LINEARSIZE
    pixelformat = struct.pack(
        "<II4sIIIII",
        32,                  # dwSize
        _DDPF_FOURCC,        # dwFlags
        fourcc,              # dwFourCC (4 ASCII bytes, not a uint32)
        0, 0, 0, 0, 0,       # masks unused with FourCC
    )
    header = (
        b"DDS "
        + struct.pack("<I", 124)            # dwSize
        + struct.pack("<I", flags)
        + struct.pack("<I", height)
        + struct.pack("<I", width)
        + struct.pack("<I", payload_size)   # dwPitchOrLinearSize
        + struct.pack("<I", 0)              # dwDepth (unused for 2D)
        + struct.pack("<I", 1)              # dwMipMapCount (we have only mip 0)
        + b"\x00" * 44                       # 11 reserved DWORDs
        + pixelformat
        + struct.pack("<I", _DDSCAPS_TEXTURE)
        + b"\x00" * 16                       # caps2..reserved2
    )
    if fourcc == b"DX10":
        # DDS_HEADER_DXT10: dxgiFormat, resourceDimension (3=TEXTURE2D),
        # miscFlag, arraySize, miscFlags2. BC7's DXGI IDs are 98/99.
        header += struct.pack("<IIIII", dxgi_format, 3, 0, 1, 0)
    return header


# ─── Public API ──────────────────────────────────────────────────────

def decode_tex(
    payload_path: Path,
    width: int,
    height: int,
    fmt: int,
) -> "PILImage":
    """Decompress a ``.tex`` payload's mip 0 into a PIL :class:`Image`.

    ``fmt`` is the ``eTexFormat`` enum (see :data:`TEX_FORMATS`).
    Returns the image in whichever mode Pillow's DDS decoder produces:
    ``L`` for BC4, ``RGBA`` for BC1, ``RGB`` for BC5 (with B=0 — call
    :func:`reconstruct_normal_z` if you need a glTF-ready normal map).

    Raises :class:`TextureDecodeError` for unknown formats, missing
    files, or size mismatches; callers should fall back to the
    factor-only material in that case.
    """
    from PIL import Image  # local import keeps module load fast

    payload_path = Path(payload_path)
    if not payload_path.is_file():
        raise TextureDecodeError(f"Texture payload not found: {payload_path}")

    raw = payload_path.read_bytes()
    codec = _resolve_codec(fmt, width, height, len(raw))
    if codec is None:
        raise TextureDecodeError(
            f"Unsupported eTexFormat: {fmt} (file {payload_path.name}). "
            f"Known formats: {sorted(TEX_FORMATS)}"
        )

    expected = int(width * height * codec.bytes_per_pixel)
    if len(raw) != expected:
        # Diablo IV's payload contains exactly mip 0; if the size doesn't
        # match, either the meta dimensions are wrong or this is a
        # streaming-mip leftover. Continue with whatever's there but warn.
        log.warning(
            "Size mismatch for %s: got %d bytes, expected %d (%dx%d %s)",
            payload_path.name, len(raw), expected, width, height, codec.name,
        )

    dds = _build_dds_header(
        width, height, codec.fourcc, len(raw),
        dxgi_format=codec.dxgi_format,
    ) + raw
    try:
        img = Image.open(io.BytesIO(dds))
        img.load()
    except Exception as exc:
        raise TextureDecodeError(
            f"Pillow rejected DDS-wrapped {codec.name} for "
            f"{payload_path.name} ({width}x{height}): {exc}"
        ) from exc

    # BC3-decoded layered terrain colors come back as RGBA, but the
    # alpha channel here is a runtime blend/height mask — not pixel
    # transparency. Stripping it now means the glTF exporter's
    # alpha-mode heuristic (which keys off ``min(alpha) < 255``) sees
    # an opaque RGB image and leaves the material's alphaMode at
    # OPAQUE rather than misclassifying it as MASK / BLEND. The
    # ``codec.fourcc == b"DXT5"`` gate keeps us from stripping the
    # *real* 1-bit alpha that comes back when fmt=49 resolves to BC1
    # for a character-body Colour texture.
    if (
        fmt in _OPAQUE_BC3_FORMATS
        and codec.fourcc == b"DXT5"
        and img.mode == "RGBA"
    ):
        img = img.convert("RGB")
    return img


def reconstruct_normal_z(normal_img: "PILImage") -> "PILImage":
    """Rebuild the Z channel of a BC5 normal map and convert DX→GL.

    BC5 stores only X and Y in the R and G channels (B is zero). For a
    glTF normalTexture we need a full RGB image where Z is reconstructed
    from the unit-vector constraint ``Z = sqrt(1 - X² - Y²)``.

    D4 stores tangent-space normals in **DirectX convention** (Y points
    "down" in UV space), but glTF / OpenGL renderers expect Y pointing
    "up" — without the conversion Blender requires switching the
    Normal Map node from "OpenGL" to "DirectX" on every imported
    material. We flip the green channel here (``G' = 255 - G``) so the
    output is glTF-ready by default.

    Flipping is applied **before** the SNORM decode, so Z is derived
    from the corrected Y. (Mathematically the order doesn't matter:
    flipping Y changes its sign but ``Y²`` is preserved, so Z is
    unchanged either way — but the spec wants the flip-first ordering
    for clarity.)

    Input expected mode: ``RGB`` with B≈0.
    Output mode: ``RGB``, glTF-ready.
    """
    import numpy as np
    from PIL import Image

    if normal_img.mode != "RGB":
        normal_img = normal_img.convert("RGB")

    arr = np.asarray(normal_img, dtype=np.float32)
    # DX → GL: invert the green channel BEFORE decoding to SNORM.
    g_flipped = 255.0 - arr[..., 1]
    # Map [0, 255] → [-1, 1]
    nx = arr[..., 0] / 127.5 - 1.0
    ny = g_flipped / 127.5 - 1.0
    nz_sq = 1.0 - nx * nx - ny * ny
    nz = np.sqrt(np.clip(nz_sq, 0.0, 1.0))
    # Map back to [0, 255]
    out = np.stack([nx, ny, nz], axis=-1)
    out = ((out + 1.0) * 127.5).clip(0.0, 255.0).astype(np.uint8)
    return Image.fromarray(out, "RGB")


def combine_metallic_roughness(
    *,
    roughness_img: "PILImage | None",
    metallic_img: "PILImage | None",
) -> "PILImage":
    """Pack BC4 roughness + metallic into a glTF metallicRoughnessTexture.

    glTF convention: ``G = roughness``, ``B = metallic``, ``R`` unused
    (we leave it 255 so an integrator that misreads R as AO doesn't
    accidentally darken the surface). Both inputs must be ``L``-mode
    Pillow images; if their resolutions differ the metallic image is
    upscaled with bilinear filtering to match the roughness.

    At least one of the two inputs must be non-None.
    """
    from PIL import Image

    if roughness_img is None and metallic_img is None:
        raise TextureDecodeError(
            "combine_metallic_roughness needs at least one input"
        )

    # Resolve target size from the larger input — roughness usually wins
    # because metallic is often half-resolution (Goatman: 1024² vs 512²).
    sizes: list[tuple[int, int]] = []
    if roughness_img is not None:
        sizes.append(roughness_img.size)
    if metallic_img is not None:
        sizes.append(metallic_img.size)
    target = max(sizes, key=lambda s: s[0] * s[1])

    def _ensure_l(img: "PILImage | None", default: int) -> "PILImage":
        if img is None:
            return Image.new("L", target, default)
        if img.mode != "L":
            img = img.convert("L")
        if img.size != target:
            img = img.resize(target, Image.Resampling.BILINEAR)
        return img

    g = _ensure_l(roughness_img, 255)
    b = _ensure_l(metallic_img, 0)
    r = Image.new("L", target, 255)  # unused channel — neutral white
    return Image.merge("RGB", (r, g, b))


def to_png_bytes(img: "PILImage") -> bytes:
    """Encode a Pillow image to PNG bytes ready for glTF embedding."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def image_min_alpha(img: "PILImage") -> int | None:
    """Return the minimum alpha-channel value, or ``None`` for opaque modes.

    For ``RGBA`` images returns ``min(alpha) ∈ [0, 255]``. For ``RGB``,
    ``L``, or any other mode without an alpha channel returns ``None`` —
    callers should treat that as fully opaque.

    Used by the glTF exporter to choose between ``alphaMode="OPAQUE"``
    and ``alphaMode="MASK"`` based on the BC1 punch-through alpha bit:
    a strictly opaque RGBA image yields 255 (no transparent texels)
    and the exporter leaves the material opaque; any value below 255
    triggers MASK + ``alphaCutoff=0.5`` + ``doubleSided=True``.
    """
    if img.mode != "RGBA":
        return None
    alpha = img.getchannel("A")
    return alpha.getextrema()[0]


# ─── Path resolution ─────────────────────────────────────────────────

def texture_payload_path(texture_dir: Path, casc_path: str) -> Path:
    """Map a ``base/meta/Texture/<name>.tex`` reference to a payload path.

    ``texture_dir`` is the root directory the user pointed
    ``--texture-dir`` at — typically ``samples/`` containing the
    ``base/payload/Texture/`` subtree. We translate ``meta`` to
    ``payload`` in the relative path and append ``.tex`` semantics
    (the ``__targetFileName__`` already ends with ``.tex``).
    """
    rel = casc_path.replace("base/meta/Texture/", "base/payload/Texture/", 1)
    return Path(texture_dir) / rel
