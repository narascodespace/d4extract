"""Parser for Diablo IV .ani animation files.

The .ani format mirrors .app's meta/payload split.  The meta JSON in
d4data carries the ``AnimPermutation`` metadata (frame rate, bone count,
keyframe count, compression mode) and a pointer (``ptPayloadData``)
that points into the binary payload at byte ``dataOffset``.  At that
offset sits a 208-byte ``AnimPayloadData`` header — 13 consecutive
``DT_VARIABLEARRAY`` records (16 bytes each, ``[pad4][pad4][off:u32]
[size:u32]``) whose ``off`` fields are absolute byte offsets into the
payload file.

This module reads that header, follows each sub-array, parses the
fixed-size content (bone hashes, root-motion floats, depth-of-field),
captures the per-bone curve descriptors (translation/rotation/scale),
and decodes the per-frame transforms.  ``flCompression`` 0, 1, 2, 3, 4,
5 and 6 all share one short-form curve layout (see
``docs/research-flcompression5.md`` and ``docs/research-flcompression1.md``);
long-form (>255-frame) animations use a u16-timestamp layout that is not
yet decoded.

**Phase 4 finding:** comp=3 turned out to use the *same* byte layout
as comp=0 — translation and rotation curves are decoded by the
identical reader.  Comp=3 just stores far sparser keyframes (e.g.
14 timestamps for a 61-frame curve), which is where its 6× density
advantage comes from.  Scale is the only channel where the two modes
diverge: a non-empty comp=3 scale curve drops the 12-byte float32
baseline that translation carries, so its data section starts right
after the 2-byte ``[u8 count][u8 fmt]`` header + timestamps.

flCompression=0 layout — per-bone, per-channel:

Translation curve (format byte = 0x04)::

    [u8  count]
    [u8  format = 0x04]
    [u16 pad = 0]
    [float32 baseline_x][float32 baseline_y][float32 baseline_z]
    if count >= 2:
        [u8  timestamps[count]]      # explicit frame indices for each key
        [pad to 4-byte alignment]
        [float32 vec3 [count]]       # absolute positions per keyframe
    # count == 1 means a constant curve; the baseline IS the value.

Rotation curve (no format byte — count itself signals constant)::

    [u16 count]
    if count == 1:
        [int16 quat[4] / 32767]      # X, Y, Z, W at offset 2
        [u16 trailer = 0]            # pad to 4-byte total
    else:
        [u8  timestamps[count - 1]]  # frames 1..N (frame 0 is implicit)
        [pad to 2-byte alignment]    # int16x4 only needs 2-byte align;
                                     # 4-byte align overshoots when
                                     # ``2 + ts_count`` is odd
        [int16 quat[4] / 32767] × count

Scale curves: empty for every comp=0 file we've sampled — interpreted
as "use the rest-pose scale".

Authoritative reference: ``skills/d4-animation-extraction/SKILL.md`` §
"AnimPayloadData struct (hash 2313381993, 208 bytes)" and § "Curve
storage".  The comp=0 layout above was reverse-engineered against
``CMP_dogLarge_ui_loadingScreen_pose_01`` (58 bones, static pose) and
``Chimera_lionsnake_attk_basic`` (134 bones, 2 permutations); see
``analysis/anim_decode_validate.py`` for the verification script.
"""

from __future__ import annotations

import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from d4extract.formats.app_parser import Skeleton

log = logging.getLogger(__name__)

# ─── Format constants ────────────────────────────────────────────────

# AnimPayloadData carries 13 DT_VARIABLEARRAY records, each 16 bytes.
ANIM_PAYLOAD_HEADER_SIZE = 208
DT_VARIABLEARRAY_SIZE = 16
ANIM_PAYLOAD_ARRAY_COUNT = ANIM_PAYLOAD_HEADER_SIZE // DT_VARIABLEARRAY_SIZE  # 13

# Per-array slot positions inside the 208-byte header.
SLOT_BONE_NAMES = 0
SLOT_UNK_A9EAD38 = 1
SLOT_UNK_8C9E18F = 2
SLOT_NONLINEAR_OFFSET = 3
SLOT_UNK_5CD81C8 = 4
SLOT_UNK_FA7ECFB = 5
SLOT_FACING_YAW = 6
SLOT_ROOT_SCALE = 7
SLOT_UNK_7C60205 = 8
SLOT_DEPTH_OF_FIELD = 9
SLOT_TRANSLATION_CURVES = 10
SLOT_ROTATION_CURVES = 11
SLOT_SCALE_CURVES = 12

ANIM_SLOT_NAMES: tuple[str, ...] = (
    "ptBoneNames",
    "unk_a9ead38",
    "unk_8c9e18f",
    "pwvNonlinearOffset",
    "unk_5cd81c8",
    "unk_fa7ecfb",
    "pflFacingYaw",
    "pflRootScale",
    "unk_7c60205",
    "ptDepthOfField",
    "ptTranslationCurves",
    "ptRotationCurves",
    "ptScaleCurves",
)
assert len(ANIM_SLOT_NAMES) == ANIM_PAYLOAD_ARRAY_COUNT

# Element sizes in bytes for sanity checks and downstream readers.
ANIM_SLOT_ELEMENT_SIZES: tuple[int, ...] = (
    4,   # ptBoneNames           — uint32
    4,   # unk_a9ead38           — float
    4,   # unk_8c9e18f           — float
    12,  # pwvNonlinearOffset    — vec3 per frame
    12,  # unk_5cd81c8           — vec3 per frame
    12,  # unk_fa7ecfb           — vec3 per frame
    4,   # pflFacingYaw          — float per frame
    4,   # pflRootScale          — float per frame
    4,   # unk_7c60205           — float per frame
    8,   # ptDepthOfField        — fStop + focalDistance (2 × float)
    16,  # ptTranslationCurves   — TranslationCurve (DT_VARIABLEARRAY)
    16,  # ptRotationCurves      — RotationCurve (DT_VARIABLEARRAY)
    16,  # ptScaleCurves         — ScaleCurve (DT_VARIABLEARRAY)
)

# Compression modes the curve decoders support.  The
# d4-animation-extraction skill originally documented modes 0
# (uncompressed) and 3 (quantized).  Cross-mode curve-blob aliasing
# research (``docs/research-flcompression5.md`` and
# ``docs/research-flcompression1.md``) proved modes 1, 2, 4, 5 and 6 are
# all byte-identical to mode 3 for short-form animations — flCompression
# does not control curve encoding at all.
#
# The real binary fork is timestamp width, not compression mode — see
# LONG_FORM_FRAME_LIMIT below.
_SUPPORTED_COMPRESSION: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)

# Curve keyframe timestamps are stored as u8 when an animation has this
# many frames or fewer, and as u16 above it (a u8 cannot index frame
# 256+).  The decoder implements only the u8 "short-form" layout, so
# ``decode_permutation`` rejects >255-frame "long-form" permutations
# rather than silently mis-decoding them.  Implementing the u16 layout
# is a separate project — see ``docs/research-flcompression1.md`` §10.
LONG_FORM_FRAME_LIMIT = 255


class AnimFormatError(Exception):
    """Raised when an .ani file (meta JSON or payload) is malformed."""


# ─── Dataclasses ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class AnimVarArray:
    """One ``DT_VARIABLEARRAY`` header — a pointer into the payload.

    ``data_offset`` is an absolute byte offset into the payload file.
    ``data_size`` is in bytes.  An empty/absent array is signalled by
    ``data_size == 0`` (``data_offset`` is ignored in that case).
    """

    data_offset: int
    data_size: int

    @property
    def is_empty(self) -> bool:
        return self.data_size == 0


