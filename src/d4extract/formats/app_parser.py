"""Parser for Diablo IV .app model files.

Authoritative format reference: docs/app_format_spec.md sections 9.1-9.6
(VertexElem / GeoChunkVertexBuffer / GeoChunk / SubObject / SubObjectSegment).
Sections 2-3 of that document predate the d4data cross-reference and are
superseded wherever they conflict with section 9.

The .app format stores a model across two paired files:
    Meta:    base/meta/Appearance/<name>.app
    Payload: base/payload/Appearance/<name>.app
linked by `meta[0x10] == payload[0x00]`.

Two vertex buffer formats are observed across all sampled appearance files:

    Stride 36 (eVBFormat=4, static):
        +0   POSITION    eFormat 1   R32G32B32_FLOAT (12B)
        +12  NORMAL      eFormat 8   packed SNORM4   (4B)
        +16  COLOR_0     eFormat 5   R8G8B8A8_UNORM  (4B)
        +20  COLOR_1     eFormat 5   R8G8B8A8_UNORM  (4B)
        +24  TEXCOORD_0  eFormat 7   R16G16_FLOAT    (4B)
        +28  TEXCOORD_1  eFormat 7   R16G16_FLOAT    (4B)
        +32  TANGENT     eFormat 8   packed SNORM4   (4B)

    Stride 44 (eVBFormat=6, skinned):
        +0   POSITION    eFormat 1   R32G32B32_FLOAT (12B)
        +12  NORMAL      eFormat 8   packed SNORM4   (4B)
        +16  TANGENT     eFormat 8   packed SNORM4   (4B)   <-- ahead of colors
        +20  COLOR_0     eFormat 5   R8G8B8A8_UNORM  (4B)
        +24  COLOR_1     eFormat 5   R8G8B8A8_UNORM  (4B)
        +28  TEXCOORD_0  eFormat 7   R16G16_FLOAT    (4B)
        +32  TEXCOORD_1  eFormat 7   R16G16_FLOAT    (4B)
        +36  JOINTS_0    eFormat 4   R8G8B8A8_UINT   (4B)
        +40  WEIGHTS_0   eFormat 5   R8G8B8A8_UNORM  (4B)

The attribute order differs between the two strides (TANGENT moves from
offset 32 to offset 16); offsets are never inferred from `stride - 4`.

Decoding is layout-driven: each VertexLayout carries an explicit list of
VertexElem(semantic, format, offset) records, and one decoder per eFormat
walks the buffer.  Layouts can be supplied externally, read from the meta
file when the canonical signature is found, or fall back to the table
above (verified against 619/621 sampled .app.json files).

Coordinate system: D4 uses left-handed Z-up; glTF uses right-handed Y-up.
The parser preserves D4-native coordinates and exposes
`convert_to_gltf_coords()` for callers that want the converted values.
The bundled exporter applies the conversion when its
`coordinate_transform` parameter is set to `"z_up_to_y_up"` or `"auto"`.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .hash_names import resolve_hash

log = logging.getLogger(__name__)

# ─── Format constants ────────────────────────────────────────────────

APP_META_MAGIC = 0xDEADBEEF
INDEX_FORMAT_U16 = 2
INDEX_FORMAT_U32 = 4

VERTEX_STRIDE_SIMPLE = 36   # eVBFormat=4, static
VERTEX_STRIDE_SKINNED = 44  # eVBFormat=6, skinned
SUPPORTED_STRIDES = [VERTEX_STRIDE_SIMPLE, VERTEX_STRIDE_SKINNED]

# pnVertexElemPerSemantic length identifies the buffer format:
#   11 entries -> stride 36, 13 entries -> stride 44.
# This is what the FindSubMesh sentinel-prefixed uint32 actually counts;
# it is NOT the number of VertexElem records (which is 7 vs 9).
ATTRIB_COUNT_TO_STRIDE = {11: VERTEX_STRIDE_SIMPLE, 13: VERTEX_STRIDE_SKINNED}

# eSemantic values (docs/app_format_spec.md § 9.2)
SEM_POSITION = 0
SEM_TEXCOORD_0 = 1
SEM_TEXCOORD_1 = 2
SEM_COLOR_0 = 7
SEM_COLOR_1 = 8
SEM_NORMAL = 9
SEM_TANGENT = 10
SEM_BLENDINDICES = 11
SEM_BLENDWEIGHTS = 12

# eFormat values (docs/app_format_spec.md § 9.3)
FMT_R32G32B32_FLOAT = 1
FMT_R16G16_SNORM = 2
FMT_R8G8B8A8_UINT = 4
FMT_R8G8B8A8_UNORM = 5
FMT_R16G16_FLOAT = 7
FMT_PACKED_SNORM4 = 8

FORMAT_BYTE_SIZES: dict[int, int] = {
    FMT_R32G32B32_FLOAT: 12,
    FMT_R16G16_SNORM:     4,
    FMT_R8G8B8A8_UINT:    4,
    FMT_R8G8B8A8_UNORM:   4,
    FMT_R16G16_FLOAT:     4,
    FMT_PACKED_SNORM4:    4,
}

# 19-byte FindSubMesh sentinel (Durik256 AppToOBJ.exe).  The byte at
# pos-1 plus this run forms (attrib_count uint32, 0xFFFFFFFF, 12 zeros)
# that prefaces a per-LOD descriptor block in the meta file.
_FIND_SUBMESH_PATTERN = (
    b"\x00\x00\x00"
    b"\xFF\xFF\xFF\xFF"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
)

# Signature of the first VertexElem in any layout: (semantic=0, format=1,
# offset=0) serialized as three little-endian uint32s.
_VERTEX_ELEM_SIGNATURE = b"\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00"


# ─── Vertex layout types ─────────────────────────────────────────────

@dataclass(frozen=True)
class VertexElem:
    """One entry from GeoChunkVertexBuffer.ptVertexElems."""
    semantic: int
    format: int
    offset: int

    @property
    def size(self) -> int:
        return FORMAT_BYTE_SIZES[self.format]


@dataclass(frozen=True)
class VertexLayout:
    """Vertex buffer layout: stride + ordered VertexElems."""
    stride: int
    elems: tuple[VertexElem, ...]
    vb_format: int = 0

    def find(self, semantic: int) -> VertexElem | None:
        for e in self.elems:
            if e.semantic == semantic:
                return e
        return None

    def has(self, semantic: int) -> bool:
        return self.find(semantic) is not None


# Canonical layouts from § 9.4 (verified across 619/621 sampled
# d4data/json/base/meta/Appearance/*.app.json files).
CANONICAL_LAYOUT_36 = VertexLayout(
    stride=VERTEX_STRIDE_SIMPLE, vb_format=4,
    elems=(
        VertexElem(SEM_POSITION,   FMT_R32G32B32_FLOAT, 0),
        VertexElem(SEM_NORMAL,     FMT_PACKED_SNORM4,   12),
        VertexElem(SEM_COLOR_0,    FMT_R8G8B8A8_UNORM,  16),
        VertexElem(SEM_COLOR_1,    FMT_R8G8B8A8_UNORM,  20),
        VertexElem(SEM_TEXCOORD_0, FMT_R16G16_FLOAT,    24),
        VertexElem(SEM_TEXCOORD_1, FMT_R16G16_FLOAT,    28),
        VertexElem(SEM_TANGENT,    FMT_PACKED_SNORM4,   32),
    ),
)

CANONICAL_LAYOUT_44 = VertexLayout(
    stride=VERTEX_STRIDE_SKINNED, vb_format=6,
    elems=(
        VertexElem(SEM_POSITION,     FMT_R32G32B32_FLOAT, 0),
        VertexElem(SEM_NORMAL,       FMT_PACKED_SNORM4,   12),
        VertexElem(SEM_TANGENT,      FMT_PACKED_SNORM4,   16),
        VertexElem(SEM_COLOR_0,      FMT_R8G8B8A8_UNORM,  20),
        VertexElem(SEM_COLOR_1,      FMT_R8G8B8A8_UNORM,  24),
        VertexElem(SEM_TEXCOORD_0,   FMT_R16G16_FLOAT,    28),
        VertexElem(SEM_TEXCOORD_1,   FMT_R16G16_FLOAT,    32),
        VertexElem(SEM_BLENDINDICES, FMT_R8G8B8A8_UINT,   36),
        VertexElem(SEM_BLENDWEIGHTS, FMT_R8G8B8A8_UNORM,  40),
    ),
)

CANONICAL_LAYOUTS: dict[int, VertexLayout] = {
    VERTEX_STRIDE_SIMPLE:  CANONICAL_LAYOUT_36,
    VERTEX_STRIDE_SKINNED: CANONICAL_LAYOUT_44,
}


# ─── Per-format decoders ─────────────────────────────────────────────

def _decode_f32x3(data: bytes, base: int) -> tuple[float, float, float]:
    return struct.unpack_from("<3f", data, base)


def _decode_half2(data: bytes, base: int) -> tuple[float, float]:
    u, v = struct.unpack_from("<2e", data, base)
    return (float(u), float(v))


def _decode_packed_snorm4(
    data: bytes, base: int,
) -> tuple[float, float, float, float]:
    """Decode an eFormat=8 packed quadruple to four floats in [-1, 1].

    The bytes are **signed 8-bit integers** (DXGI ``R8G8B8A8_SNORM``):
    byte ``0x00`` decodes to ``0``, ``0x7F`` (127) to ``+1.0``, and
    ``0x80`` (-128 in 2's complement) to ``-1.008`` (close to ``-1``;
    the conventional one-LSB asymmetry that the DX10 SNORM spec
    recommends clamping to -1, but the residual error is negligible
    after renormalisation).

    Used for NORMAL and TANGENT. The earlier AppToOBJ-style
    ``b/127.5 - 1`` formula was wrong here — verified by an empirical
    sweep against geometric face-normal averages on Goatman, where
    that formula scored mean dot ``-0.51`` (essentially inverted)
    while ``int8(b)/127`` scored mean dot ``+0.94`` and produced
    near-unit-length output (``|n|`` mean ≈ ``1.002``).
    """
    b = struct.unpack_from("<4b", data, base)
    return (b[0] / 127.0, b[1] / 127.0, b[2] / 127.0, b[3] / 127.0)


def _decode_unorm4(
    data: bytes, base: int,
) -> tuple[float, float, float, float]:
    """Decode 4 unsigned-normalized bytes to floats in [0, 1]."""
    return (
        data[base]     / 255.0,
        data[base + 1] / 255.0,
        data[base + 2] / 255.0,
        data[base + 3] / 255.0,
    )


def _decode_uint8x4(
    data: bytes, base: int,
) -> tuple[int, int, int, int]:
    return (data[base], data[base + 1], data[base + 2], data[base + 3])


def _decode_snorm16x2(data: bytes, base: int) -> tuple[float, float]:
    a, b = struct.unpack_from("<2h", data, base)
    return (a / 32767.0, b / 32767.0)


_FORMAT_DECODERS: dict[int, Callable[[bytes, int], object]] = {
    FMT_R32G32B32_FLOAT: _decode_f32x3,
    FMT_R16G16_FLOAT:    _decode_half2,
    FMT_PACKED_SNORM4:   _decode_packed_snorm4,
    FMT_R8G8B8A8_UNORM:  _decode_unorm4,
    FMT_R8G8B8A8_UINT:   _decode_uint8x4,
    FMT_R16G16_SNORM:    _decode_snorm16x2,
}


def _decode_half_float(h: int) -> float:
    """Pure-Python 16-bit IEEE 754 half-float decoder (kept for tests)."""
    sign = (h >> 15) & 1
    exp = (h >> 10) & 0x1F
    frac = h & 0x3FF
    if exp == 0:
        return ((-1) ** sign) * (2 ** -14) * (frac / 1024.0)
    if exp == 31:
        return float("inf") if frac == 0 else float("nan")
    return ((-1) ** sign) * (2 ** (exp - 15)) * (1 + frac / 1024.0)


def _unpack_normal(
    b0: int, b1: int, b2: int, b3: int,
) -> tuple[float, float, float, float]:
    """Decode a packed eFormat=8 quadruple from 4 bytes to floats.

    Bytes are signed (DXGI ``R8G8B8A8_SNORM``): see
    :func:`_decode_packed_snorm4` for the formula derivation.
    """
    def _to_signed(b: int) -> float:
        return ((b - 256) if b >= 128 else b) / 127.0
    return (_to_signed(b0), _to_signed(b1), _to_signed(b2), _to_signed(b3))


def _renormalize_xyz(stream: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
    """Renormalise the (x, y, z) of each tuple; leave w as-is.

    glTF 2.0 requires unit-length normals and tangents. Decoded SNORM
    values land at ~99.8% of unit length thanks to 8-bit quantisation
    plus the asymmetric ``int8/127`` formula — close enough that
    consumers usually re-normalise per pixel anyway, but renormalising
    at export time is cheap (~1.8 K verts × 1 sqrt) and keeps the
    output spec-compliant. ``w`` carries handedness for tangents and
    must not be rescaled.
    """
    out: list[tuple[float, float, float, float]] = []
    for x, y, z, w in stream:
        L = (x * x + y * y + z * z) ** 0.5
        if L < 1e-9:
            out.append((0.0, 0.0, 0.0, w))
        else:
            out.append((x / L, y / L, z / L, w))
    return out


# ─── Public dataclasses ──────────────────────────────────────────────

@dataclass
class Vertex:
    """Single decoded vertex (kept for back-compat; not used internally)."""
    position: tuple[float, float, float]
    normal: tuple[float, float, float, float]
    color: tuple[float, float, float, float]
    tangent: tuple[float, float, float, float]
    uv: tuple[float, float] | None = None


@dataclass
class Submesh:
    """Draw call range, mirroring SubObjectSegment fields.

    When SubObjectSegment data cannot be parsed from the meta file (the
    common case until the segment serialization is fully reversed), the
    range is populated from the index-buffer-break heuristic.

    ``bone_palette`` is the segment's ``pBoneIDs`` array — a per-draw-call
    palette mapping local-joint-byte (the values stored in JOINTS_0) to
    a global bone index in ``MeshData.skeleton.bones``.  Empty for static
    meshes and for fixture submeshes that don't come from a real .app.
    """
    vertex_offset: int = 0
    vertex_count: int = 0
    index_offset: int = 0
    index_count: int = 0
    material_index: int = 0
    bone_palette: tuple[int, ...] = ()
    sub_object_hash: int = 0  # SubObject.dwSubObjectHash (DJB2 of mesh name)
    slot_hash: int = 0        # SubObjectNameInfo.dwSlotHash (DJB2 of slot abbrev)
    name: str = ""            # resolved from sub_object_hash via hash_names


# ─── Skeleton dataclasses ────────────────────────────────────────────

@dataclass(frozen=True)
class BoneTransform:
    """One ``PRSTransform`` (40 bytes): rotation quat + translation + scale.

    ``q`` is ``(x, y, z, w)`` (D3D order), ``wp`` and ``scale`` are
    ``(x, y, z)``.  Coordinates remain D4-native (left-handed Z-up); the
    exporter applies the configured axis swap.
    """
    q: tuple[float, float, float, float]
    wp: tuple[float, float, float]
    scale: tuple[float, float, float]


@dataclass
class Bone:
    """One ``BoneStructure`` record.

    ``parent_index`` is ``-1`` for root bones.  ``local_trs`` is the
    ``transformParentRel`` field (rest pose relative to parent — used as
    the glTF node TRS).  ``inv_bind_trs`` is the ``transformSkinningInv``
    field (inverse bind pose — used to build glTF
    ``inverseBindMatrices`` directly, *no further inversion*).
    """
    index: int
    parent_index: int       # -1 for roots
    name_hash: int          # dwHash from BoneStructure +0x20
    flags: int              # dwFlags from BoneStructure +0x24
    lod: int                # nLOD from BoneStructure +0x2A
    local_trs: BoneTransform        # transformParentRel
    inv_bind_trs: BoneTransform     # transformSkinningInv

    @property
    def name(self) -> str:
        return f"bone_{self.name_hash:08x}"


@dataclass
class Skeleton:
    """Bone hierarchy embedded inside ``Structure.ptBoneData[0]``.

    ``template_id`` is ``BoneData.unk_a3acec8`` — a stable per-character-
    class identifier.  Two appearance files with the same ``template_id``
    use the same bone indices and can be combined under a shared
    armature.
    """
    bones: list[Bone] = field(default_factory=list)
    base_bone_count: int = 0
    cloth_bone_count: int = 0
    template_id: int = 0
    bone_array_offset: int = 0   # payload byte offset (debug)


@dataclass
class MetaHeader:
    magic: int = 0
    link_id: int = 0
    struct_table_offset: int = 0
    struct_entry_size: int = 0
    config_block_offset: int = 0
    config_block_size: int = 0
    unknown_68: int = 0
    lod_or_submesh_count: int = 0


@dataclass
class ConfigBlock:
    index_buffer_offset: int = 0
    index_buffer_size: int = 0
    sentinel: int = 0
    flags: int = 0
    geometry_data_size: int = 0
    raw: bytes = field(default=b"", repr=False)


@dataclass
class PayloadHeader:
    link_id: int = 0
    group_count: int = 0
    header_data_size: int = 0


@dataclass
class MeshData:
    """Parsed .app model (D4-native left-handed Z-up coordinates).

    Aliased as ``D4Model`` for new code.
    """
    name: str
    vertex_count: int = 0
    index_count: int = 0
    submesh_count: int = 0
    layout: VertexLayout | None = None
    positions: list[tuple[float, float, float]] = field(default_factory=list)
    normals:   list[tuple[float, float, float, float]] = field(default_factory=list)
    tangents:  list[tuple[float, float, float, float]] = field(default_factory=list)
    colors:    list[tuple[float, float, float, float]] = field(default_factory=list)
    colors_1:  list[tuple[float, float, float, float]] = field(default_factory=list)
    uvs:       list[tuple[float, float]] = field(default_factory=list)
    uvs_1:     list[tuple[float, float]] = field(default_factory=list)
    joints:    list[tuple[int, int, int, int]] = field(default_factory=list)
    weights:   list[tuple[float, float, float, float]] = field(default_factory=list)
    indices: list[int] = field(default_factory=list)
    submesh_breaks: list[int] = field(default_factory=list)
    submeshes: list[Submesh] = field(default_factory=list)
    meta_header: MetaHeader | None = None
    payload_header: PayloadHeader | None = None
    skeleton: "Skeleton | None" = None
    # Optional material roster resolved from d4data JSON. Populated by the
    # CLI when --d4data-path is given; ``None`` means "fall back to
    # Material_<n> stubs". See d4extract.formats.material_parser.
    materials: "list | None" = None


D4Model = MeshData


class AppFormatError(Exception):
    """Raised when an .app file has invalid or unrecognized structure."""


# ─── Coordinate conversion ───────────────────────────────────────────

def convert_to_gltf_coords(
    pos: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Convert a single D4-native position (LH Z-up) to glTF (RH Y-up).

    The mapping is (x, y, z) -> (x, z, -y).  Apply the same transform
    componentwise to direction vectors (normals, tangents).
    """
    x, y, z = pos
    return (x, z, -y)


