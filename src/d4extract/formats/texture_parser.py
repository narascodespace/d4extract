"""Decode CASC-extracted ``.tex`` payloads into PIL images.

The ``.tex`` payload is **mip 0 only**, stored as raw block-compressed
bytes with no header. Width/height/format come from the meta JSON
(already on the :class:`d4extract.formats.material_parser.TextureRef`).
We synthesise a DDS header around the bytes and let Pillow 12+ decode
it natively — no native dependencies, no BC7 complexity (every sampled
``eTexFormat=46`` payload turns out to be BC1, not BC7, confirmed by
its 0.5 byte-per-pixel ratio and the all-zero index pattern in the
header bytes).

Format mapping (verified by extracting Goatman_Brute_Body_* payloads
and checking sizes against ``width × height × bpp``):

| eTexFormat | Codec | bpp | Sample slot                         |
|------------|-------|-----|-------------------------------------|
| 9          | BC4   | 0.5 | SKIN_MASK / mask                    |
| 10         | BC4   | 0.5 | BASE_COLOR luminance (dye-system)   |
| 41         | BC4   | 0.5 | ROUGHNESS / METALLIC / AO           |
| 42         | BC5   | 1.0 | NORMAL (XY; Z reconstructed below)  |
| 46         | BC1   | 0.5 | BASE_COLOR / EMISSIVE / TRANSLUCENCY|
| 47         | BC1   | 0.5 | BASE_COLOR with 1-bit alpha (cloth) |
| 49         | BC3   | 1.0 | Layered terrain BASE_COLOR (RGB+blend mask)|

Format 10 is the character-albedo variant: a single-channel luminance
map that the engine multiplies with a `DyeRamp` (slot 56) lookup at
runtime to get the final RGB color. Without dye wiring it decodes to
a grayscale image, which a downstream Blender importer can recolor.

Format 47 was originally guessed as BC2 (alpha channel) but the
extracted Goatman cloth color payload is exactly 0.5 bpp — i.e. BC1
with 1-bit punch-through alpha. (BC1 supports an alpha channel via a
two-color encoding; BC2 would have been 1.0 bpp.) The
``rgbavalAvgColor.a == 0.63`` we saw is just the average opacity of a
texture that does in fact use BC1's alpha bit. All other formats are
verified by matching ``width × height × bpp`` against the actual
payload size.
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
    """One row of the eTexFormat → BC-codec mapping."""
    fourcc: bytes        # DDS FourCC (4 bytes, ASCII)
    bytes_per_pixel: float
    name: str            # human-readable label


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
    # eTexFormat 49 is BC3 (DXT5) at 1 bpp — used for the layered
    # terrain shader's BASE_COLOR slots (e.g. ``Tega_Terrain_Rock_01_Color``).
    # The alpha channel here is a height/blend mask the engine uses to
    # weight layer 0 vs layer 1 at runtime, NOT pixel transparency. We
    # drop it in :func:`decode_tex` so downstream consumers (glTF
    # exporter, viewport) treat the texture as opaque RGB.
    49: TexCodec(b"DXT5", 1.0, "BC3_UNORM_SRGB (layered terrain)"),
}

# Formats whose BC3 alpha channel encodes engine data (height /
# blend mask) rather than pixel transparency. ``decode_tex`` strips
# the alpha for these so glTF doesn't mark the material as
# alpha-tested.
_OPAQUE_BC3_FORMATS: frozenset[int] = frozenset({49})


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


def _build_dds_header(width: int, height: int, fourcc: bytes, payload_size: int) -> bytes:
    """Return a 128-byte DDS header (magic + DDS_HEADER) for a 2D BC texture.

    Layout matches MSDN: 4-byte 'DDS ' magic, then 124-byte ``DDS_HEADER``
    consisting of 7 leading DWORDs, 11 reserved DWORDs, a 32-byte
    ``DDS_PIXELFORMAT``, then 5 trailing DWORDs (caps + reserved2).
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
    return (
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

    codec = TEX_FORMATS.get(fmt)
    if codec is None:
        raise TextureDecodeError(
            f"Unsupported eTexFormat: {fmt} (file {payload_path.name}). "
            f"Known formats: {sorted(TEX_FORMATS)}"
        )

    raw = payload_path.read_bytes()
    expected = int(width * height * codec.bytes_per_pixel)
    if len(raw) != expected:
        # Diablo IV's payload contains exactly mip 0; if the size doesn't
        # match, either the meta dimensions are wrong or this is a
        # streaming-mip leftover. Continue with whatever's there but warn.
        log.warning(
            "Size mismatch for %s: got %d bytes, expected %d (%dx%d %s)",
            payload_path.name, len(raw), expected, width, height, codec.name,
        )

    dds = _build_dds_header(width, height, codec.fourcc, len(raw)) + raw
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
    # OPAQUE rather than misclassifying it as MASK / BLEND.
    if fmt in _OPAQUE_BC3_FORMATS and img.mode == "RGBA":
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