@dataclass
class AnimCurve:
    """One bone's translation, rotation, or scale curve.

    The keyframe data is stored as a raw byte blob whose encoding
    depends on the parent permutation's ``flCompression`` mode.  Phase
    2 only captures the bytes; Phase 3 will decode them.
    """

    keys_offset: int
    keys_size: int
    raw_keys: bytes = b""

    @property
    def is_empty(self) -> bool:
        return self.keys_size == 0


@dataclass
class AnimDepthOfField:
    """Per-frame depth-of-field samples (fStop + focal distance)."""

    f_stop: list[float] = field(default_factory=list)
    focal_distance: list[float] = field(default_factory=list)

    @property
    def frame_count(self) -> int:
        return len(self.f_stop)


@dataclass
class AnimPayloadHeader:
    """Parsed 208-byte ``AnimPayloadData`` header with resolved sub-arrays."""

    bone_names: list[int] = field(default_factory=list)
    translation_curves: list[AnimCurve] = field(default_factory=list)
    rotation_curves: list[AnimCurve] = field(default_factory=list)
    scale_curves: list[AnimCurve] = field(default_factory=list)

    # Root motion (per-frame; may be empty).
    nonlinear_offsets: list[tuple[float, float, float]] = field(
        default_factory=list,
    )
    facing_yaw: list[float] = field(default_factory=list)
    root_scale: list[float] = field(default_factory=list)

    # Depth-of-field samples (per frame; usually empty).
    depth_of_field: AnimDepthOfField = field(default_factory=AnimDepthOfField)

    # All 13 raw header records, indexed by ``SLOT_*``, kept verbatim
    # so that future research on the unk_* fields has a starting point.
    raw_arrays: list[AnimVarArray] = field(default_factory=list)


@dataclass
class AnimPermutationData:
    """One fully parsed animation permutation.

    Combines the meta-side scalars (frame rate, compression, counts)
    with the binary header parsed out of the payload.
    """

    frame_rate: float
    compression: int
    bone_count: int
    keyframe_count: int
    cycle_count: int
    payload_offset: int
    permutation_index: int
    anim_to_structure: list[int] | None
    header: AnimPayloadHeader

    # Carry the originating filenames so diagnostics and tests can
    # print "Animation: <stem>" without the caller passing them in
    # again.  Optional because synthetic test fixtures don't have
    # paths.
    name: str = ""
    payload_size: int = 0


# ─── Low-level reads ─────────────────────────────────────────────────


def _read_var_array(payload: bytes, offset: int) -> AnimVarArray:
    """Decode one 16-byte ``DT_VARIABLEARRAY`` header.

    Layout: ``[pad4][pad4][dataOffset:u32][dataSize:u32]``.  The two
    pad uint32s are typically zero but we don't validate them — the
    engine is the source of truth and we don't want to brick on a
    non-zero sentinel.
    """
    if offset + DT_VARIABLEARRAY_SIZE > len(payload):
        raise AnimFormatError(
            f"DT_VARIABLEARRAY at offset {offset} extends past end of "
            f"payload ({len(payload)} bytes)"
        )
    _pad0, _pad1, data_offset, data_size = struct.unpack_from(
        "<4I", payload, offset,
    )
    return AnimVarArray(data_offset=data_offset, data_size=data_size)


def _slice_payload(
    payload: bytes, arr: AnimVarArray, *, label: str,
) -> bytes:
    """Return ``payload[arr.data_offset : arr.data_offset + arr.data_size]``.

    Raises ``AnimFormatError`` if the slice runs past the end of the
    payload.  Empty arrays return ``b""`` without bounds-checking.
    """
    if arr.is_empty:
        return b""
    end = arr.data_offset + arr.data_size
    if arr.data_offset < 0 or end > len(payload):
        raise AnimFormatError(
            f"{label} sub-array out of range: "
            f"offset={arr.data_offset}, size={arr.data_size}, "
            f"payload_size={len(payload)}"
        )
    return bytes(payload[arr.data_offset:end])


def _check_element_size(
    arr: AnimVarArray, element_size: int, *, label: str,
) -> None:
    """Warn if ``arr.data_size`` is not a multiple of ``element_size``.

    Treated as a soft check — we still parse what we can — because a
    misaligned tail is more informative than an outright failure when
    we don't yet fully understand every field.
    """
    if arr.is_empty:
        return
    if arr.data_size % element_size != 0:
        log.warning(
            "%s size %d is not a multiple of element size %d",
            label, arr.data_size, element_size,
        )


# ─── Sub-array decoders ──────────────────────────────────────────────


def _decode_uint32_array(blob: bytes) -> list[int]:
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack_from(f"<{n}I", blob, 0))


def _decode_float_array(blob: bytes) -> list[float]:
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack_from(f"<{n}f", blob, 0))


def _decode_vec3_array(blob: bytes) -> list[tuple[float, float, float]]:
    if not blob:
        return []
    n = len(blob) // 12
    out: list[tuple[float, float, float]] = []
    for i in range(n):
        x, y, z = struct.unpack_from("<3f", blob, i * 12)
        out.append((x, y, z))
    return out


def _decode_depth_of_field(blob: bytes) -> AnimDepthOfField:
    """``AnimDepthOfField`` = pair of float32 per frame (fStop, focal)."""
    if not blob:
        return AnimDepthOfField()
    n = len(blob) // 8
    f_stop: list[float] = []
    focal: list[float] = []
    for i in range(n):
        a, b = struct.unpack_from("<2f", blob, i * 8)
        f_stop.append(a)
        focal.append(b)
    return AnimDepthOfField(f_stop=f_stop, focal_distance=focal)


def _decode_curve_list(
    payload: bytes, arr: AnimVarArray, *, label: str,
) -> list[AnimCurve]:
    """Decode an array of 16-byte curve descriptors and follow each.

    Each element is itself a ``DT_VARIABLEARRAY`` whose
    ``[dataOffset, dataSize]`` points at the raw ``ptKeysComp`` byte
    blob.  We capture those bytes verbatim — the actual quantization /
    delta / smallest-3 layout is a Phase 3 problem.
    """
    blob = _slice_payload(payload, arr, label=label)
    if not blob:
        return []
    if len(blob) % DT_VARIABLEARRAY_SIZE != 0:
        log.warning(
            "%s: array size %d is not a multiple of %d",
            label, len(blob), DT_VARIABLEARRAY_SIZE,
        )
    count = len(blob) // DT_VARIABLEARRAY_SIZE
    curves: list[AnimCurve] = []
    for i in range(count):
        _p0, _p1, koff, ksize = struct.unpack_from(
            "<4I", blob, i * DT_VARIABLEARRAY_SIZE,
        )
        if ksize == 0:
            curves.append(AnimCurve(keys_offset=koff, keys_size=0))
            continue
        end = koff + ksize
        if koff < 0 or end > len(payload):
            raise AnimFormatError(
                f"{label}[{i}].ptKeysComp out of range: "
                f"offset={koff}, size={ksize}, "
                f"payload_size={len(payload)}"
            )
        curves.append(AnimCurve(
            keys_offset=koff,
            keys_size=ksize,
            raw_keys=bytes(payload[koff:end]),
        ))
    return curves


# ─── Header parser ───────────────────────────────────────────────────