# ─── Low-level reads ─────────────────────────────────────────────────

def _read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _read_u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _read_f32(data: bytes, offset: int) -> float:
    return struct.unpack_from("<f", data, offset)[0]


# ─── Header parsers ──────────────────────────────────────────────────

def parse_meta_header(data: bytes) -> MetaHeader:
    if len(data) < 0xC0:
        raise AppFormatError(
            f"Meta file too small: {len(data)} bytes (need >= 192)"
        )
    magic = _read_u32(data, 0x00)
    if magic != APP_META_MAGIC:
        raise AppFormatError(
            f"Invalid meta magic: 0x{magic:08X} (expected 0xDEADBEEF)"
        )
    return MetaHeader(
        magic=magic,
        link_id=_read_u32(data, 0x10),
        struct_table_offset=_read_u32(data, 0x40),
        struct_entry_size=_read_u32(data, 0x44),
        config_block_offset=_read_u32(data, 0x60),
        config_block_size=_read_u32(data, 0x64),
        unknown_68=_read_u32(data, 0x68),
        lod_or_submesh_count=_read_u32(data, 0xBC),
    )


def parse_config_block(data: bytes, header: MetaHeader) -> ConfigBlock | None:
    off = header.config_block_offset
    size = header.config_block_size
    if off == 0 or size == 0:
        return None
    if off + size > len(data):
        raise AppFormatError(
            f"Config block out of bounds: offset={off}, size={size}, "
            f"file_size={len(data)}"
        )
    blk = data[off:off + size]
    return ConfigBlock(
        index_buffer_offset=_read_u32(blk, 0x00),
        index_buffer_size=_read_u32(blk, 0x04),
        sentinel=_read_u32(blk, 0x08),
        flags=_read_u32(blk, 0x0C),
        geometry_data_size=_read_u32(blk, 0x24) if size > 0x28 else 0,
        raw=blk,
    )


def parse_payload_header(data: bytes) -> PayloadHeader:
    if len(data) < 0x10:
        raise AppFormatError(
            f"Payload file too small: {len(data)} bytes (need >= 16)"
        )
    return PayloadHeader(
        link_id=_read_u32(data, 0x00),
        group_count=_read_u32(data, 0x08),
        header_data_size=_read_u32(data, 0x0C),
    )


# ─── Vertex layout discovery ─────────────────────────────────────────

def _try_read_vertex_layout(
    meta: bytes, search_origin: int, stride: int,
) -> VertexLayout | None:
    """Search for a literal serialized VertexElem array near search_origin.

    Each VertexElem is three LE uint32s (semantic, format, offset); the
    POSITION element at the head of every layout is (0, 1, 0), which
    serializes as `_VERTEX_ELEM_SIGNATURE`.  When the signature is found
    in a small window around search_origin and the entries that follow
    are self-consistent (known formats, non-decreasing offsets, total
    coverage matching `stride`), the parsed layout is returned.

    Returns None if no plausible array is found; callers should fall
    back to ``CANONICAL_LAYOUTS[stride]``.
    """
    window_lo = max(0, search_origin - 0x800)
    window_hi = min(len(meta), search_origin + 0x800)
    pos = meta.find(_VERTEX_ELEM_SIGNATURE, window_lo, window_hi)
    if pos < 0:
        return None
    elems: list[VertexElem] = []
    cursor = pos
    last_offset = -1
    while cursor + 12 <= len(meta):
        sem, fmt, off = struct.unpack_from("<3I", meta, cursor)
        size = FORMAT_BYTE_SIZES.get(fmt)
        if size is None or off < last_offset or off + size > stride:
            break
        elems.append(VertexElem(sem, fmt, off))
        last_offset = off
        cursor += 12
        if off + size == stride:
            return VertexLayout(stride=stride, vb_format=0, elems=tuple(elems))
    return None


def resolve_vertex_layout(
    stride: int, meta: bytes | None = None, search_origin: int = 0,
) -> VertexLayout:
    """Return the VertexLayout for ``stride``, preferring a meta-derived
    layout when one can be parsed and validated, else the canonical fallback.
    """
    if meta is not None:
        parsed = _try_read_vertex_layout(meta, search_origin, stride)
        if parsed is not None:
            return parsed
    if stride not in CANONICAL_LAYOUTS:
        raise AppFormatError(f"Unsupported vertex stride: {stride}")
    return CANONICAL_LAYOUTS[stride]


# ─── Layout-driven vertex decoding ───────────────────────────────────

def _decode_vertices_layout(
    data: bytes, offset: int, count: int, layout: VertexLayout,
) -> dict[int, list]:
    """Decode ``count`` interleaved vertices using an explicit layout.

    Returns a dict keyed by eSemantic; values are per-vertex decoded tuples.
    Element offsets are read from ``layout.elems`` — never derived from
    ``stride - 4`` or any other positional shortcut.
    """
    end = offset + count * layout.stride
    if end > len(data):
        raise AppFormatError(
            f"Vertex buffer overflow: need {end} bytes, have {len(data)}"
        )
    streams: dict[int, list] = {e.semantic: [] for e in layout.elems}
    decoders = [
        (e.semantic, _FORMAT_DECODERS[e.format], e.offset) for e in layout.elems
    ]
    for i in range(count):
        base = offset + i * layout.stride
        for sem, dec, eoff in decoders:
            streams[sem].append(dec(data, base + eoff))
    return streams


def _decode_vertices(
    data: bytes,
    offset: int,
    count: int,
    stride: int = VERTEX_STRIDE_SIMPLE,
) -> tuple[
    list[tuple[float, float, float]],
    list[tuple[float, float, float, float]],
    list[tuple[float, float, float, float]],
    list[tuple[float, float, float, float]],
    list[tuple[float, float]],
]:
    """Legacy 5-tuple decoder.

    Returns ``(positions, normals, colors_0, tangents, uvs_0)``.  Colors
    are normalized [0, 1] floats per § 9.3 (eFormat 5 = R8G8B8A8_UNORM);
    callers that want raw bytes should multiply by 255.

    Internally uses the layout-driven decoder with the canonical layout
    for ``stride``, so stride 44 correctly places TANGENT at offset 16
    and JOINTS/WEIGHTS at offsets 36/40.
    """
    layout = CANONICAL_LAYOUTS.get(stride)
    if layout is None:
        raise AppFormatError(f"Unsupported stride: {stride}")
    streams = _decode_vertices_layout(data, offset, count, layout)
    return (
        streams.get(SEM_POSITION, []),
        streams.get(SEM_NORMAL, []),
        streams.get(SEM_COLOR_0, []),
        streams.get(SEM_TANGENT, []),
        streams.get(SEM_TEXCOORD_0, []),
    )


# ─── Index buffer decoding ───────────────────────────────────────────

def _decode_indices_u16(data: bytes, offset: int, count: int) -> list[int]:
    end = offset + count * 2
    if end > len(data):
        raise AppFormatError(
            f"Index buffer overflow: need {end} bytes, have {len(data)}"
        )
    return [_read_u16(data, offset + i * 2) for i in range(count)]


def _decode_indices_u32(data: bytes, offset: int, count: int) -> list[int]:
    end = offset + count * 4
    if end > len(data):
        raise AppFormatError(
            f"Index buffer overflow: need {end} bytes, have {len(data)}"
        )
    return [_read_u32(data, offset + i * 4) for i in range(count)]


# ─── GeoChunkVertexBuffer / GeoChunkIndexBuffer scanning ─────────────

# A meta-resident DT_VARIABLEARRAY occupies 16 bytes laid out as
#   +0x00..0x07: zero placeholder (8 bytes)
#   +0x08..0x0B: dataOffset (uint32)
#   +0x0C..0x0F: dataSize   (uint32)
# When `dataOffset` references META-resident data, the actual bytes live
# at meta[dataOffset + 16 .. dataOffset + 16 + dataSize] (16-byte prefix).
# When it references PAYLOAD data (vbuf/ibuf), the bytes live at
# payload[dataOffset .. dataOffset + dataSize] with no prefix.

# GeoChunkVertexBuffer struct layout (size 80, hash 3646617580):
#   +0x00 eVBFormat             u32  ({4, 6, 27})
#   +0x04 dwVertStride          u32  ({36, 44})
#   +0x08 ptVertexElems         DT_VARIABLEARRAY (16B)
#   +0x18 pnVertexElemPerSemantic DT_VARIABLEARRAY (16B)
#   +0x28 vfid                  i32
#   +0x2C (padding)             -- 4 bytes of zero alignment
#   +0x30 ptChunkVertices       DT_VARIABLEARRAY (16B)  -- payload pointer
#   +0x40 vbid                  i32
#   +0x44 baid                  i32
#   +0x48 unk_4c43adc           u32
#   +0x4C fOptional             u32  (1 = LOD0, 0 = LOD1+)