def parse_anim_payload_header(
    payload: bytes, payload_offset: int,
) -> AnimPayloadHeader:
    """Parse the 208-byte ``AnimPayloadData`` header at ``payload_offset``.

    The 13 sub-arrays are followed and decoded into typed Python
    structures.  The unk_* slots are kept as raw ``AnimVarArray``
    pointers in :attr:`AnimPayloadHeader.raw_arrays` (no field-specific
    accessors yet — promote to typed when their meaning is known).
    """
    if payload_offset < 0:
        raise AnimFormatError(
            f"Negative payload_offset {payload_offset}",
        )
    end = payload_offset + ANIM_PAYLOAD_HEADER_SIZE
    if end > len(payload):
        raise AnimFormatError(
            f"AnimPayloadData header at offset {payload_offset} would "
            f"extend to {end}, past end of payload ({len(payload)} bytes)"
        )

    # Decode all 13 records up-front so error messages can reference
    # them by slot name rather than by an opaque index.
    raw: list[AnimVarArray] = [
        _read_var_array(payload, payload_offset + i * DT_VARIABLEARRAY_SIZE)
        for i in range(ANIM_PAYLOAD_ARRAY_COUNT)
    ]
    for i, arr in enumerate(raw):
        _check_element_size(
            arr, ANIM_SLOT_ELEMENT_SIZES[i], label=ANIM_SLOT_NAMES[i],
        )

    # Bone hashes (uint32 each).
    bone_blob = _slice_payload(
        payload, raw[SLOT_BONE_NAMES], label=ANIM_SLOT_NAMES[SLOT_BONE_NAMES],
    )
    bone_names = _decode_uint32_array(bone_blob)

    # Root motion (per-frame).
    nonlinear_offsets = _decode_vec3_array(_slice_payload(
        payload, raw[SLOT_NONLINEAR_OFFSET],
        label=ANIM_SLOT_NAMES[SLOT_NONLINEAR_OFFSET],
    ))
    facing_yaw = _decode_float_array(_slice_payload(
        payload, raw[SLOT_FACING_YAW],
        label=ANIM_SLOT_NAMES[SLOT_FACING_YAW],
    ))
    root_scale = _decode_float_array(_slice_payload(
        payload, raw[SLOT_ROOT_SCALE],
        label=ANIM_SLOT_NAMES[SLOT_ROOT_SCALE],
    ))

    depth_of_field = _decode_depth_of_field(_slice_payload(
        payload, raw[SLOT_DEPTH_OF_FIELD],
        label=ANIM_SLOT_NAMES[SLOT_DEPTH_OF_FIELD],
    ))

    translation_curves = _decode_curve_list(
        payload, raw[SLOT_TRANSLATION_CURVES],
        label=ANIM_SLOT_NAMES[SLOT_TRANSLATION_CURVES],
    )
    rotation_curves = _decode_curve_list(
        payload, raw[SLOT_ROTATION_CURVES],
        label=ANIM_SLOT_NAMES[SLOT_ROTATION_CURVES],
    )
    scale_curves = _decode_curve_list(
        payload, raw[SLOT_SCALE_CURVES],
        label=ANIM_SLOT_NAMES[SLOT_SCALE_CURVES],
    )

    return AnimPayloadHeader(
        bone_names=bone_names,
        translation_curves=translation_curves,
        rotation_curves=rotation_curves,
        scale_curves=scale_curves,
        nonlinear_offsets=nonlinear_offsets,
        facing_yaw=facing_yaw,
        root_scale=root_scale,
        depth_of_field=depth_of_field,
        raw_arrays=raw,
    )


# ─── Meta JSON loader ────────────────────────────────────────────────


def _load_meta_permutation(
    meta_json_path: Path, permutation_index: int,
) -> tuple[dict, str]:
    """Return ``(permutation_dict, anim_stem)`` from the meta JSON.

    Raises ``AnimFormatError`` for missing fields or out-of-range
    permutation indices so the caller's error path is uniform.
    """
    try:
        with meta_json_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise AnimFormatError(
            f"Could not read animation meta JSON {meta_json_path}: {exc}"
        ) from exc

    perms = meta.get("ptPermutations")
    if not isinstance(perms, list) or not perms:
        raise AnimFormatError(
            f"{meta_json_path}: ptPermutations missing or empty"
        )
    if permutation_index < 0 or permutation_index >= len(perms):
        raise AnimFormatError(
            f"{meta_json_path}: permutation_index {permutation_index} "
            f"out of range (anim has {len(perms)} permutation(s))"
        )

    perm = perms[permutation_index]
    if not isinstance(perm, dict):
        raise AnimFormatError(
            f"{meta_json_path}: permutation {permutation_index} is not a JSON object"
        )

    # The d4data filename is "<stem>.ani" — strip both extensions.
    anim_stem = meta_json_path.name
    for ext in (".json", ".ani"):
        if anim_stem.endswith(ext):
            anim_stem = anim_stem[: -len(ext)]
    return perm, anim_stem