# GeoChunkIndexBuffer struct layout (size 24, hash 99338022):
#   +0x00 pdwChunkIndices       DT_VARIABLEARRAY (16B) -- payload pointer
#   +0x10 ibid                  i32
#   +0x14 fOptional             u32

# SubObject struct layout (size 240, hash 4121622049):
#   relevant fields only:
#   +0x00 dwFlags               u32
#   +0x60 nMaterialIndex        i32
#   +0x68 nVertBufferIndex      i32  (vbi — index into ptChunkVertexBuffers)
#   +0x6C nIndexBufferIndex     i32  (ibi — index into ptChunkIndexBuffers)
#   +0x70 nBaseLODSubObjectIndex i32
#   +0x78 nSubObjectMaxLOD      i32
#   +0xC8 ptSegments            DT_VARIABLEARRAY (16B) -- meta pointer to
#                                                       segment struct(s)


@dataclass
class _GeoChunkVertexBuffer:
    """One vbuf record from the meta's ptChunkVertexBuffers array."""
    file_offset: int
    array_index: int
    eVBFormat: int
    dwVertStride: int
    dataOffset: int          # payload byte offset of the vbuf
    dataSize: int            # vbuf size in bytes
    fOptional: bool          # True for LOD0, False for the LOD1+ packed buffer


@dataclass
class _GeoChunkIndexBuffer:
    """One ibuf record from the meta's ptChunkIndexBuffers array."""
    file_offset: int
    array_index: int
    dataOffset: int          # payload byte offset of the ibuf
    dataSize: int            # ibuf size in bytes (u16 indices => /2 = count)
    ibid: int
    fOptional: bool


def _scan_geo_chunk_vertex_buffers(
    meta: bytes, payload_size: int,
) -> list[_GeoChunkVertexBuffer]:
    """Locate every GeoChunkVertexBuffer struct head in the meta.

    Recognised by the eVBFormat / dwVertStride pair at the head of the
    80-byte struct.  ``ptChunkVertices.dataOffset`` lives at +0x38 and
    must point INTO the payload (not the meta), with its `dataSize`
    being a multiple of `dwVertStride`.  `fOptional` at +0x4C selects
    the LOD0 buffer (== 1).
    """
    out: list[_GeoChunkVertexBuffer] = []
    n = len(meta)
    for off in range(0, n - 80 + 1, 4):
        vbf = struct.unpack_from("<I", meta, off)[0]
        if vbf not in (4, 6, 27):
            continue
        stride = struct.unpack_from("<I", meta, off + 4)[0]
        if stride not in (VERTEX_STRIDE_SIMPLE, VERTEX_STRIDE_SKINNED):
            continue
        dataOffset = struct.unpack_from("<I", meta, off + 0x38)[0]
        dataSize = struct.unpack_from("<I", meta, off + 0x3C)[0]
        if dataOffset == 0 or dataSize == 0:
            continue
        if dataOffset + dataSize > payload_size:
            continue
        if dataSize % stride != 0:
            continue
        # Don't gate on `dataOffset > meta_size` — payloads with a tiny
        # header_data_size (e.g. offHandsSorc, header=32 bytes) place
        # their first vbuf at payload offset 32, which is smaller than
        # most meta files.  The eVBFormat/dwVertStride signature plus
        # the dataSize % stride check are already strong enough.
        fOptional = struct.unpack_from("<I", meta, off + 0x4C)[0]
        if fOptional not in (0, 1):
            continue
        out.append(_GeoChunkVertexBuffer(
            file_offset=off, array_index=-1,
            eVBFormat=vbf, dwVertStride=stride,
            dataOffset=dataOffset, dataSize=dataSize,
            fOptional=bool(fOptional),
        ))
    out.sort(key=lambda v: v.file_offset)
    for i, v in enumerate(out):
        v.array_index = i
    return out


def _scan_geo_chunk_index_buffers(
    meta: bytes, payload_size: int,
    vbs: list[_GeoChunkVertexBuffer] | None = None,
) -> list[_GeoChunkIndexBuffer]:
    """Locate every GeoChunkIndexBuffer struct head in the meta.

    The 24-byte struct begins with 8 zero bytes (the leading half of the
    DT_VARIABLEARRAY header), then ``dataOffset`` and ``dataSize`` of
    the index buffer, then ``ibid`` and ``fOptional``.

    Because the leading 8-zero signature is weak (every DT_VARIABLEARRAY
    starts that way), the raw scan can pick up unrelated arrays — most
    notably ``BoneData.ptBoneStructure``, whose payload-resident header
    has the same shape.  When ``vbs`` is supplied we apply two
    additional filters that reject those false positives:

    1. **Position relative to the VB array.**  Real IBs sit immediately
       after the VBs in the meta (every sample we have inspected places
       them within ~256 bytes of the last VB's end).  Candidates with
       a negative gap (i.e. before the VBs) or a very large gap are
       dropped.
    2. **24-byte-strided clustering.**  ``GeoChunkIndexBuffer`` records
       are stored in a contiguous array, so real IBs are always at
       24-byte file-offset stride from each other.  When the post-VB
       set contains contiguous runs we keep the longest one — that
       discards a singleton bone-array header that happened to sit in
       the same neighbourhood.

    When ``vbs`` is ``None`` (legacy callers) the raw scan is returned
    unchanged.
    """
    out: list[_GeoChunkIndexBuffer] = []
    n = len(meta)
    for off in range(0, n - 24 + 1, 4):
        h0, h1 = struct.unpack_from("<2I", meta, off)
        if h0 != 0 or h1 != 0:
            continue
        dataOffset = struct.unpack_from("<I", meta, off + 0x08)[0]
        dataSize = struct.unpack_from("<I", meta, off + 0x0C)[0]
        if dataOffset == 0 or dataSize == 0:
            continue
        if dataOffset <= n:
            # meta-resident reference, not an ibuf
            continue
        if dataOffset + dataSize > payload_size:
            continue
        if dataSize % 2 != 0:
            continue
        ibid = struct.unpack_from("<i", meta, off + 0x10)[0]
        fOptional = struct.unpack_from("<I", meta, off + 0x14)[0]
        if fOptional not in (0, 1):
            continue
        if ibid != -1 and not (0 <= ibid <= 1024):
            continue
        out.append(_GeoChunkIndexBuffer(
            file_offset=off, array_index=-1,
            dataOffset=dataOffset, dataSize=dataSize,
            ibid=ibid, fOptional=bool(fOptional),
        ))
    out.sort(key=lambda v: v.file_offset)

    if vbs:
        out = _filter_ib_cluster(out, vbs)

    for i, v in enumerate(out):
        v.array_index = i
    return out


def _filter_ib_cluster(
    candidates: list[_GeoChunkIndexBuffer],
    vbs: list[_GeoChunkVertexBuffer],
) -> list[_GeoChunkIndexBuffer]:
    """Filter index-buffer candidates to those that form the real array.

    See :func:`_scan_geo_chunk_index_buffers` for the rationale.  This
    helper keeps the logic isolated so the raw scan remains a simple,
    independently testable signature match.
    """
    if not candidates:
        return candidates
    last_vb_end = max(vb.file_offset + 80 for vb in vbs)
    # Window: real IBs sit just after the VBs.  1024 bytes is generous —
    # in every sample the gap is under 256.
    window = [
        ib for ib in candidates
        if last_vb_end < ib.file_offset <= last_vb_end + 1024
    ]
    if not window:
        return candidates  # no positional anchor — fall back
    # Find runs of contiguous 24-byte-strided entries.
    runs: list[list[_GeoChunkIndexBuffer]] = [[window[0]]]
    for ib in window[1:]:
        if ib.file_offset - runs[-1][-1].file_offset == 24:
            runs[-1].append(ib)
        else:
            runs.append([ib])
    # Prefer the longest run; on tie, the earliest (closest to VBs).
    best = max(runs, key=lambda r: (len(r), -r[0].file_offset))
    return best


# ─── SubObjectSegment scanning ───────────────────────────────────────

@dataclass
class _RawSegment:
    """A SubObjectSegment record located in the meta file."""
    file_offset: int                    # byte position of the (vc,vo,ic,io) tuple
    nVertCount: int
    nVertOffset: int                    # bytes from start of vbuf
    nIndexCount: int
    nIndexOffset: int                   # indices from start of ibuf
    pBoneIDs: tuple[int, ...] = ()      # per-segment palette → global bone idx


@dataclass
class _SubObjectInfo:
    """A SubObject record paired with its single SubObjectSegment.

    Each SubObject in the sampled .app files carries exactly one
    segment, so we treat the pair as one draw call.  The vbi/ibi indices
    select which entry of ptChunkVertexBuffers / ptChunkIndexBuffers
    this draw call uses; a SubObject with vbi == optional_vb_idx and
    ibi == optional_ib_idx is part of LOD0.
    """
    file_offset: int
    nMaterialIndex: int
    vbi: int
    ibi: int
    nBaseLODSubObjectIndex: int
    nSubObjectMaxLOD: int
    dwFlags: int
    dwSubObjectHash: int
    dwSlotHash: int
    segment: _RawSegment


def _find_subobjects(
    meta: bytes, segments: list[_RawSegment],
) -> list[_SubObjectInfo]:
    """For each scanned segment, locate the SubObject that owns it.

    SubObject's ``ptSegments`` (at +0xC8) is a meta-resident
    DT_VARIABLEARRAY whose ``dataOffset`` (at +0x08 within the array
    header, i.e. struct offset 0xD0) equals the segment's struct start
    minus the 16-byte data prefix.  We search the meta for that 4-byte
    LE value, then for each match check that the surrounding 16 bytes
    look like a DT_VARIABLEARRAY header (8 leading zeros) and that
    ``dataSize`` (at +0x0C) equals 32 — the size of one segment record.
    The SubObject struct head is then ``hit - 0x08 - 0xC8`` = ``hit -
    0xD0``.
    """
    out: list[_SubObjectInfo] = []
    used_seg_offsets: set[int] = set()
    for seg in segments:
        if seg.file_offset - 16 in used_seg_offsets:
            continue  # already paired with a SubObject
        target = seg.file_offset - 16 - 16  # segment.struct_start - 16 prefix
        # seg.file_offset = struct_offset + 16 (the (vc,vo,...) is at +0x10).
        # struct_offset = seg.file_offset - 16.  ptSegments.dataOffset =
        # struct_offset - 16 (the +16 prefix on meta-resident array data).
        pat = struct.pack("<I", target)
        pos = 0
        found_so: _SubObjectInfo | None = None
        while True:
            p = meta.find(pat, pos)
            if p < 0:
                break
            if p < 8 or p + 8 > len(meta):
                pos = p + 1
                continue
            pre0, pre1 = struct.unpack_from("<2I", meta, p - 8)
            size_at = struct.unpack_from("<I", meta, p + 4)[0]
            if pre0 != 0 or pre1 != 0 or size_at != 32:
                pos = p + 1
                continue
            so_start = p - 0x08 - 0xC8
            if so_start < 0 or so_start + 240 > len(meta):
                pos = p + 1
                continue
            mat = struct.unpack_from("<i", meta, so_start + 0x60)[0]
            vbi = struct.unpack_from("<i", meta, so_start + 0x68)[0]
            ibi = struct.unpack_from("<i", meta, so_start + 0x6C)[0]
            baseLOD = struct.unpack_from("<i", meta, so_start + 0x70)[0]
            maxLOD = struct.unpack_from("<i", meta, so_start + 0x78)[0]
            flags = struct.unpack_from("<I", meta, so_start + 0x00)[0]
            so_hash = struct.unpack_from("<I", meta, so_start + 0x64)[0]
            # tNameInfo (SubObjectNameInfo) starts at +0x38; dwSlotHash at +0x10 within.
            slot_hash = struct.unpack_from("<I", meta, so_start + 0x38 + 0x10)[0]
            # Sanity: vbi/ibi must be small array indices
            if not (-1 <= vbi <= 16) or not (-1 <= ibi <= 16):
                pos = p + 1
                continue
            found_so = _SubObjectInfo(
                file_offset=so_start,
                nMaterialIndex=mat, vbi=vbi, ibi=ibi,
                nBaseLODSubObjectIndex=baseLOD,
                nSubObjectMaxLOD=maxLOD,
                dwFlags=flags,
                dwSubObjectHash=so_hash,
                dwSlotHash=slot_hash,
                segment=seg,
            )
            used_seg_offsets.add(seg.file_offset - 16)
            break
        if found_so is not None:
            out.append(found_so)
    out.sort(key=lambda s: s.file_offset)
    return out


# ─── Mesh descriptor extraction ──────────────────────────────────────

@dataclass
class _MeshDescriptor:
    """One LOD's vertex/index buffer location, found via the FindSubMesh sentinel."""
    stride: int = 0
    attrib_count: int = 0
    sentinel_offset: int = 0
    vbuf_offset: int = 0
    vbuf_size: int = 0
    ibuf_offset: int = 0
    ibuf_size: int = 0

    @property
    def vcount(self) -> int:
        return self.vbuf_size // self.stride if self.stride else 0


def _extract_mesh_descriptors(
    meta: bytes, payload_size: int,
) -> list[_MeshDescriptor]:
    """Scan the meta file for every LOD vertex-buffer descriptor.

    The 19-byte sentinel matches the (attrib_count u32, 0xFFFFFFFF, 12
    zero bytes) prefix of a per-LOD descriptor.  attrib_count is the
    length of the buffer's pnVertexElemPerSemantic array (11 -> stride
    36, 13 -> stride 44; see § 9.2-9.4).  vbuf_offset/vbuf_size sit at
    +20/+24 from the attrib_count word.
    """
    descriptors: list[_MeshDescriptor] = []
    pos = 0
    while True:
        pos = meta.find(_FIND_SUBMESH_PATTERN, pos)
        if pos == -1:
            break
        if pos < 1:
            pos += 1
            continue
        attrib_off = pos - 1
        attrib_count = _read_u32(meta, attrib_off)
        stride = ATTRIB_COUNT_TO_STRIDE.get(attrib_count)
        if stride is None or attrib_off + 28 > len(meta):
            pos += 1
            continue
        vbuf_offset = _read_u32(meta, attrib_off + 20)
        vbuf_size = _read_u32(meta, attrib_off + 24)
        if (vbuf_offset == 0 or vbuf_size == 0
                or vbuf_offset + vbuf_size > payload_size):
            pos += 1
            continue
        descriptors.append(_MeshDescriptor(
            stride=stride,
            attrib_count=attrib_count,
            sentinel_offset=attrib_off,
            vbuf_offset=vbuf_offset,
            vbuf_size=vbuf_size,
        ))
        pos += 1

    descriptors.sort(key=lambda d: d.vbuf_offset)
    for i, d in enumerate(descriptors):
        end = d.vbuf_offset + d.vbuf_size
        next_start = (
            descriptors[i + 1].vbuf_offset
            if i + 1 < len(descriptors) else payload_size
        )
        d.ibuf_offset = end
        d.ibuf_size = max(0, next_start - end)
    return descriptors


def _pick_lod0(
    descriptors: list[_MeshDescriptor], header_data_size: int,
) -> _MeshDescriptor | None:
    """Pick the highest-detail descriptor.  LOD0's vbuf starts at
    payload header_data_size; falls back to the largest vbuf."""
    if not descriptors:
        return None
    for d in descriptors:
        if d.vbuf_offset == header_data_size:
            return d
    return max(descriptors, key=lambda d: d.vbuf_size)


# ─── Buffer location ─────────────────────────────────────────────────