def _extract_payload_pointer(perm: dict) -> tuple[int, int]:
    """Return ``(dataOffset, dataSize)`` from ``perm.ptPayloadData.value``."""
    pt = perm.get("ptPayloadData")
    if not isinstance(pt, dict):
        raise AnimFormatError("permutation missing ptPayloadData")
    val = pt.get("value")
    if not isinstance(val, dict):
        raise AnimFormatError("ptPayloadData.value missing")
    try:
        data_offset = int(val["dataOffset"])
        data_size = int(val["dataSize"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AnimFormatError(
            f"ptPayloadData.value missing dataOffset/dataSize: {exc}"
        ) from exc
    if data_size != ANIM_PAYLOAD_HEADER_SIZE:
        log.warning(
            "ptPayloadData.dataSize=%d, expected %d — proceeding anyway",
            data_size, ANIM_PAYLOAD_HEADER_SIZE,
        )
    return data_offset, data_size


def _coerce_anim_to_structure(perm: dict) -> list[int] | None:
    """Best-effort decode of ``pnAnimToStructure``.

    In d4data JSON this is rendered as the string ``"0"`` (i.e. an
    external pointer into the binary section of the meta file we don't
    parse here).  When it shows up as a JSON list we surface it; in
    every other shape we return ``None`` and let downstream phases
    locate the array if/when they need it.
    """
    raw = perm.get("pnAnimToStructure")
    if isinstance(raw, list):
        try:
            return [int(x) for x in raw]
        except (TypeError, ValueError):
            return None
    return None


# ─── Top-level entry point ───────────────────────────────────────────


def parse_anim(
    meta_json_path: Path,
    payload_path: Path,
    *,
    permutation_index: int = 0,
) -> AnimPermutationData:
    """Parse an animation meta+payload pair into an ``AnimPermutationData``.

    Args:
        meta_json_path: Path to ``base/meta/Anim/<name>.ani.json``
            in d4data (the human-readable JSON dump of the .ani meta).
        payload_path: Path to ``base/payload/Anim/<name>.ani`` extracted
            from CASC.  May contain multiple permutations'
            ``AnimPayloadData`` headers at distinct byte offsets.
        permutation_index: Which ``ptPermutations[]`` entry to parse.
            ``0`` is the canonical / first variant.

    Raises:
        AnimFormatError: Meta JSON missing required fields, payload
            file truncated, or any sub-array points outside the
            payload.
    """
    perm, anim_stem = _load_meta_permutation(meta_json_path, permutation_index)
    payload_offset, _payload_size = _extract_payload_pointer(perm)

    try:
        payload_bytes = payload_path.read_bytes()
    except OSError as exc:
        raise AnimFormatError(
            f"Could not read animation payload {payload_path}: {exc}"
        ) from exc

    header = parse_anim_payload_header(payload_bytes, payload_offset)

    bone_count = int(perm.get("nBoneCount", 0))
    keyframe_count = int(perm.get("nKeyframeCount", 0))
    cycle_count = int(perm.get("nCycleCount", 0))
    frame_rate = float(perm.get("flFrameRate", 0.0))
    compression = int(perm.get("flCompression", 0))

    # Length sanity checks — log, don't raise.  A real-world payload
    # could in principle ship a shorter curve list; we'd rather see the
    # mismatch in the diagnostic dump than refuse to parse.
    if bone_count and len(header.bone_names) != bone_count:
        log.warning(
            "%s p%d: bone_names length %d != nBoneCount %d",
            anim_stem, permutation_index,
            len(header.bone_names), bone_count,
        )
    for label, curves in (
        ("ptTranslationCurves", header.translation_curves),
        ("ptRotationCurves", header.rotation_curves),
        ("ptScaleCurves", header.scale_curves),
    ):
        if bone_count and len(curves) != bone_count:
            log.warning(
                "%s p%d: %s length %d != nBoneCount %d",
                anim_stem, permutation_index, label,
                len(curves), bone_count,
            )

    return AnimPermutationData(
        frame_rate=frame_rate,
        compression=compression,
        bone_count=bone_count,
        keyframe_count=keyframe_count,
        cycle_count=cycle_count,
        payload_offset=payload_offset,
        permutation_index=permutation_index,
        anim_to_structure=_coerce_anim_to_structure(perm),
        header=header,
        name=anim_stem,
        payload_size=len(payload_bytes),
    )


# ─── Cross-reference helpers ─────────────────────────────────────────


def cross_reference_bone_hashes(
    bone_hashes: list[int], skeleton_hashes: list[int],
) -> tuple[list[int], list[int]]:
    """Compare animation bone hashes against a skeleton's ``dwHash`` set.

    Returns ``(matched, unmatched)`` — each a sub-list of
    ``bone_hashes`` preserving original order.  Empty
    ``skeleton_hashes`` returns ``([], list(bone_hashes))`` so the
    caller can distinguish "no skeleton supplied" from "skeleton
    supplied, no overlap".
    """
    if not skeleton_hashes:
        return [], list(bone_hashes)
    available = set(skeleton_hashes)
    matched: list[int] = []
    unmatched: list[int] = []
    for h in bone_hashes:
        (matched if h in available else unmatched).append(h)
    return matched, unmatched


# ─── Diagnostic formatting ───────────────────────────────────────────


def _format_hex(blob: bytes, limit: int) -> str:
    """Render ``blob`` as ``AB CD EF …`` truncated at ``limit`` bytes."""
    if not blob:
        return "(empty)"
    if limit <= 0 or len(blob) <= limit:
        return " ".join(f"{b:02X}" for b in blob)
    head = " ".join(f"{b:02X}" for b in blob[:limit])
    return f"{head} …(+{len(blob) - limit} bytes)"


def format_diagnostic(
    perm: AnimPermutationData,
    *,
    hex_limit: int = 64,
    max_curves: int | None = None,
) -> str:
    """Render a parsed permutation as a human-readable diagnostic block.

    ``hex_limit`` is the maximum number of curve bytes to display per
    bone.  ``max_curves`` caps the number of bones whose curves are
    rendered (``None`` = all); useful when dumping a 190-bone
    barbarian idle without flooding the terminal.
    """
    h = perm.header
    lines: list[str] = []
    name = perm.name or "<anonymous>"
    lines.append(f"Animation: {name}")
    lines.append(
        f"  Permutation {perm.permutation_index}: "
        f"{perm.bone_count} bones, {perm.keyframe_count} frames, "
        f"comp={perm.compression}, {perm.frame_rate:g}fps, "
        f"cycles={perm.cycle_count}"
    )
    lines.append(
        f"  Payload offset: {perm.payload_offset}, "
        f"header size: {ANIM_PAYLOAD_HEADER_SIZE}, "
        f"payload size: {perm.payload_size}"
    )
    if perm.anim_to_structure is not None:
        lines.append(
            f"  pnAnimToStructure: {len(perm.anim_to_structure)} entries "
            f"(inline list)"
        )
    else:
        lines.append("  pnAnimToStructure: external (not parsed)")

    lines.append("")
    lines.append("  AnimPayloadData arrays:")
    label_w = max(len(n) for n in ANIM_SLOT_NAMES)
    for i, arr in enumerate(h.raw_arrays):
        elem_size = ANIM_SLOT_ELEMENT_SIZES[i]
        count = arr.data_size // elem_size if elem_size else 0
        lines.append(
            f"    [{i:>2}] {ANIM_SLOT_NAMES[i]:<{label_w}}  "
            f"offset={arr.data_offset:>8}  size={arr.data_size:>6}  "
            f"({count} × {elem_size}B)"
        )

    lines.append("")
    lines.append(f"  Bone hashes ({len(h.bone_names)}):")
    for i, hash_val in enumerate(h.bone_names):
        lines.append(f"    [{i:>3}] 0x{hash_val:08X}")

    def _curve_section(title: str, curves: list[AnimCurve]) -> None:
        lines.append("")
        lines.append(f"  {title} ({len(curves)}):")
        rendered = curves if max_curves is None else curves[:max_curves]
        for i, c in enumerate(rendered):
            lines.append(
                f"    bone[{i:>3}]: keys_offset={c.keys_offset:>8}  "
                f"keys_size={c.keys_size:>5}  "
                f"raw: {_format_hex(c.raw_keys, hex_limit)}"
            )
        if max_curves is not None and len(curves) > max_curves:
            lines.append(
                f"    … ({len(curves) - max_curves} more bones omitted)"
            )

    _curve_section("Translation curves", h.translation_curves)
    _curve_section("Rotation curves", h.rotation_curves)
    _curve_section("Scale curves", h.scale_curves)

    if h.nonlinear_offsets or h.facing_yaw or h.root_scale:
        lines.append("")
        lines.append("  Root motion:")
        if h.nonlinear_offsets:
            sample = h.nonlinear_offsets[0]
            lines.append(
                f"    pwvNonlinearOffset: {len(h.nonlinear_offsets)} frames"
                f"  first=({sample[0]:.4f}, {sample[1]:.4f}, {sample[2]:.4f})"
            )
        if h.facing_yaw:
            lines.append(
                f"    pflFacingYaw:       {len(h.facing_yaw)} frames"
                f"  first={h.facing_yaw[0]:.4f}"
            )
        if h.root_scale:
            lines.append(
                f"    pflRootScale:       {len(h.root_scale)} frames"
                f"  first={h.root_scale[0]:.4f}"
            )
    if h.depth_of_field.frame_count:
        lines.append(
            f"  DepthOfField: {h.depth_of_field.frame_count} frames "
            f"(fStop[0]={h.depth_of_field.f_stop[0]:.4f}, "
            f"focal[0]={h.depth_of_field.focal_distance[0]:.4f})"
        )

    return "\n".join(lines)


# ─── flCompression=0 curve decoders ──────────────────────────────────


# A few aliases to keep the type annotations short.
_Vec3 = tuple[float, float, float]
_Quat = tuple[float, float, float, float]

_TRANSLATION_FMT = 0x04
_QUAT_NORMALIZER = 32767.0


def _align4(x: int) -> int:
    """Round ``x`` up to the next 4-byte boundary."""
    return (x + 3) & ~3


def _align2(x: int) -> int:
    """Round ``x`` up to the next 2-byte boundary.

    Rotation curves use this — quaternion keyframes are int16 quads
    (8 bytes), which only need 2-byte alignment.  Using 4-byte
    alignment when ``2 + ts_count`` is odd would skip 2 bytes and
    shift every component by one int16 slot (identity reads as 180°
    around Z, etc.).
    """
    return (x + 1) & ~1


def _const_per_frame(value, frame_count: int) -> np.ndarray:
    """Expand one TRS value to a ``(frame_count, dim)`` float32 array.

    Empty curves and ``count == 1`` constant curves hold the same vector
    on every frame; this is their per-frame array.  The result is a
    fresh, writable, contiguous array — ``empty`` + a broadcast assign
    rather than ``broadcast_to`` + ``copy``, which measured cheaper for
    the many static bones a typical skeleton carries.
    """
    v = np.asarray(value, dtype=np.float32).reshape(-1)
    out = np.empty((frame_count, v.shape[0]), dtype=np.float32)
    out[:] = v
    return out


def _fill_per_frame(
    frame_count: int,
    keyframes: np.ndarray,
    frame_indices: list[int],
    fallback,
) -> np.ndarray:
    """LERP-fill a ``(frame_count, dim)`` array from sparse keyframes.

    ``keyframes`` is a ``(count, dim)`` array of absolute vec3 values
    (translations and scales); ``frame_indices[k]`` is the frame row
    ``k`` lands on.  Before the first keyframe the fallback is used;
    after the last the final keyframe is held; between, values are
    linearly interpolated.  This is the vectorised replacement for the
    old per-frame Python loop — ``np.interp`` does the segment lerp in
    one call per axis, with ``left=`` covering the pre-first-keyframe
    fallback and the default ``right`` holding the last keyframe.
    """
    keyframes = np.asarray(keyframes, dtype=np.float32)
    fallback = np.asarray(fallback, dtype=np.float32).reshape(-1)
    dim = fallback.shape[0]

    # Map each in-range frame index to the keyframe row that occupies
    # it; a later row wins a tie, matching the old overwrite order.
    slot = np.full(frame_count, -1, dtype=np.int64)
    for kf_idx, frame in enumerate(frame_indices):
        if 0 <= frame < frame_count:
            slot[frame] = kf_idx

    placed = np.nonzero(slot >= 0)[0]
    if placed.size == 0:
        return np.broadcast_to(fallback, (frame_count, dim)).copy()

    placed_vals = keyframes[slot[placed]]          # (P, dim)
    xs = np.arange(frame_count, dtype=np.float64)
    xp = placed.astype(np.float64)
    out = np.empty((frame_count, dim), dtype=np.float32)
    for axis in range(dim):
        # left= → fallback before the first keyframe; right defaults to
        # fp[-1], which holds the last keyframe afterwards.
        out[:, axis] = np.interp(
            xs, xp, placed_vals[:, axis], left=float(fallback[axis]),
        )
    return out


def _fill_per_frame_slerp(
    frame_count: int,
    keyframes: np.ndarray,
    frame_indices: list[int],
    fallback,
) -> np.ndarray:
    """SLERP-fill a ``(frame_count, 4)`` quaternion array from sparse keys.

    Spherical linear interpolation between consecutive keyframes so a
    rotation follows the shortest great-circle arc; the ``dot < 0`` flip
    keeps it from travelling the long way round the sphere.  Frames
    whose bracketing keyframes are within ~1° fall back to normalised
    LERP, where SLERP's ``sin θ`` denominator loses precision.

    The whole interpolation runs as one vectorised pass over *every*
    output frame: each frame's segment is found with ``searchsorted``,
    its keyframe pair gathered, and SLERP/NLERP evaluated in bulk. A
    per-segment loop would instead pay numpy's call overhead on tiny
    (~4-frame) slices — comp=3 clips have many short segments — and
    measured several times slower than the scalar code it replaced.
    """
    keyframes = np.asarray(keyframes, dtype=np.float32)
    fallback = np.asarray(fallback, dtype=np.float32).reshape(-1)

    slot = np.full(frame_count, -1, dtype=np.int64)
    for kf_idx, frame in enumerate(frame_indices):
        if 0 <= frame < frame_count:
            slot[frame] = kf_idx

    placed = np.nonzero(slot >= 0)[0]
    out = np.empty((frame_count, 4), dtype=np.float32)
    if placed.size == 0:
        out[:] = fallback
        return out

    # Keyframe quats, in float64 for the trig. One (qa, qb) keyframe
    # pair is gathered per output frame from the segment it falls in.
    pv = keyframes[slot[placed]].astype(np.float64)     # (P, 4)
    n_placed = placed.size
    xs = np.arange(frame_count)
    seg = np.clip(
        np.searchsorted(placed, xs, side="right") - 1,
        0, max(n_placed - 2, 0),
    )
    nxt = np.minimum(seg + 1, n_placed - 1)
    qa = pv[seg]                                        # (frame_count, 4)
    qb = pv[nxt].copy()
    fa = placed[seg].astype(np.float64)
    span = (placed[nxt] - placed[seg]).astype(np.float64)
    t = np.where(span > 0.0, (xs - fa) / np.where(span > 0.0, span, 1.0), 0.0)

    # Shortest-path: flip qb where the dot is negative. The dot is
    # constant within a segment, so this matches the scalar code's
    # per-segment flip.
    dot = (qa * qb).sum(axis=1)
    qb[dot < 0.0] = -qb[dot < 0.0]
    dot = np.minimum(np.abs(dot), 1.0)
    theta = np.arccos(dot)
    small = theta < 0.0175                              # ~1° — NLERP band

    # NLERP for the near-zero-angle frames (SLERP's sin denominator is
    # unstable there); rows with a degenerate near-zero result fall back
    # to qa, exactly as the scalar version did.
    nlerp = qa * (1.0 - t)[:, None] + qb * t[:, None]
    nl_norm = np.sqrt((nlerp * nlerp).sum(axis=1))
    nlerp = np.where(
        nl_norm[:, None] > 1e-9,
        nlerp / np.where(nl_norm > 1e-9, nl_norm, 1.0)[:, None],
        qa,
    )
    # SLERP for the rest. errstate hides the sin-of-0 in the NLERP rows,
    # whose slerp values np.where discards anyway.
    with np.errstate(invalid="ignore", divide="ignore"):
        sin_theta = np.sin(theta)
        wa = np.sin((1.0 - t) * theta) / sin_theta
        wb = np.sin(t * theta) / sin_theta
    slerp = wa[:, None] * qa + wb[:, None] * qb

    res = np.where(small[:, None], nlerp, slerp)
    # Keyframe frames take their stored quat verbatim; the ends extend
    # with the fallback (before the first key) and a hold (after the last).
    res[placed] = pv
    res[xs < placed[0]] = fallback
    res[xs >= placed[-1]] = pv[-1]
    out[:] = res
    return out


def decode_translation_curve(
    curve: AnimCurve, keyframe_count: int, compression: int,
    *, rest_translation: _Vec3 = (0.0, 0.0, 0.0),
) -> np.ndarray:
    """Decode a translation curve to per-frame absolute ``(x, y, z)`` values.

    Returned values are the bone's *absolute* local translation per
    frame (ready to drop into ``trs_to_matrix`` directly).  This unifies
    the three storage variants:

    - ``count == 0`` / empty curve: every frame uses ``rest_translation``.
    - ``count == 1``: every frame uses the curve's stored baseline (an
      absolute pose that may differ from rest, e.g. dogLarge's loading-
      screen pose, or match it, e.g. the chest's "Neutral" idle).
    - ``count >= 2``: each keyframe is stored as a delta against the
      curve's own baseline; we bake ``baseline + delta`` here so the
      output is always absolute.  The baseline is the engine's
      authoritative reference; for animated bones it's effectively
      rest_t but written into the .ani file with its own float32
      precision.

    The skinning layer therefore uses these values verbatim — it must
    NOT add ``bone.local_trs.wp`` again, which would double-count the
    constant and empty cases.

    Args:
        curve: One ``AnimCurve`` from the parsed payload header.
        keyframe_count: ``nKeyframeCount`` from the meta JSON — the
            number of frames the caller wants populated.
        compression: ``flCompression`` from the meta JSON.  ``0``, ``3``
            and ``5`` use the same byte layout (comp=3 is just sparser
            keyframes; comp=5 is byte-identical to comp=3 — see
            ``docs/research-flcompression5.md``).
        rest_translation: Fallback used when the curve is empty / has
            ``count == 0``.  For curves with stored keyframes the
            curve's own baseline is the source of truth.
    """
    if compression not in _SUPPORTED_COMPRESSION:
        raise AnimFormatError(
            f"decode_translation_curve: unknown flCompression={compression}"
        )

    if curve.is_empty:
        return _const_per_frame(rest_translation, keyframe_count)

    blob = curve.raw_keys
    if len(blob) < 16:
        raise AnimFormatError(
            f"translation curve too small: {len(blob)} bytes"
        )

    count = blob[0]
    fmt = blob[1]
    # ``count == 0`` is a "no keyframes, use rest pose" curve. The
    # format byte is irrelevant in that case — empty curves observed
    # in the wild often pad with zeros, which would otherwise trip the
    # strict ``fmt == 0x04`` check below. Short-circuit here so a
    # zero-padded empty curve doesn't error out the whole animation.
    if count == 0:
        return _const_per_frame(rest_translation, keyframe_count)
    if fmt != _TRANSLATION_FMT:
        raise AnimFormatError(
            f"translation curve: unexpected format byte 0x{fmt:02X} "
            f"(expected 0x{_TRANSLATION_FMT:02X})"
        )
    baseline = struct.unpack_from("<3f", blob, 4)

    if count == 1:
        # Constant curve — every frame uses the baseline.
        return _const_per_frame(baseline, keyframe_count)

    ts_offset = 16
    timestamps = list(blob[ts_offset:ts_offset + count])
    data_offset = _align4(ts_offset + count)
    needed = data_offset + count * 12
    if needed > len(blob):
        raise AnimFormatError(
            f"translation curve truncated: need {needed} bytes, "
            f"have {len(blob)} (count={count})"
        )

    # The baseline doubles as the curve's "delta basis": engine semantics
    # for count >= 2 are ``final = baseline + keyframe_value``.  We bake
    # that addition in here so callers always get absolute local
    # translations, regardless of curve count.  baseline ≈ rest_t for
    # animated bones; the small per-curve drift (float32 round-trip
    # through the .ani file) is the engine's authoritative value, so
    # using baseline rather than the skeleton's rest_t is intentional.
    # np.frombuffer decodes the whole float32 vec3 block at once; the
    # count*12-byte bound was just checked, so it cannot overrun.
    baseline_arr = np.asarray(baseline, dtype=np.float32)
    deltas = np.frombuffer(
        blob, dtype="<f4", count=count * 3, offset=data_offset,
    ).reshape(count, 3)
    keyframes = deltas + baseline_arr
    # Fallback for unpopulated frames is the absolute baseline (= delta 0).
    return _fill_per_frame(
        keyframe_count, keyframes, timestamps, baseline_arr,
    )


def decode_rotation_curve(
    curve: AnimCurve, keyframe_count: int, compression: int,
    *, rest_rotation: _Quat = (0.0, 0.0, 0.0, 1.0),
) -> np.ndarray:
    """Decode a rotation curve to per-frame ``(x, y, z, w)`` quaternions.

    Quaternion components stay in D4-native order (X, Y, Z, W).  No
    coordinate-system conversion is applied here; that happens at
    export time.

    ``flCompression`` 0, 3 and 5 all use this layout — comp=3 stores
    fewer keyframes (sparser timestamps), and comp=5 is byte-identical
    to comp=3 (see ``docs/research-flcompression5.md``).
    """
    if compression not in _SUPPORTED_COMPRESSION:
        raise AnimFormatError(
            f"decode_rotation_curve: unknown flCompression={compression}"
        )

    if curve.is_empty:
        return _const_per_frame(rest_rotation, keyframe_count)

    blob = curve.raw_keys
    if len(blob) < 4:
        raise AnimFormatError(
            f"rotation curve too small: {len(blob)} bytes"
        )

    count = struct.unpack_from("<H", blob, 0)[0]
    if count == 0:
        return _const_per_frame(rest_rotation, keyframe_count)

    if count == 1:
        # Special layout: int16x4 quat sits directly at offset 2,
        # followed by a 2-byte trailer that pads the blob to 12B.
        if len(blob) < 10:
            raise AnimFormatError(
                f"static rotation curve too small: {len(blob)} bytes"
            )
        raw = struct.unpack_from("<4h", blob, 2)
        q = tuple(x / _QUAT_NORMALIZER for x in raw)
        return _const_per_frame(q, keyframe_count)

    # count >= 2: count-1 explicit timestamps for frames 1..N (frame 0
    # is implicit at keyframe 0); count int16x4 quaternions.  The data
    # section needs 2-byte alignment (int16 elements) — using _align4
    # here would overshoot by 2 whenever ``2 + ts_count`` is odd and
    # garble every quaternion by one int16 slot.
    ts_count = count - 1
    timestamps = list(blob[2:2 + ts_count])
    data_offset = _align2(2 + ts_count)
    needed = data_offset + count * 8
    if needed > len(blob):
        raise AnimFormatError(
            f"rotation curve truncated: need {needed} bytes, "
            f"have {len(blob)} (count={count})"
        )

    # Decode every int16x4 quat in one frombuffer; the count*8-byte
    # bound was just checked. astype + the scalar division produce a
    # fresh, writable float32 array so the W recovery can patch rows
    # in place.
    quats = (
        np.frombuffer(blob, dtype="<i2", count=count * 4, offset=data_offset)
        .astype(np.float32)
        .reshape(count, 4)
        / _QUAT_NORMALIZER
    )

    # Comp=3 occasionally stores the closing/wraparound keyframe with
    # W=0 instead of the real value (only ever seen on the LAST
    # keyframe, where X/Y/Z match keyframe 0).  Detect a non-unit
    # length and recover W via smallest-3:
    # W = +/- sqrt(max(0, 1 - X^2 - Y^2 - Z^2)).  The length test
    # vectorises over all rows; the sign — which copies the *previous*
    # keyframe's W to keep the curve continuous — is a sequential
    # dependency, so a tight Python loop over only the flagged rows
    # preserves that semantic without scanning every row.
    length_sq = np.einsum("ij,ij->i", quats, quats)
    bad = length_sq < 0.9025                       # |q| < 0.95
    bad[0] = False                                 # row 0 has no prior key
    if bad.any():
        xyz_sq = np.einsum("ij,ij->i", quats[:, :3], quats[:, :3])
        recovered = np.sqrt(np.maximum(0.0, 1.0 - xyz_sq))
        for i in np.nonzero(bad)[0]:
            w = recovered[i]
            if quats[i - 1, 3] < 0.0:              # match prior W's sign
                w = -w
            quats[i, 3] = w

    # Frame 0 = keyframe 0, then keyframe i (i>=1) lives at frame timestamps[i-1].
    frame_indices = [0] + list(timestamps)
    return _fill_per_frame_slerp(
        keyframe_count, quats, frame_indices, quats[0],
    )


def decode_scale_curve(
    curve: AnimCurve, keyframe_count: int, compression: int,
    *, rest_scale: _Vec3 = (1.0, 1.0, 1.0),
) -> np.ndarray:
    """Decode a scale curve to per-frame ``(x, y, z)`` scale factors.

    Every comp=0 sample we have ships an empty scale curve, so for
    comp=0 this almost always falls through to the rest pose.  Comp=3
    uses a slimmer header — just ``[u8 count][u8 fmt=4]`` followed by
    timestamps and per-keyframe ``float32 vec3`` values, with no
    12-byte baseline (the translation decoder's ``[u16 pad][float32
    vec3 baseline]`` interlude is omitted for scale).  Comp=5 reuses the
    comp=3 scale layout unchanged.
    """
    if compression not in _SUPPORTED_COMPRESSION:
        raise AnimFormatError(
            f"decode_scale_curve: unknown flCompression={compression}"
        )

    if curve.is_empty:
        return _const_per_frame(rest_scale, keyframe_count)

    blob = curve.raw_keys
    if len(blob) < 4:
        raise AnimFormatError(
            f"scale curve too small: {len(blob)} bytes"
        )

    count = blob[0]
    fmt = blob[1]
    # See ``decode_translation_curve``: ``count == 0`` short-circuits
    # before the format check so zero-padded empty curves don't raise.
    if count == 0:
        return _const_per_frame(rest_scale, keyframe_count)
    if fmt != _TRANSLATION_FMT:
        raise AnimFormatError(
            f"scale curve: unexpected format byte 0x{fmt:02X} "
            f"(expected 0x{_TRANSLATION_FMT:02X})"
        )

    if count == 1:
        # Same shape as a count=1 translation: one explicit value.
        # No samples observed in CASC, but it would be wasteful to
        # emit anything other than a constant curve here.
        if len(blob) < 16:
            raise AnimFormatError(
                f"static scale curve too small: {len(blob)} bytes"
            )
        value = struct.unpack_from("<3f", blob, 4)
        return _const_per_frame(value, keyframe_count)

    # count >= 2: u8 timestamps directly after the 2-byte header,
    # pad to 4-byte alignment, then count * float32 vec3.
    ts_offset = 2
    timestamps = list(blob[ts_offset:ts_offset + count])
    data_offset = _align4(ts_offset + count)
    needed = data_offset + count * 12
    if needed > len(blob):
        raise AnimFormatError(
            f"scale curve truncated: need {needed} bytes, "
            f"have {len(blob)} (count={count})"
        )

    # Scale keyframes are stored absolute (no baseline to add); decode
    # the whole float32 vec3 block in one frombuffer.
    keyframes = np.frombuffer(
        blob, dtype="<f4", count=count * 3, offset=data_offset,
    ).reshape(count, 3)
    # Frame 0 is explicit when timestamps[0] == 0; otherwise the
    # first frames inherit the rest scale until the first keyframe.
    return _fill_per_frame(
        keyframe_count, keyframes, timestamps, rest_scale,
    )


# ─── High-level decoder ──────────────────────────────────────────────


@dataclass
class DecodedBoneAnimation:
    """Per-frame transforms for one bone of a decoded permutation.

    All three arrays are shape ``(frame_count, dim)`` float32 even for
    static / empty channels — empty channels fill with the rest pose so
    callers can blindly index by frame.  ``dim`` is 3 for translation
    and scale, 4 for rotation.

    Quaternion component order is ``(x, y, z, w)`` — D4-native, matching
    the skeleton parser (``Bone.local_trs.q``) and the glTF output.

    ``__post_init__`` coerces any array-like input to a float32 ndarray,
    so the numpy invariant holds for every instance regardless of how it
    was built — the decoders already hand in arrays, while test fixtures
    and the per-channel rest-pose fallback may pass lists of tuples.
    """

    bone_hash: int
    translations: np.ndarray   # (frame_count, 3) float32, D4-native
    rotations: np.ndarray      # (frame_count, 4) float32, (x, y, z, w)
    scales: np.ndarray         # (frame_count, 3) float32

    def __post_init__(self) -> None:
        # asarray is a no-op when the input is already a float32 ndarray
        # (the decoder path), so this costs nothing on the hot path.
        self.translations = np.asarray(self.translations, dtype=np.float32)
        self.rotations = np.asarray(self.rotations, dtype=np.float32)
        self.scales = np.asarray(self.scales, dtype=np.float32)


@dataclass
class DecodedAnimation:
    """All bones' decoded curves for a single permutation."""

    frame_rate: float
    frame_count: int
    compression: int
    permutation_index: int
    bone_animations: list[DecodedBoneAnimation] = field(default_factory=list)
    name: str = ""
    # When True, the glTF exporter emits a translation/rotation/scale
    # channel for every bone even when the values equal the bone's rest
    # pose.  Set on the synthetic animation from build_rest_pose_animation;
    # real animations leave it False so the static-channel-skip
    # optimisation still prunes redundant channels.
    force_static_channels: bool = False


def decode_permutation(
    perm: AnimPermutationData,
    *,
    rest_pose: dict[int, tuple[_Quat, _Vec3, _Vec3]] | None = None,
) -> DecodedAnimation:
    """Decode every curve in a parsed permutation.

    ``rest_pose`` maps bone-name-hash → ``(quat, translation, scale)`` —
    pass it when you have the matching ``Skeleton`` so empty curves
    fill with the right rest-pose values rather than identity / zero.
    The mapping is what callers usually build via
    ``{b.name_hash: (b.local_trs.q, b.local_trs.wp, b.local_trs.scale)
    for b in skeleton.bones}``.

    Raises ``AnimFormatError`` for an unsupported ``flCompression`` mode
    or a long-form (>``LONG_FORM_FRAME_LIMIT``-frame, u16-timestamp)
    permutation — the u16 layout is not yet implemented.
    """
    if perm.compression not in _SUPPORTED_COMPRESSION:
        raise AnimFormatError(
            f"decode_permutation: {perm.name or 'animation'} uses "
            f"unsupported flCompression={perm.compression}"
        )

    # Long-form (u16-timestamp) animations are not yet supported.  The
    # format fork is frame-count driven, not flCompression-driven:
    # <=255 frames uses u8 timestamps (implemented), >255 uses u16.
    # Without this guard the u8-timestamp decoder silently mis-decodes
    # every >255-frame permutation — including ~1,800 comp=3 ones in
    # shipped data.  Failing cleanly here surfaces them in the export
    # failure dump.  The message deliberately omits an "flCompression="
    # substring so the dump's per-mode bucketiser treats long-form as
    # its own failure category rather than mis-attributing it to a mode.
    if perm.keyframe_count > LONG_FORM_FRAME_LIMIT:
        raise AnimFormatError(
            f"decode_permutation: {perm.name or 'animation'} uses u16 "
            f"long-form timestamps (nKeyframeCount={perm.keyframe_count}"
            f" > {LONG_FORM_FRAME_LIMIT}), not yet supported"
        )

    rest_pose = rest_pose or {}
    bones: list[DecodedBoneAnimation] = []
    h = perm.header
    fallback_count = 0
    first_fallback_msg: str | None = None

    def _try_decode(decoder, curve, fallback, label, bone_hash):
        """Run ``decoder`` for one curve; on AnimFormatError fall back.

        A single bone with an unsupported curve format would otherwise
        sink the whole animation. Per-channel fallback to rest pose
        keeps every other bone usable; the caller sees a count of
        fallbacks via the log.
        """
        nonlocal fallback_count, first_fallback_msg
        try:
            return decoder(curve, perm.keyframe_count, perm.compression,
                           **{label: fallback})
        except AnimFormatError as exc:
            fallback_count += 1
            if first_fallback_msg is None:
                first_fallback_msg = (
                    f"bone 0x{bone_hash:08x} {label[5:]} curve: {exc}"
                )
            return _const_per_frame(fallback, perm.keyframe_count)

    for i, bone_hash in enumerate(h.bone_names):
        rest = rest_pose.get(bone_hash)
        rest_q = rest[0] if rest else (0.0, 0.0, 0.0, 1.0)
        rest_t = rest[1] if rest else (0.0, 0.0, 0.0)
        rest_s = rest[2] if rest else (1.0, 1.0, 1.0)

        t_curve = (
            h.translation_curves[i]
            if i < len(h.translation_curves) else AnimCurve(0, 0)
        )
        r_curve = (
            h.rotation_curves[i]
            if i < len(h.rotation_curves) else AnimCurve(0, 0)
        )
        s_curve = (
            h.scale_curves[i]
            if i < len(h.scale_curves) else AnimCurve(0, 0)
        )

        translations = _try_decode(
            decode_translation_curve, t_curve, rest_t,
            "rest_translation", bone_hash,
        )
        rotations = _try_decode(
            decode_rotation_curve, r_curve, rest_q,
            "rest_rotation", bone_hash,
        )
        scales = _try_decode(
            decode_scale_curve, s_curve, rest_s,
            "rest_scale", bone_hash,
        )
        bones.append(DecodedBoneAnimation(
            bone_hash=bone_hash,
            translations=translations,
            rotations=rotations,
            scales=scales,
        ))

    if fallback_count:
        log.warning(
            "%s: %d curve(s) fell back to rest pose. First: %s",
            perm.name or "animation",
            fallback_count, first_fallback_msg,
        )

    return DecodedAnimation(
        frame_rate=perm.frame_rate,
        frame_count=perm.keyframe_count,
        compression=perm.compression,
        permutation_index=perm.permutation_index,
        bone_animations=bones,
        name=perm.name,
    )


def build_rest_pose_animation(
    skeleton: "Skeleton",
    *,
    name: str = "rest_pose",
    frame_rate: float = 30.0,
) -> DecodedAnimation:
    """Synthesize a 1-frame ``DecodedAnimation`` that holds every bone at rest.

    Used by the GLB exporter to give Blender users a clickable Action
    that snaps the rig back to its bind pose.  Each bone gets a single
    keyframe whose translation/rotation/scale equal ``bone.local_trs``
    — the same values the bone NODE is built from — keyed by
    ``bone.name_hash`` so the exporter's bone-hash → node lookup finds it.

    The result must be exported with the static-channel-skip
    optimisation disabled: set ``force_static_channels = True`` on the
    returned object before handing it to ``GltfExporter.export``.
    Otherwise ``GltfExporter._build_animations`` prunes every channel as
    "equal to rest" and Blender shows an empty, unusable Action.
    """
    bones: list[DecodedBoneAnimation] = []
    for bone in skeleton.bones:
        trs = bone.local_trs
        # One keyframe per channel: a (1, dim) array holding the bone's
        # rest TRS verbatim.
        bones.append(DecodedBoneAnimation(
            bone_hash=bone.name_hash,
            translations=np.asarray([trs.wp], dtype=np.float32),
            rotations=np.asarray([trs.q], dtype=np.float32),
            scales=np.asarray([trs.scale], dtype=np.float32),
        ))
    return DecodedAnimation(
        frame_rate=frame_rate,
        frame_count=1,
        compression=0,
        permutation_index=0,
        bone_animations=bones,
        name=name,
    )


# ─── Rest-pose validation ────────────────────────────────────────────


@dataclass
class BoneValidationResult:
    bone_hash: int
    in_skeleton: bool
    translation_error: float | None     # Euclidean distance, None if not validated
    rotation_error_deg: float | None    # angle between quats, None if not validated
    rotation_unit_length: float | None  # |q|, None if curve was empty


def _quat_dot(a: _Quat, b: _Quat) -> float:
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2] + a[3]*b[3]


def _quat_normalize(q: _Quat) -> _Quat:
    n = (q[0]*q[0] + q[1]*q[1] + q[2]*q[2] + q[3]*q[3]) ** 0.5
    if n < 1e-9:
        return q
    return (q[0]/n, q[1]/n, q[2]/n, q[3]/n)


def _quat_angle_deg(a: _Quat, b: _Quat) -> float:
    import math
    a = _quat_normalize(a)
    b = _quat_normalize(b)
    d = max(0.0, min(1.0, abs(_quat_dot(a, b))))
    return math.degrees(2.0 * math.acos(d))


def _vec3_dist(a: _Vec3, b: _Vec3) -> float:
    return ((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2) ** 0.5


def validate_against_rest_pose(
    decoded: DecodedAnimation,
    skeleton_rest: dict[int, tuple[_Quat, _Vec3, _Vec3]],
    *,
    frame: int = 0,
) -> tuple[list[BoneValidationResult], str]:
    """Compare decoded ``frame`` against ``skeleton_rest`` and report.

    Args:
        decoded: A ``DecodedAnimation`` produced by ``decode_permutation``.
        skeleton_rest: ``{bone_hash: (q, t, s)}`` mapping from the
            associated ``.app`` skeleton.
        frame: Which frame to compare against the rest pose.  Frame 0
            is the canonical choice for poses / idle animations.

    Returns:
        Per-bone results plus a one-paragraph human-readable summary.
        The summary highlights how many bones round-trip back to the
        rest pose — a high match count for an idle/pose animation is
        the primary "decoder is correct" signal.
    """
    import math

    results: list[BoneValidationResult] = []
    in_skel = 0
    t_match = 0
    r_match = 0
    r_unit = 0
    t_errs: list[float] = []
    r_errs: list[float] = []

    for bone in decoded.bone_animations:
        rest = skeleton_rest.get(bone.bone_hash)
        if rest is None:
            results.append(BoneValidationResult(
                bone_hash=bone.bone_hash, in_skeleton=False,
                translation_error=None,
                rotation_error_deg=None,
                rotation_unit_length=None,
            ))
            continue
        in_skel += 1

        rest_q, rest_t, _rest_s = rest
        # ``.shape[0] == 0`` rather than a truthiness test: the curves
        # are numpy arrays now, and ``bool(array)`` is ambiguous for
        # length-2+ arrays. A zero-length array means a zero-frame clip.
        if bone.translations.shape[0] == 0:
            t_err = None
        else:
            t_err = float(_vec3_dist(rest_t, bone.translations[frame]))
            t_errs.append(t_err)
            if t_err < 1e-3:
                t_match += 1

        if bone.rotations.shape[0] == 0:
            r_err = None
            r_len = None
        else:
            q = bone.rotations[frame]
            r_len = float((q[0]**2 + q[1]**2 + q[2]**2 + q[3]**2) ** 0.5)
            if abs(r_len - 1.0) < 0.01:
                r_unit += 1
            r_err = float(_quat_angle_deg(q, rest_q))
            r_errs.append(r_err)
            if r_err < 1.0:
                r_match += 1

        results.append(BoneValidationResult(
            bone_hash=bone.bone_hash, in_skeleton=True,
            translation_error=t_err,
            rotation_error_deg=r_err,
            rotation_unit_length=r_len,
        ))

    total = len(decoded.bone_animations)
    # Defensive median / max — avoid IndexError on tiny animations.
    def _median(xs: list[float]) -> float:
        if not xs:
            return float("nan")
        s = sorted(xs)
        return s[len(s) // 2]

    summary_lines = [
        f"  bones decoded:        {total}",
        f"  bones in skeleton:    {in_skel}",
        f"  T match-rest (<1e-3): {t_match} / {len(t_errs)}  "
        f"(median err {_median(t_errs):.4e}, max {max(t_errs) if t_errs else 0:.4e})",
        f"  R match-rest (<1.0deg): {r_match} / {len(r_errs)}  "
        f"(median {_median(r_errs):.3f}deg, max {max(r_errs) if r_errs else 0:.3f}deg)",
        f"  R unit-length (|q|~=1): {r_unit} / {len(r_errs)}",
    ]
    return results, "\n".join(summary_lines)