def _find_vertex_and_index_buffers(
    payload: bytes,
    phdr: PayloadHeader,
    config: ConfigBlock | None,
    meta: bytes | None = None,
) -> tuple[int, int, int, int, int, int]:
    """Locate the LOD0 vertex and index buffers in the payload.

    Returns ``(vertex_offset, vertex_count, index_offset, index_count,
    index_format, stride)``.
    """
    if meta is not None:
        descriptors = _extract_mesh_descriptors(meta, len(payload))
        lod0 = _pick_lod0(descriptors, phdr.header_data_size)

        if lod0 is not None and _validate_vertex_region(
                payload, lod0.vbuf_offset, min(lod0.vcount, 20), lod0.stride):
            stride = lod0.stride
            voff = lod0.vbuf_offset
            vcount = lod0.vcount

            candidates: list[tuple[int, int, str]] = []
            if lod0.ibuf_size >= 6:
                candidates.append((lod0.ibuf_offset, lod0.ibuf_size, "lod0_gap"))
            if (config and config.sentinel == 0xFFFFFFFF
                    and config.index_buffer_size >= 6
                    and config.index_buffer_offset
                        + config.index_buffer_size <= len(payload)):
                co = config.index_buffer_offset
                ce = co + config.index_buffer_size
                lo = lod0.ibuf_offset
                le = lo + lod0.ibuf_size
                overlap = max(0, min(ce, le) - max(co, lo))
                if overlap >= max(6, config.index_buffer_size // 2):
                    cand = (config.index_buffer_offset,
                            config.index_buffer_size, "config")
                    if cand not in candidates:
                        candidates.append(cand)

            best = _pick_index_buffer(
                payload, [(o, s) for (o, s, _t) in candidates], vcount,
            )
            if best is not None:
                ioff, icount, ifmt = best
                return voff, vcount, ioff, icount, ifmt, stride

    # Fallback: forward scan from header_data_size with stride hints.
    stride_hint = None
    if meta is not None:
        descriptors = _extract_mesh_descriptors(meta, len(payload))
        if descriptors:
            stride_hint = descriptors[0].stride

    vdata_start = phdr.header_data_size if phdr.header_data_size > 0 else 0x10
    stride_order = [stride_hint] if stride_hint else []
    for s in SUPPORTED_STRIDES:
        if s not in stride_order:
            stride_order.append(s)

    for stride in stride_order:
        if vdata_start + stride * 3 > len(payload):
            continue
        if not _validate_vertex_region(payload, vdata_start, 3, stride):
            continue

        vend = vdata_start
        while vend + stride <= len(payload):
            x = _read_f32(payload, vend)
            y = _read_f32(payload, vend + 4)
            z = _read_f32(payload, vend + 8)
            if (all(abs(v) < 500 for v in (x, y, z))
                    and any(abs(v) > 1e-7 for v in (x, y, z))):
                vend += stride
            else:
                break
        n_verts = (vend - vdata_start) // stride
        if n_verts < 3:
            continue

        ibuf_result = _find_index_buffer_after(payload, vend, n_verts)
        if ibuf_result:
            ioff, icount, ifmt = ibuf_result
            return vdata_start, n_verts, ioff, icount, ifmt, stride

    idx_info = _scan_index_buffer_backward(payload)
    if idx_info:
        idx_off, idx_count, max_idx, idx_fmt = idx_info
        n_verts = max_idx + 1
        for stride in stride_order or SUPPORTED_STRIDES:
            vbuf_size = n_verts * stride
            vbuf_start = idx_off - vbuf_size
            if (vbuf_start >= 0
                    and _validate_vertex_region(
                        payload, vbuf_start, n_verts, stride)):
                return vbuf_start, n_verts, idx_off, idx_count, idx_fmt, stride

    raise AppFormatError("Could not locate vertex/index buffers in payload")


def _pick_index_buffer(
    payload: bytes,
    candidates: list[tuple[int, int]],
    vcount: int,
) -> tuple[int, int, int] | None:
    """Choose the cleanest uint16 index buffer from a list of candidates.

    Ranks by (degenerate-triangle ratio, fewer indices) — a buffer whose
    triangles are well-formed beats a larger but garbled one.
    """
    best = None
    best_key: tuple[float, int] | None = None
    for off, size in candidates:
        if off <= 0 or size < 6 or off + size > len(payload):
            continue
        n = size // 2
        indices = [_read_u16(payload, off + i * 2) for i in range(n)]
        while indices and indices[-1] == 0:
            indices.pop()
        n = (len(indices) // 3) * 3
        indices = indices[:n]
        if n < 3:
            continue

        max_idx = max(indices)
        if max_idx >= vcount:
            continue
        ntri = n // 3
        degen = sum(
            1 for i in range(0, n - 2, 3)
            if indices[i] == indices[i + 1]
            or indices[i + 1] == indices[i + 2]
            or indices[i] == indices[i + 2]
        )
        key = (degen / max(1, ntri), -n)
        if best_key is None or key < best_key:
            best_key = key
            best = (off, n, INDEX_FORMAT_U16)
    return best


def detect_submesh_breaks(indices: list[int]) -> list[int]:
    """Identify triangle positions where a new submesh begins.

    Submesh-grouped models concatenate draw calls whose index buffers are
    locally zero-based.  See § 9.6 / SubObjectSegment.{nVertOffset,
    nIndexOffset} for the authoritative segmentation; this heuristic is
    used as a fallback when SubObjectSegment records cannot yet be
    located in the binary meta.
    """
    if len(indices) < 6:
        return [0]
    breaks = [0]
    submesh_max = max(indices[0], indices[1], indices[2])
    n = len(indices)
    ti = 3
    while ti + 5 < n:
        tri_max = max(indices[ti], indices[ti + 1], indices[ti + 2])
        next_tri_max = max(indices[ti + 3], indices[ti + 4], indices[ti + 5])
        if (submesh_max >= 30
                and tri_max <= 5
                and next_tri_max < submesh_max // 3):
            breaks.append(ti)
            submesh_max = max(tri_max, next_tri_max)
        else:
            submesh_max = max(submesh_max, tri_max)
        ti += 3
    return breaks


def apply_submesh_offsets(
    indices: list[int], breaks: list[int],
) -> tuple[list[int], list[int]]:
    """Translate per-submesh local indices into a single global index space."""
    if not breaks:
        return list(indices), [0]
    adjusted: list[int] = []
    bases: list[int] = []
    cum_offset = 0
    for sm_i, start in enumerate(breaks):
        end = breaks[sm_i + 1] if sm_i + 1 < len(breaks) else len(indices)
        chunk = indices[start:end]
        if not chunk:
            continue
        bases.append(cum_offset)
        for c in chunk:
            adjusted.append(c + cum_offset)
        local_max = max(chunk)
        cum_offset += local_max + 1
    return adjusted, bases


def _find_index_buffer_after(
    payload: bytes, search_start: int, vcount: int,
) -> tuple[int, int, int] | None:
    """Scan for a uint16 index buffer at or near ``search_start``."""
    ioff = search_start
    if ioff % 2 != 0:
        ioff += 1

    scan_limit = min(ioff + 64, len(payload) - 6)
    while (ioff <= scan_limit
            and _read_u16(payload, ioff) == 0
            and _read_u16(payload, ioff + 2) == 0):
        ioff += 2
    if ioff + 6 > len(payload):
        return None

    test_idx = [_read_u16(payload, ioff + j * 2) for j in range(3)]
    if not all(v < vcount for v in test_idx):
        return None

    icount = 0
    pos = ioff
    while pos + 2 <= len(payload):
        v = _read_u16(payload, pos)
        if v >= vcount + 500:
            break
        icount += 1
        pos += 2
    while icount > 0 and _read_u16(payload, ioff + (icount - 1) * 2) == 0:
        icount -= 1
    icount -= icount % 3

    if icount >= 3:
        max_idx = max(_read_u16(payload, ioff + j * 2) for j in range(icount))
        if max_idx < vcount:
            return ioff, icount, INDEX_FORMAT_U16
    return None


def _validate_vertex_region(
    data: bytes, offset: int, count: int, stride: int = VERTEX_STRIDE_SIMPLE,
) -> bool:
    """Heuristic: do the first few vertex positions look like floats?"""
    check = min(count, 20)
    if offset + check * stride > len(data):
        return False
    valid = 0
    for i in range(check):
        base = offset + i * stride
        x = _read_f32(data, base)
        y = _read_f32(data, base + 4)
        z = _read_f32(data, base + 8)
        if (all(abs(v) < 500 for v in (x, y, z))
                and any(abs(v) > 1e-7 for v in (x, y, z))):
            valid += 1
    return valid >= check * 0.7


def _scan_index_buffer_backward(
    data: bytes,
) -> tuple[int, int, int, int] | None:
    """Find the index buffer by scanning backward from end of payload.

    Returns ``(offset, count, max_index, format_size)`` or None.
    """
    end = len(data) - 1
    while end > 0 and data[end] == 0:
        end -= 1
    end += 1
    if end % 2 != 0:
        end += 1
    end = min(end, len(data))

    if end < 6:
        return None

    tail = [_read_u16(data, end - 6 + i * 2) for i in range(3)]
    max_tail = max(tail)
    if max_tail > 200000:
        return None

    max_idx = max_tail
    idx_start = end
    for pos in range(end - 2, max(0, end - 2_000_000), -2):
        val = _read_u16(data, pos)
        if val <= max_idx + 500:
            idx_start = pos
            max_idx = max(max_idx, val)
        else:
            break

    idx_count = (end - idx_start) // 2
    if idx_count < 3:
        return None
    idx_count -= idx_count % 3

    actual_max = 0
    for i in range(idx_count):
        actual_max = max(actual_max, _read_u16(data, idx_start + i * 2))

    return idx_start, idx_count, actual_max, INDEX_FORMAT_U16


# ─── LOD0 extraction (authoritative path) ────────────────────────────

def _build_lod0_mesh(
    name: str,
    meta: bytes,
    payload: bytes,
    meta_hdr: MetaHeader,
    payload_hdr: PayloadHeader,
) -> MeshData | None:
    """Build a MeshData from the .app's LOD0 buffers (the authoritative
    path).  Returns None when the meta does not contain locatable
    GeoChunkVertexBuffer / GeoChunkIndexBuffer / SubObject structures,
    so the caller can fall back to the legacy heuristic path used by
    the synthetic-fixture tests.

    Steps:
      1. Scan all GeoChunkVertexBuffer struct heads and pick the one
         with ``fOptional=1`` — that's LOD0.  Same for index buffers.
         Both are read from the meta with their exact ``dataOffset`` /
         ``dataSize`` fields, so no leading-padding shift is needed.
      2. Decode every vertex in the LOD0 vbuf using the layout for
         ``dwVertStride``.
      3. Locate every SubObjectSegment in the meta and pair each one
         with its parent SubObject via the SubObject's ``ptSegments``
         array header.
      4. Filter SubObjects to those whose ``nVertBufferIndex`` matches
         the LOD0 vbuf's array index (and ``nIndexBufferIndex`` matches
         the LOD0 ibuf's index).  These are the LOD0 draw calls.
      5. For each LOD0 SubObject, slice its segment's range out of the
         LOD0 ibuf, add ``nVertOffset / stride`` (BaseVertexLocation)
         to each index, drop degenerate triangles, and emit a Submesh
         carrying the SubObject's ``nMaterialIndex``.
    """
    vbs = _scan_geo_chunk_vertex_buffers(meta, len(payload))
    ibs = _scan_geo_chunk_index_buffers(meta, len(payload), vbs)
    if not vbs or not ibs:
        return None

    vb0 = next((v for v in vbs if v.fOptional), None)
    ib0 = next((i for i in ibs if i.fOptional), None)
    if vb0 is None or ib0 is None:
        return None

    stride = vb0.dwVertStride
    vcount = vb0.dataSize // stride
    layout = resolve_vertex_layout(stride, meta, vb0.file_offset)

    if vb0.dataOffset + vb0.dataSize > len(payload):
        return None
    streams = _decode_vertices_layout(payload, vb0.dataOffset, vcount, layout)

    icount = ib0.dataSize // 2
    if ib0.dataOffset + ib0.dataSize > len(payload):
        return None
    raw_indices = [
        struct.unpack_from("<H", payload, ib0.dataOffset + i * 2)[0]
        for i in range(icount)
    ]

    segments = _scan_segments_unbounded(meta)
    sub_objects = _find_subobjects(meta, segments)
    lod0_subs = [
        so for so in sub_objects
        if so.vbi == vb0.array_index and so.ibi == ib0.array_index
    ]
    if not lod0_subs:
        return None

    # Order by index range first, vertex range second — the natural draw
    # order matches what the engine submits.
    lod0_subs.sort(key=lambda s: (s.segment.nIndexOffset, s.segment.nVertOffset))

    out_indices: list[int] = []
    breaks: list[int] = []
    submeshes: list[Submesh] = []
    cursor = 0
    for so in lod0_subs:
        seg = so.segment
        base_v = seg.nVertOffset // stride
        end_idx = seg.nIndexOffset + seg.nIndexCount
        if end_idx > len(raw_indices):
            return None
        chunk = raw_indices[seg.nIndexOffset:end_idx]
        emitted = 0
        for i in range(0, len(chunk) - 2, 3):
            a, b, c = chunk[i], chunk[i + 1], chunk[i + 2]
            if a == b or b == c or a == c:
                continue
            ga = a + base_v
            gb = b + base_v
            gc = c + base_v
            if ga >= vcount or gb >= vcount or gc >= vcount:
                return None
            out_indices.extend((ga, gb, gc))
            emitted += 3
        if emitted == 0:
            continue
        breaks.append(cursor)
        submeshes.append(Submesh(
            vertex_offset=base_v,
            vertex_count=seg.nVertCount,
            index_offset=cursor,
            index_count=emitted,
            material_index=so.nMaterialIndex,
            bone_palette=seg.pBoneIDs,
            sub_object_hash=so.dwSubObjectHash,
            slot_hash=so.dwSlotHash,
            name=resolve_hash(so.dwSubObjectHash) or "",
        ))
        cursor += emitted

    if not submeshes:
        return None

    return MeshData(
        name=name,
        vertex_count=vcount,
        index_count=len(out_indices),
        submesh_count=len(submeshes),
        layout=layout,
        positions=streams.get(SEM_POSITION, []),
        normals=_renormalize_xyz(streams.get(SEM_NORMAL, [])),
        tangents=_renormalize_xyz(streams.get(SEM_TANGENT, [])),
        colors=streams.get(SEM_COLOR_0, []),
        colors_1=streams.get(SEM_COLOR_1, []),
        uvs=streams.get(SEM_TEXCOORD_0, []),
        uvs_1=streams.get(SEM_TEXCOORD_1, []),
        joints=streams.get(SEM_BLENDINDICES, []),
        weights=streams.get(SEM_BLENDWEIGHTS, []),
        indices=out_indices,
        submesh_breaks=breaks,
        submeshes=submeshes,
        meta_header=meta_hdr,
        payload_header=payload_hdr,
        skeleton=parse_skeleton(meta, payload),
    )


def _read_pBoneIDs(meta: bytes, struct_offset: int) -> tuple[int, ...]:
    """Read the segment's pBoneIDs (DT_VARIABLEARRAY<DT_INT>) values.

    The ``SubObjectSegment`` struct begins with the 16-byte
    DT_VARIABLEARRAY header for ``pBoneIDs``: 8 zero bytes, then
    ``dataOffset`` and ``dataSize``.  When ``dataOffset`` references
    META-resident data the actual int32 values live at
    ``meta[dataOffset + 16]`` (16-byte prefix convention; see the long
    comment near `_GeoChunkVertexBuffer`).  Returns an empty tuple when
    the array is empty or extends past the meta.
    """
    if struct_offset + 16 > len(meta):
        return ()
    do, ds = struct.unpack_from("<2I", meta, struct_offset + 8)
    if do == 0 or ds == 0 or ds % 4 != 0:
        return ()
    base = do + 16
    end = base + ds
    if end > len(meta):
        return ()
    n = ds // 4
    raw = struct.unpack_from(f"<{n}i", meta, base)
    if any(v < 0 or v > 100_000 for v in raw):
        return ()
    return tuple(raw)


def _scan_segments_unbounded(meta: bytes) -> list[_RawSegment]:
    """Find every SubObjectSegment in the meta without descriptor bounds.

    The bounded ``_scan_subobject_segments`` requires LOD descriptors to
    validate (vc, vo, ic, io); when we use the explicit VB/IB scan we
    don't need that filter — every segment is genuine because the
    pBoneIDs leading-8-zeros + (ic % 3, io % 3, vc≥1, ic≥3) checks are
    already very strong.
    """
    out: list[_RawSegment] = []
    for off in range(16, len(meta) - 16, 4):
        h0, h1 = struct.unpack_from("<2I", meta, off - 16)
        if h0 != 0 or h1 != 0:
            continue
        vc, vo, ic, io = struct.unpack_from("<4I", meta, off)
        if not (1 <= vc <= 200_000):
            continue
        if not (3 <= ic <= 5_000_000) or ic % 3 != 0:
            continue
        if io > 5_000_000 or io % 3 != 0:
            continue
        out.append(_RawSegment(
            file_offset=off,
            nVertCount=vc, nVertOffset=vo,
            nIndexCount=ic, nIndexOffset=io,
            pBoneIDs=_read_pBoneIDs(meta, off - 16),
        ))
    return out


# ─── Skeleton parser ─────────────────────────────────────────────────

# BoneStructure (size 232, hash 3352290229) layout — see
# docs/skeleton_format_spec.md §2 for the full table.
_BONE_STRUCT_SIZE = 232
_BONE_PARENT_OFFSET = 0x28           # int16 nParentIndex (0xFFFF = root)
_BONE_HASH_OFFSET = 0x20             # uint32 dwHash
_BONE_FLAGS_OFFSET = 0x24            # uint32 dwFlags
_BONE_LOD_OFFSET = 0x2A              # int16 nLOD
_BONE_LOCAL_TRS_OFFSET = 0x94        # PRSTransform transformParentRel
_BONE_INV_BIND_TRS_OFFSET = 0xBC     # PRSTransform transformSkinningInv

# BoneData (size 416, hash 1364938622) field offsets used here:
_BONEDATA_TEMPLATE_OFFSET = 0x00     # uint32 unk_a3acec8 — skeleton template id
_BONEDATA_NBASE_OFFSET = 0x04        # int32 nBaseBoneCount
_BONEDATA_PTBONESTRUCT_OFFSET = 0x08 # DT_VARIABLEARRAY ptBoneStructure (16 B)
_BONEDATA_NCLOTH_OFFSET = 0x18       # int32 nClothBoneCount


def _read_prstransform(buf: bytes, base: int) -> BoneTransform:
    """Decode one ``PRSTransform`` (40 bytes) at ``buf[base..base+40]``.

    Layout: ``q`` (4 floats: x, y, z, w) at +0x00, ``wp`` (3 floats) at
    +0x10, ``vScale`` (3 floats) at +0x1C.  Coordinates remain D4-native.
    """
    qx, qy, qz, qw = struct.unpack_from("<4f", buf, base + 0x00)
    wx, wy, wz = struct.unpack_from("<3f", buf, base + 0x10)
    sx, sy, sz = struct.unpack_from("<3f", buf, base + 0x1C)
    return BoneTransform(q=(qx, qy, qz, qw), wp=(wx, wy, wz), scale=(sx, sy, sz))


def _scan_bone_array(
    meta: bytes, payload: bytes,
) -> tuple[int, int, int] | None:
    """Locate the ``ptBoneStructure`` array in the (meta, payload) pair.

    Searches the meta for a 16-byte DT_VARIABLEARRAY header (8 zero
    bytes + dataOffset + dataSize) where:
      * ``dataSize`` is a non-zero multiple of ``sizeof(BoneStructure)``,
      * ``dataOffset + dataSize`` lies within the payload, and
      * the first record at ``payload[dataOffset]`` looks like a root
        bone — ``int16`` at +0x28 equals ``-1`` (``0xFFFF``) and the
        ``transformParentRel`` quaternion at +0x94 has magnitude near 1.

    These constraints uniquely identify the bone array in every skinned
    sample we have inspected and reject every static model.

    Returns ``(meta_header_offset, payload_offset, bone_count)`` or
    ``None`` when no skeleton is present.
    """
    n = len(meta)
    payload_n = len(payload)
    for off in range(0, n - 16, 4):
        h0, h1 = struct.unpack_from("<2I", meta, off)
        if h0 != 0 or h1 != 0:
            continue
        do, ds = struct.unpack_from("<2I", meta, off + 8)
        if do == 0 or ds == 0 or ds % _BONE_STRUCT_SIZE != 0:
            continue
        if do + ds > payload_n:
            continue
        if do + _BONE_STRUCT_SIZE > payload_n:
            continue
        parent = struct.unpack_from("<h", payload, do + _BONE_PARENT_OFFSET)[0]
        if parent != -1:
            continue
        try:
            qx, qy, qz, qw = struct.unpack_from(
                "<4f", payload, do + _BONE_LOCAL_TRS_OFFSET,
            )
        except struct.error:
            continue
        mag2 = qx * qx + qy * qy + qz * qz + qw * qw
        if not (0.5 < mag2 < 1.5):
            continue
        return off, do, ds // _BONE_STRUCT_SIZE
    return None


def _read_bonedata_header(meta: bytes, ptr_offset: int) -> tuple[int, int, int]:
    """Read ``(template_id, nBaseBoneCount, nClothBoneCount)`` from the
    surrounding ``BoneData`` struct.

    ``ptr_offset`` is the meta offset of the ``ptBoneStructure``
    DT_VARIABLEARRAY header (returned by :func:`_scan_bone_array`).  In
    BoneData, ``ptBoneStructure`` lives at +0x08, so the BoneData head
    is ``ptr_offset - 0x08``.  Falls back to all-zero when out of bounds.
    """
    bd_head = ptr_offset - _BONEDATA_PTBONESTRUCT_OFFSET
    if bd_head < 0 or bd_head + _BONEDATA_NCLOTH_OFFSET + 4 > len(meta):
        return 0, 0, 0
    template_id = struct.unpack_from(
        "<I", meta, bd_head + _BONEDATA_TEMPLATE_OFFSET,
    )[0]
    n_base = struct.unpack_from(
        "<i", meta, bd_head + _BONEDATA_NBASE_OFFSET,
    )[0]
    n_cloth = struct.unpack_from(
        "<i", meta, bd_head + _BONEDATA_NCLOTH_OFFSET,
    )[0]
    return template_id, max(0, n_base), max(0, n_cloth)


def parse_skeleton(meta: bytes, payload: bytes) -> Skeleton | None:
    """Locate and decode the embedded skeleton, if any.

    Returns ``None`` for static models (no ``BoneData`` array, or empty
    ``ptBoneStructure``).  For skinned models, returns a fully populated
    :class:`Skeleton` with one :class:`Bone` per record in storage order.

    Bone names are not stored in the binary — only the ``dwHash`` of the
    name.  ``Bone.name`` falls back to ``"bone_<hash:08x>"``; resolving
    to a readable string requires the d4data name dictionary at a higher
    layer.
    """
    located = _scan_bone_array(meta, payload)
    if located is None:
        return None
    meta_off, payload_off, count = located
    if count == 0:
        return None

    template_id, n_base, n_cloth = _read_bonedata_header(meta, meta_off)

    bones: list[Bone] = []
    for i in range(count):
        rec = payload_off + i * _BONE_STRUCT_SIZE
        name_hash = struct.unpack_from(
            "<I", payload, rec + _BONE_HASH_OFFSET,
        )[0]
        flags = struct.unpack_from(
            "<I", payload, rec + _BONE_FLAGS_OFFSET,
        )[0]
        parent = struct.unpack_from(
            "<h", payload, rec + _BONE_PARENT_OFFSET,
        )[0]
        lod = struct.unpack_from(
            "<h", payload, rec + _BONE_LOD_OFFSET,
        )[0]
        local = _read_prstransform(payload, rec + _BONE_LOCAL_TRS_OFFSET)
        inv_bind = _read_prstransform(payload, rec + _BONE_INV_BIND_TRS_OFFSET)
        bones.append(Bone(
            index=i,
            parent_index=parent,        # already -1 for root via int16 sign
            name_hash=name_hash,
            flags=flags,
            lod=lod,
            local_trs=local,
            inv_bind_trs=inv_bind,
        ))

    return Skeleton(
        bones=bones,
        base_bone_count=n_base or count,
        cloth_bone_count=n_cloth,
        template_id=template_id,
        bone_array_offset=payload_off,
    )


# ─── Top-level parse ─────────────────────────────────────────────────

def parse_app(
    meta_path: Path, data_path: Path,
    *, allow_link_mismatch: bool = False,
) -> MeshData:
    """Parse a paired (meta, payload) .app set into a MeshData / D4Model.

    Coordinates remain D4-native (left-handed Z-up).  Apply
    ``convert_to_gltf_coords`` to positions/normals/tangents at export
    time when targeting glTF.

    The authoritative path uses the meta's GeoChunkVertexBuffer /
    GeoChunkIndexBuffer / SubObject records to pick LOD0 (the buffer
    with ``fOptional=1``) and emit one Submesh per LOD0 SubObject with
    its ``nMaterialIndex`` populated.  Synthetic test fixtures that
    don't include those structures fall through to the legacy
    descriptor-scan + index-break-heuristic path.

    Args:
        meta_path: Path to the meta .app file.
        data_path: Path to the payload .app file.
        allow_link_mismatch: When *True*, a meta/payload link-ID mismatch
            is logged as a warning instead of raising.  Set this for
            shared-payload models, where the meta and payload are
            distinct SNO entries that share geometry by design.
    """
    meta_bytes = meta_path.read_bytes()
    payload_bytes = data_path.read_bytes()

    meta_hdr = parse_meta_header(meta_bytes)
    payload_hdr = parse_payload_header(payload_bytes)

    if meta_hdr.link_id != payload_hdr.link_id:
        msg = (
            f"Link ID mismatch: meta=0x{meta_hdr.link_id:X}, "
            f"payload=0x{payload_hdr.link_id:X}"
        )
        if not allow_link_mismatch:
            raise AppFormatError(msg)
        log.warning("%s (allowed: shared payload)", msg)

    name = meta_path.stem
    primary = _build_lod0_mesh(
        name, meta_bytes, payload_bytes, meta_hdr, payload_hdr,
    )
    if primary is not None:
        return primary

    return _parse_app_legacy(
        name, meta_bytes, payload_bytes, meta_hdr, payload_hdr,
    )


def _parse_app_legacy(
    name: str,
    meta_bytes: bytes,
    payload_bytes: bytes,
    meta_hdr: MetaHeader,
    payload_hdr: PayloadHeader,
) -> MeshData:
    """Legacy descriptor-scan + index-break heuristic path.

    Used for synthetic test fixtures whose meta files do not contain
    proper GeoChunkVertexBuffer / SubObject structures.  Real .app
    files always take the authoritative LOD0 path in
    ``_build_lod0_mesh`` instead.
    """
    config = parse_config_block(meta_bytes, meta_hdr)

    voff, vcount, ioff, icount, ifmt, stride = _find_vertex_and_index_buffers(
        payload_bytes, payload_hdr, config, meta=meta_bytes
    )

    descriptors = _extract_mesh_descriptors(meta_bytes, len(payload_bytes))
    lod0 = _pick_lod0(descriptors, payload_hdr.header_data_size)
    search_origin = lod0.sentinel_offset if lod0 is not None else 0
    layout = resolve_vertex_layout(stride, meta_bytes, search_origin)

    streams = _decode_vertices_layout(payload_bytes, voff, vcount, layout)

    if ifmt == INDEX_FORMAT_U16:
        raw_indices = _decode_indices_u16(payload_bytes, ioff, icount)
    else:
        raw_indices = _decode_indices_u32(payload_bytes, ioff, icount)

    if payload_hdr.group_count > 0:
        breaks = detect_submesh_breaks(raw_indices)
        if len(breaks) > 1:
            adjusted, _bases = apply_submesh_offsets(raw_indices, breaks)
            adj_max = max(adjusted) if adjusted else 0
            coverage = adj_max / vcount if vcount else 0
            if adj_max < vcount and coverage >= 0.85:
                indices = adjusted
            else:
                indices = raw_indices
                breaks = [0]
        else:
            indices = raw_indices
    else:
        breaks = [0]
        indices = raw_indices

    cleaned: list[int] = []
    for i in range(0, len(indices) - 2, 3):
        a, b, c = indices[i], indices[i + 1], indices[i + 2]
        if a == b or b == c or a == c:
            continue
        cleaned.extend((a, b, c))
    indices = cleaned

    submeshes = _build_submeshes(breaks, len(indices), vcount)

    return MeshData(
        name=name,
        vertex_count=vcount,
        index_count=len(indices),
        submesh_count=max(payload_hdr.group_count, len(breaks)),
        layout=layout,
        positions=streams.get(SEM_POSITION, []),
        normals=_renormalize_xyz(streams.get(SEM_NORMAL, [])),
        tangents=_renormalize_xyz(streams.get(SEM_TANGENT, [])),
        colors=streams.get(SEM_COLOR_0, []),
        colors_1=streams.get(SEM_COLOR_1, []),
        uvs=streams.get(SEM_TEXCOORD_0, []),
        uvs_1=streams.get(SEM_TEXCOORD_1, []),
        joints=streams.get(SEM_BLENDINDICES, []),
        weights=streams.get(SEM_BLENDWEIGHTS, []),
        indices=indices,
        submesh_breaks=breaks,
        submeshes=submeshes,
        meta_header=meta_hdr,
        payload_header=payload_hdr,
        skeleton=parse_skeleton(meta_bytes, payload_bytes),
    )


def _build_submeshes(
    breaks: list[int], total_indices: int, vcount: int,
) -> list[Submesh]:
    """Convert index-buffer break positions into Submesh records."""
    if not breaks:
        return [Submesh(vertex_count=vcount, index_count=total_indices)]
    out: list[Submesh] = []
    for i, start in enumerate(breaks):
        end = breaks[i + 1] if i + 1 < len(breaks) else total_indices
        out.append(Submesh(
            vertex_offset=0,
            vertex_count=vcount,
            index_offset=start,
            index_count=max(0, end - start),
            material_index=i,
        ))
    return out


def identify_app_version(meta_path: Path) -> str:
    """Detect the .app format version from the file header."""
    data = meta_path.read_bytes()
    header = parse_meta_header(data)
    if header.struct_table_offset == 0x138 and header.struct_entry_size == 88:
        return "d4_v1"
    return "d4_unknown"
