"""Tests for the Diablo IV .ani animation parser."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import pytest

from d4extract.formats.anim_parser import (
    ANIM_PAYLOAD_ARRAY_COUNT,
    ANIM_PAYLOAD_HEADER_SIZE,
    ANIM_SLOT_NAMES,
    DT_VARIABLEARRAY_SIZE,
    SLOT_BONE_NAMES,
    SLOT_DEPTH_OF_FIELD,
    SLOT_FACING_YAW,
    SLOT_NONLINEAR_OFFSET,
    SLOT_ROOT_SCALE,
    SLOT_ROTATION_CURVES,
    SLOT_SCALE_CURVES,
    SLOT_TRANSLATION_CURVES,
    AnimCurve,
    AnimFormatError,
    AnimVarArray,
    cross_reference_bone_hashes,
    format_diagnostic,
    parse_anim,
    parse_anim_payload_header,
)


# ─── Synthetic payload builder ───────────────────────────────────────


def _var_array(data_offset: int, data_size: int) -> bytes:
    """Encode one ``DT_VARIABLEARRAY`` header (16 bytes)."""
    return struct.pack("<4I", 0, 0, data_offset, data_size)


class _PayloadBuilder:
    """Append-only buffer that returns absolute offsets for each blob.

    Mirrors the way the engine lays out the .ani payload: a top-level
    ``AnimPayloadData`` header with 13 ``DT_VARIABLEARRAY`` records
    pointing at appended blobs.  Building a payload this way keeps the
    test data easy to read — every test states up-front what shapes
    each slot should have, then asks the builder to materialize them.
    """

    def __init__(self, header_offset: int = 32) -> None:
        # Mirror the real layout: 32-byte SNO payload prelude before
        # the AnimPayloadData header.
        self._buf = bytearray(b"\x00" * header_offset)
        self._buf.extend(b"\x00" * ANIM_PAYLOAD_HEADER_SIZE)
        self.header_offset = header_offset

    @property
    def header_end(self) -> int:
        return self.header_offset + ANIM_PAYLOAD_HEADER_SIZE

    def append(self, blob: bytes) -> int:
        """Append ``blob`` and return its absolute offset."""
        offset = len(self._buf)
        self._buf.extend(blob)
        return offset

    def set_slot(self, slot: int, data_offset: int, data_size: int) -> None:
        """Overwrite the 13-array record at ``slot`` with a pointer."""
        record_off = self.header_offset + slot * DT_VARIABLEARRAY_SIZE
        self._buf[record_off:record_off + DT_VARIABLEARRAY_SIZE] = (
            _var_array(data_offset, data_size)
        )

    def add_blob(self, slot: int, blob: bytes) -> int:
        """Append ``blob`` and wire slot ``slot`` to point at it."""
        offset = self.append(blob)
        self.set_slot(slot, offset, len(blob))
        return offset

    def add_curves(self, slot: int, key_blobs: list[bytes]) -> int:
        """Append a curve list (n × 16-byte sub-headers + key blobs).

        Returns the absolute offset of the curve-list blob.
        """
        # First pass: append each key blob and remember its offset.
        key_pointers: list[tuple[int, int]] = []
        for blob in key_blobs:
            if blob:
                off = self.append(blob)
                key_pointers.append((off, len(blob)))
            else:
                key_pointers.append((0, 0))
        # Second pass: emit the curve-list array (one DT_VARIABLEARRAY per bone).
        list_blob = b"".join(_var_array(o, s) for o, s in key_pointers)
        list_offset = self.append(list_blob)
        self.set_slot(slot, list_offset, len(list_blob))
        return list_offset

    def to_bytes(self) -> bytes:
        return bytes(self._buf)


# ─── parse_anim_payload_header ───────────────────────────────────────


class TestParseHeaderBinary:
    """Direct tests of the 208-byte header decoder."""

    def test_minimal_two_bone_payload(self) -> None:
        """A minimal 2-bone, 2-frame animation parses as expected."""
        b = _PayloadBuilder()
        b.add_blob(SLOT_BONE_NAMES, struct.pack("<2I", 0xAABBCCDD, 0x12345678))
        # Per-frame root motion: 2 frames of pwvNonlinearOffset.
        b.add_blob(
            SLOT_NONLINEAR_OFFSET,
            struct.pack("<6f", 0, 0, 0, 1, 2, 3),
        )
        b.add_blob(
            SLOT_FACING_YAW,
            struct.pack("<2f", 0.0, 1.5708),
        )
        b.add_blob(
            SLOT_ROOT_SCALE,
            struct.pack("<2f", 1.0, 1.5),
        )
        # Two frames of DOF = 4 floats = 16 bytes.
        b.add_blob(
            SLOT_DEPTH_OF_FIELD,
            struct.pack("<4f", 1.4, 5.0, 2.0, 4.5),
        )
        # Per-bone curves: bone 0 gets 4 raw bytes, bone 1 gets 6.
        b.add_curves(
            SLOT_TRANSLATION_CURVES,
            [b"\xAA\xBB\xCC\xDD", b"\x01\x02\x03\x04\x05\x06"],
        )
        b.add_curves(
            SLOT_ROTATION_CURVES,
            [b"\xDE\xAD\xBE\xEF", b""],
        )
        b.add_curves(
            SLOT_SCALE_CURVES,
            [b"", b""],
        )

        h = parse_anim_payload_header(b.to_bytes(), b.header_offset)

        assert h.bone_names == [0xAABBCCDD, 0x12345678]
        assert len(h.translation_curves) == 2
        assert h.translation_curves[0].raw_keys == b"\xAA\xBB\xCC\xDD"
        assert h.translation_curves[1].raw_keys == b"\x01\x02\x03\x04\x05\x06"
        assert h.rotation_curves[0].raw_keys == b"\xDE\xAD\xBE\xEF"
        assert h.rotation_curves[1].raw_keys == b""
        assert h.rotation_curves[1].is_empty
        assert h.scale_curves[0].is_empty
        assert h.scale_curves[1].is_empty

        assert h.nonlinear_offsets == [
            (0.0, 0.0, 0.0),
            (1.0, 2.0, 3.0),
        ]
        assert h.facing_yaw == pytest.approx([0.0, 1.5708])
        assert h.root_scale == pytest.approx([1.0, 1.5])

        assert h.depth_of_field.frame_count == 2
        assert h.depth_of_field.f_stop == pytest.approx([1.4, 2.0])
        assert h.depth_of_field.focal_distance == pytest.approx([5.0, 4.5])

        # All 13 raw header records are preserved (even unused unk_* slots).
        assert len(h.raw_arrays) == ANIM_PAYLOAD_ARRAY_COUNT

    def test_empty_payload_parses_with_zero_arrays(self) -> None:
        """A header with all-zero pointers parses to empty lists."""
        b = _PayloadBuilder()  # no slots assigned

        h = parse_anim_payload_header(b.to_bytes(), b.header_offset)

        assert h.bone_names == []
        assert h.translation_curves == []
        assert h.rotation_curves == []
        assert h.scale_curves == []
        assert h.nonlinear_offsets == []
        assert h.facing_yaw == []
        assert h.root_scale == []
        assert h.depth_of_field.frame_count == 0
        assert all(arr.is_empty for arr in h.raw_arrays)

    def test_header_offset_past_eof_raises(self) -> None:
        """A header that doesn't fit in the payload errors out cleanly."""
        small = b"\x00" * 50
        with pytest.raises(AnimFormatError, match="extend"):
            parse_anim_payload_header(small, 32)

    def test_negative_offset_raises(self) -> None:
        with pytest.raises(AnimFormatError, match="Negative"):
            parse_anim_payload_header(b"\x00" * 240, -1)

    def test_sub_array_out_of_range_raises(self) -> None:
        """A header pointer past EOF raises rather than reading garbage."""
        b = _PayloadBuilder()
        # Wire bone-names slot to a bogus offset/size.
        b.set_slot(SLOT_BONE_NAMES, data_offset=99_999, data_size=8)

        with pytest.raises(AnimFormatError, match="ptBoneNames"):
            parse_anim_payload_header(b.to_bytes(), b.header_offset)

    def test_curve_keys_out_of_range_raises(self) -> None:
        """A curve sub-header pointing past EOF raises."""
        b = _PayloadBuilder()
        # Manually craft a 1-curve array pointing at an out-of-range blob.
        bogus_curve_list = _var_array(99_999, 4)
        list_offset = b.append(bogus_curve_list)
        b.set_slot(
            SLOT_TRANSLATION_CURVES, list_offset, len(bogus_curve_list),
        )

        with pytest.raises(AnimFormatError, match="ptKeysComp"):
            parse_anim_payload_header(b.to_bytes(), b.header_offset)

    def test_three_bone_curve_count_matches(self) -> None:
        """The curve list length scales with the number of bones."""
        b = _PayloadBuilder()
        b.add_blob(SLOT_BONE_NAMES, struct.pack("<3I", 1, 2, 3))
        b.add_curves(SLOT_TRANSLATION_CURVES, [b"a", b"bb", b"ccc"])
        b.add_curves(SLOT_ROTATION_CURVES, [b"AAAA", b"BBBB", b"CCCC"])
        b.add_curves(SLOT_SCALE_CURVES, [b"", b"", b""])

        h = parse_anim_payload_header(b.to_bytes(), b.header_offset)

        assert len(h.bone_names) == 3
        assert [c.keys_size for c in h.translation_curves] == [1, 2, 3]
        assert [c.keys_size for c in h.rotation_curves] == [4, 4, 4]
        assert [c.keys_size for c in h.scale_curves] == [0, 0, 0]


# ─── parse_anim (top-level) ──────────────────────────────────────────


def _write_meta_json(
    path: Path,
    *,
    permutations: list[dict],
    name: str = "TestAnim",
) -> None:
    payload = {
        "__fileName__": f"base/meta/Anim/{name}.ani",
        "__type__": "AnimationDefinition",
        "ptPermutations": permutations,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _build_payload_with_perm(
    *,
    bone_count: int,
    keyframe_count: int,
    header_offset: int = 32,
) -> bytes:
    b = _PayloadBuilder(header_offset=header_offset)
    if bone_count:
        b.add_blob(
            SLOT_BONE_NAMES,
            struct.pack(f"<{bone_count}I", *range(0xC0DE0000, 0xC0DE0000 + bone_count)),
        )
        b.add_curves(
            SLOT_TRANSLATION_CURVES,
            [b"\x01" * 4 for _ in range(bone_count)],
        )
        b.add_curves(
            SLOT_ROTATION_CURVES,
            [b"\x02" * 4 for _ in range(bone_count)],
        )
        b.add_curves(
            SLOT_SCALE_CURVES,
            [b"" for _ in range(bone_count)],
        )
    if keyframe_count:
        b.add_blob(
            SLOT_FACING_YAW,
            struct.pack(f"<{keyframe_count}f", *([0.0] * keyframe_count)),
        )
    return b.to_bytes()


class TestParseAnimEntryPoint:
    def test_basic_two_bone_two_frame(self, tmp_path: Path) -> None:
        meta = tmp_path / "Test.ani.json"
        payload = tmp_path / "Test.ani"

        payload.write_bytes(_build_payload_with_perm(
            bone_count=2, keyframe_count=2,
        ))
        _write_meta_json(meta, permutations=[{
            "flFrameRate": 30,
            "flCompression": 3,
            "nBoneCount": 2,
            "nKeyframeCount": 2,
            "nCycleCount": 1,
            "ptPayloadData": {
                "value": {
                    "dataOffset": 32,
                    "dataSize": ANIM_PAYLOAD_HEADER_SIZE,
                },
            },
            "pnAnimToStructure": "0",
        }])

        perm = parse_anim(meta, payload)

        assert perm.bone_count == 2
        assert perm.keyframe_count == 2
        assert perm.compression == 3
        assert perm.frame_rate == 30.0
        assert perm.cycle_count == 1
        assert perm.payload_offset == 32
        assert perm.permutation_index == 0
        assert perm.name == "Test"
        assert perm.payload_size == len(payload.read_bytes())

        assert len(perm.header.bone_names) == 2
        assert len(perm.header.translation_curves) == 2
        assert len(perm.header.rotation_curves) == 2
        assert len(perm.header.scale_curves) == 2
        # pnAnimToStructure as the string "0" is "external" — surface as None.
        assert perm.anim_to_structure is None

    def test_choosing_a_later_permutation(self, tmp_path: Path) -> None:
        """``permutation_index`` selects the right header offset."""
        meta = tmp_path / "Multi.ani.json"
        payload = tmp_path / "Multi.ani"

        # Build a payload with two non-overlapping headers.
        b = _PayloadBuilder(header_offset=32)
        b.add_blob(SLOT_BONE_NAMES, struct.pack("<I", 0x11111111))
        b.add_curves(SLOT_TRANSLATION_CURVES, [b"\xAA"])
        b.add_curves(SLOT_ROTATION_CURVES, [b"\xBB"])
        b.add_curves(SLOT_SCALE_CURVES, [b""])
        first_payload = b.to_bytes()
        second_offset = len(first_payload)

        b2 = _PayloadBuilder(header_offset=second_offset)
        # Note: _PayloadBuilder pre-pads up to header_offset, so the
        # bytes before second_offset are zeros.  Splice the first
        # payload over those zeros so both headers coexist.
        combined = bytearray(b2.to_bytes())
        combined[:len(first_payload)] = first_payload
        # Wire the second header.
        write_at = lambda off, blob: combined.__setitem__(
            slice(off, off + len(blob)), blob,
        )
        # Append a 2-bone names blob behind the second header.
        names_off = len(combined)
        combined.extend(struct.pack("<2I", 0x22222222, 0x33333333))
        write_at(
            second_offset + SLOT_BONE_NAMES * DT_VARIABLEARRAY_SIZE,
            _var_array(names_off, 8),
        )
        # Empty curves to keep parser happy.
        for slot in (
            SLOT_TRANSLATION_CURVES, SLOT_ROTATION_CURVES, SLOT_SCALE_CURVES,
        ):
            list_off = len(combined)
            list_blob = _var_array(0, 0) + _var_array(0, 0)
            combined.extend(list_blob)
            write_at(
                second_offset + slot * DT_VARIABLEARRAY_SIZE,
                _var_array(list_off, len(list_blob)),
            )
        payload.write_bytes(bytes(combined))

        _write_meta_json(meta, permutations=[
            {
                "flFrameRate": 30, "flCompression": 0,
                "nBoneCount": 1, "nKeyframeCount": 1, "nCycleCount": 1,
                "ptPayloadData": {"value": {
                    "dataOffset": 32,
                    "dataSize": ANIM_PAYLOAD_HEADER_SIZE,
                }},
            },
            {
                "flFrameRate": 30, "flCompression": 3,
                "nBoneCount": 2, "nKeyframeCount": 1, "nCycleCount": 1,
                "ptPayloadData": {"value": {
                    "dataOffset": second_offset,
                    "dataSize": ANIM_PAYLOAD_HEADER_SIZE,
                }},
            },
        ])

        p0 = parse_anim(meta, payload, permutation_index=0)
        p1 = parse_anim(meta, payload, permutation_index=1)

        assert p0.permutation_index == 0
        assert p0.bone_count == 1
        assert p0.payload_offset == 32
        assert p0.header.bone_names == [0x11111111]

        assert p1.permutation_index == 1
        assert p1.bone_count == 2
        assert p1.payload_offset == second_offset
        assert p1.header.bone_names == [0x22222222, 0x33333333]

    def test_out_of_range_permutation_raises(self, tmp_path: Path) -> None:
        meta = tmp_path / "single.ani.json"
        payload = tmp_path / "single.ani"
        payload.write_bytes(_build_payload_with_perm(bone_count=0, keyframe_count=0))
        _write_meta_json(meta, permutations=[{
            "flFrameRate": 30, "flCompression": 0,
            "nBoneCount": 0, "nKeyframeCount": 0, "nCycleCount": 1,
            "ptPayloadData": {"value": {
                "dataOffset": 32,
                "dataSize": ANIM_PAYLOAD_HEADER_SIZE,
            }},
        }])

        with pytest.raises(AnimFormatError, match="permutation_index 1"):
            parse_anim(meta, payload, permutation_index=1)

    def test_missing_payload_pointer_raises(self, tmp_path: Path) -> None:
        meta = tmp_path / "broken.ani.json"
        payload = tmp_path / "broken.ani"
        payload.write_bytes(b"\x00" * 256)
        _write_meta_json(meta, permutations=[{
            "flFrameRate": 30, "flCompression": 0,
            "nBoneCount": 0, "nKeyframeCount": 0, "nCycleCount": 1,
            # No ptPayloadData.
        }])

        with pytest.raises(AnimFormatError, match="ptPayloadData"):
            parse_anim(meta, payload)

    def test_corrupt_meta_json_raises(self, tmp_path: Path) -> None:
        meta = tmp_path / "bad.ani.json"
        payload = tmp_path / "bad.ani"
        meta.write_text("{not valid json", encoding="utf-8")
        payload.write_bytes(b"\x00" * 256)

        with pytest.raises(AnimFormatError, match="meta JSON"):
            parse_anim(meta, payload)


# ─── Cross-reference helper ──────────────────────────────────────────


class TestCrossReference:
    def test_full_match(self) -> None:
        matched, unmatched = cross_reference_bone_hashes(
            [1, 2, 3], [3, 2, 1, 4],
        )
        assert matched == [1, 2, 3]
        assert unmatched == []

    def test_partial_match_preserves_order(self) -> None:
        matched, unmatched = cross_reference_bone_hashes(
            [1, 99, 3], [1, 2, 3],
        )
        assert matched == [1, 3]
        assert unmatched == [99]

    def test_empty_skeleton_returns_all_unmatched(self) -> None:
        """No skeleton supplied → caller can detect 'no overlap'."""
        matched, unmatched = cross_reference_bone_hashes([1, 2, 3], [])
        assert matched == []
        assert unmatched == [1, 2, 3]


# ─── Diagnostic formatter ────────────────────────────────────────────


class TestFormatDiagnostic:
    def test_renders_all_thirteen_slots(self, tmp_path: Path) -> None:
        meta = tmp_path / "Diag.ani.json"
        payload = tmp_path / "Diag.ani"
        payload.write_bytes(_build_payload_with_perm(
            bone_count=2, keyframe_count=2,
        ))
        _write_meta_json(meta, permutations=[{
            "flFrameRate": 30, "flCompression": 3,
            "nBoneCount": 2, "nKeyframeCount": 2, "nCycleCount": 1,
            "ptPayloadData": {"value": {
                "dataOffset": 32, "dataSize": ANIM_PAYLOAD_HEADER_SIZE,
            }},
        }])
        perm = parse_anim(meta, payload)

        out = format_diagnostic(perm, hex_limit=8)

        assert "Animation: Diag" in out
        assert "Permutation 0: 2 bones, 2 frames, comp=3" in out
        for slot_name in ANIM_SLOT_NAMES:
            assert slot_name in out
        assert "Translation curves (2)" in out
        assert "Rotation curves (2)" in out
        assert "Scale curves (2)" in out
        assert "Bone hashes (2)" in out

    def test_max_curves_truncates(self, tmp_path: Path) -> None:
        meta = tmp_path / "Big.ani.json"
        payload = tmp_path / "Big.ani"
        payload.write_bytes(_build_payload_with_perm(
            bone_count=10, keyframe_count=0,
        ))
        _write_meta_json(meta, permutations=[{
            "flFrameRate": 30, "flCompression": 0,
            "nBoneCount": 10, "nKeyframeCount": 0, "nCycleCount": 1,
            "ptPayloadData": {"value": {
                "dataOffset": 32, "dataSize": ANIM_PAYLOAD_HEADER_SIZE,
            }},
        }])
        perm = parse_anim(meta, payload)

        out = format_diagnostic(perm, hex_limit=4, max_curves=3)

        # Each section should mention 7 omitted bones.
        assert out.count("(7 more bones omitted)") == 3

    def test_hex_limit_truncates_long_blobs(self) -> None:
        from d4extract.formats.anim_parser import _format_hex

        blob = bytes(range(20))
        rendered = _format_hex(blob, limit=4)

        assert rendered.startswith("00 01 02 03")
        assert "+16 bytes" in rendered

    def test_hex_limit_zero_renders_full_blob(self) -> None:
        from d4extract.formats.anim_parser import _format_hex

        blob = b"\xAA\xBB\xCC"
        assert _format_hex(blob, limit=0) == "AA BB CC"


# ─── AnimVarArray / AnimCurve sanity ─────────────────────────────────


class TestSimpleDataclasses:
    def test_var_array_empty_flag(self) -> None:
        assert AnimVarArray(0, 0).is_empty
        assert not AnimVarArray(100, 8).is_empty

    def test_curve_empty_flag(self) -> None:
        assert AnimCurve(0, 0).is_empty
        assert not AnimCurve(100, 4, b"\x00\x00\x00\x00").is_empty


# ─── flCompression=0 decoder ─────────────────────────────────────────


from d4extract.formats.anim_parser import (  # noqa: E402
    AnimPayloadHeader,
    AnimPermutationData,
    DecodedAnimation,
    decode_permutation,
    decode_rotation_curve,
    decode_scale_curve,
    decode_translation_curve,
    validate_against_rest_pose,
)


def _build_translation_blob(
    *, count: int, baseline: tuple[float, float, float],
    timestamps: list[int] | None = None,
    keyframes: list[tuple[float, float, float]] | None = None,
) -> bytes:
    """Build a comp=0 translation curve blob exactly as the engine does.

    Layout: ``[u8 count][u8 0x04][u16 0][float32x3 baseline]
                [u8 ts[count] (if count>=2)][pad to 4][float32x3[count]]``
    """
    out = bytearray()
    out.extend(struct.pack("<BBH", count, 0x04, 0))
    out.extend(struct.pack("<3f", *baseline))
    if count >= 2:
        assert timestamps is not None and keyframes is not None
        assert len(timestamps) == count and len(keyframes) == count
        out.extend(bytes(timestamps))
        # Pad to 4-byte alignment.
        while len(out) % 4 != 0:
            out.append(0)
        for v in keyframes:
            out.extend(struct.pack("<3f", *v))
    return bytes(out)


def _build_rotation_blob(
    *, count: int,
    quats: list[tuple[float, float, float, float]],
    timestamps_after_first: list[int] | None = None,
) -> bytes:
    """Build a comp=0 rotation curve blob.

    Static (count=1):  ``[u16 1][int16x4 quat][u16 0]`` = 12 bytes.
    Per-frame (count>=2): ``[u16 count][u8 ts[count-1]][pad to 2]
                              [int16x4 quat[count]]``.

    Quaternion data is int16x4 (8 bytes), so the engine pads to a
    2-byte boundary, *not* a 4-byte boundary.  Using 4-byte alignment
    here would shift quat components by an int16 slot whenever
    ``2 + ts_count`` is odd.
    """
    assert len(quats) == count
    out = bytearray()
    out.extend(struct.pack("<H", count))
    if count == 1:
        out.extend(struct.pack(
            "<4h", *(int(round(x * 32767)) for x in quats[0]),
        ))
        out.extend(b"\x00\x00")  # 2-byte trailer
        return bytes(out)
    # count >= 2: count-1 timestamps for frames 1..N (frame 0 implicit)
    assert (
        timestamps_after_first is not None
        and len(timestamps_after_first) == count - 1
    )
    out.extend(bytes(timestamps_after_first))
    if len(out) % 2 != 0:
        out.append(0)
    for q in quats:
        out.extend(struct.pack(
            "<4h", *(int(round(x * 32767)) for x in q),
        ))
    return bytes(out)


class TestDecodeTranslation:
    def test_static_count1_returns_baseline_for_every_frame(self) -> None:
        blob = _build_translation_blob(
            count=1, baseline=(1.5, -2.25, 0.75),
        )
        curve = AnimCurve(0, len(blob), blob)

        out = decode_translation_curve(curve, keyframe_count=4, compression=0)

        np.testing.assert_allclose(out, [(1.5, -2.25, 0.75)] * 4)

    def test_per_frame_full_dense(self) -> None:
        keyframes = [(0.1 * i, 0.2 * i, 0.3 * i) for i in range(3)]
        blob = _build_translation_blob(
            count=3, baseline=(0.0, 0.0, 0.0),
            timestamps=[0, 1, 2], keyframes=keyframes,
        )
        curve = AnimCurve(0, len(blob), blob)

        out = decode_translation_curve(curve, keyframe_count=3, compression=0)

        for actual, expected in zip(out, keyframes):
            for a, b in zip(actual, expected):
                assert abs(a - b) < 1e-6

    def test_sparse_keyframes_interpolated(self) -> None:
        """Frames between keyframes are linearly interpolated, then the
        baseline is added so the output is absolute (baseline+delta)."""
        # Stored keyframes are deltas; baseline=9 in every axis means the
        # per-frame output is shifted by 9 relative to the raw values.
        keyframes = [(1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.0)]
        blob = _build_translation_blob(
            count=3, baseline=(9.0, 9.0, 9.0),
            timestamps=[0, 2, 4], keyframes=keyframes,
        )
        curve = AnimCurve(0, len(blob), blob)

        out = decode_translation_curve(curve, keyframe_count=5, compression=0)

        # All five frames carry the +9 baseline offset; mid-frames lerp.
        assert out[0] == pytest.approx((10.0, 9.0, 9.0))
        assert out[1] == pytest.approx((10.5, 9.0, 9.0))  # lerp midpoint
        assert out[2] == pytest.approx((11.0, 9.0, 9.0))
        assert out[3] == pytest.approx((11.5, 9.0, 9.0))  # lerp midpoint
        assert out[4] == pytest.approx((12.0, 9.0, 9.0))

    def test_zero_baseline_preserves_keyframe_values(self) -> None:
        """When baseline is zero the output is the raw delta stream
        unchanged — the common case in tests that don't care about the
        baseline+delta semantics."""
        keyframes = [(1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.0)]
        blob = _build_translation_blob(
            count=3, baseline=(0.0, 0.0, 0.0),
            timestamps=[0, 2, 4], keyframes=keyframes,
        )
        curve = AnimCurve(0, len(blob), blob)

        out = decode_translation_curve(curve, keyframe_count=5, compression=0)
        assert out[0] == pytest.approx((1.0, 0.0, 0.0))
        assert out[2] == pytest.approx((2.0, 0.0, 0.0))
        assert out[4] == pytest.approx((3.0, 0.0, 0.0))

    def test_empty_curve_uses_rest(self) -> None:
        out = decode_translation_curve(
            AnimCurve(0, 0), keyframe_count=3, compression=0,
            rest_translation=(7.0, 8.0, 9.0),
        )
        np.testing.assert_allclose(out, [(7.0, 8.0, 9.0)] * 3)

    def test_unknown_format_raises(self) -> None:
        # Format byte 0x05 is invalid for comp=0.
        bogus = struct.pack("<BBH", 1, 0x05, 0) + struct.pack("<3f", 0, 0, 0)
        curve = AnimCurve(0, len(bogus), bogus)
        with pytest.raises(AnimFormatError, match="format byte"):
            decode_translation_curve(curve, keyframe_count=1, compression=0)

    def test_zero_count_zero_format_uses_rest(self) -> None:
        """count=0 short-circuits before the format check.

        Empty curves observed in CASC are routinely zero-padded — the
        whole 16-byte blob is 0x00 — which would trip the strict
        ``fmt == 0x04`` check if we didn't return early on count=0.
        Regression for: half of npc_crow's animations failing with
        ``unexpected format byte 0x00``.
        """
        zero_blob = b"\x00" * 16
        curve = AnimCurve(0, len(zero_blob), zero_blob)
        out = decode_translation_curve(
            curve, keyframe_count=4, compression=0,
            rest_translation=(1.5, 2.5, 3.5),
        )
        np.testing.assert_allclose(out, [(1.5, 2.5, 3.5)] * 4)

    def test_truncated_per_frame_data_raises(self) -> None:
        # Build a blob that promises count=3 but cuts off the data.
        good = _build_translation_blob(
            count=3, baseline=(0, 0, 0),
            timestamps=[0, 1, 2],
            keyframes=[(1, 1, 1), (2, 2, 2), (3, 3, 3)],
        )
        truncated = good[:-4]  # drop the last 4 bytes
        curve = AnimCurve(0, len(truncated), truncated)
        with pytest.raises(AnimFormatError, match="truncated"):
            decode_translation_curve(curve, keyframe_count=3, compression=0)

    def test_compression3_uses_same_layout(self) -> None:
        """Phase 4 confirmed comp=3 reads with the same byte layout as comp=0."""
        blob = _build_translation_blob(
            count=1, baseline=(0.5, -0.25, 1.5),
        )
        curve = AnimCurve(0, len(blob), blob)

        out = decode_translation_curve(curve, keyframe_count=2, compression=3)

        np.testing.assert_allclose(out, [(0.5, -0.25, 1.5)] * 2)

    def test_compression5_matches_compression3(self) -> None:
        """comp=5 is byte-identical to comp=3 (docs/research-flcompression5.md).

        The same blob decoded under comp=5 and comp=3 must be identical.
        """
        blob = _build_translation_blob(
            count=3, baseline=(9.0, 9.0, 9.0),
            timestamps=[0, 2, 4],
            keyframes=[(1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (3.0, 0.0, 0.0)],
        )
        curve = AnimCurve(0, len(blob), blob)

        out5 = decode_translation_curve(curve, keyframe_count=5, compression=5)
        out3 = decode_translation_curve(curve, keyframe_count=5, compression=3)

        np.testing.assert_array_equal(out5, out3)

    def test_unknown_compression_raises(self) -> None:
        """Anything other than 0 / 3 / 5 still surfaces an error."""
        curve = AnimCurve(0, 0)
        with pytest.raises(AnimFormatError, match="unknown flCompression"):
            decode_translation_curve(curve, keyframe_count=1, compression=99)


class TestDecodeRotation:
    def test_static_count1_identity(self) -> None:
        blob = _build_rotation_blob(count=1, quats=[(0.0, 0.0, 0.0, 1.0)])
        curve = AnimCurve(0, len(blob), blob)

        out = decode_rotation_curve(curve, keyframe_count=2, compression=0)

        assert len(out) == 2
        for q in out:
            assert q == pytest.approx((0.0, 0.0, 0.0, 1.0), abs=1e-4)

    def test_static_count1_arbitrary_quat(self) -> None:
        # 90deg about Z = (0, 0, sin(45), cos(45))
        import math
        s = math.sin(math.pi / 4)
        c = math.cos(math.pi / 4)
        blob = _build_rotation_blob(count=1, quats=[(0.0, 0.0, s, c)])
        curve = AnimCurve(0, len(blob), blob)

        out = decode_rotation_curve(curve, keyframe_count=1, compression=0)

        assert out[0] == pytest.approx((0.0, 0.0, s, c), abs=1e-4)

    def test_per_frame_dense(self) -> None:
        # 3 keyframes for a 3-frame animation; frame 0 implicit (kf 0),
        # explicit timestamps [1, 2] for frames 1 and 2.
        quats = [(0.0, 0.0, 0.0, 1.0),
                 (0.1, 0.2, 0.3, 0.927)  ,
                 (0.0, 1.0, 0.0, 0.0)]
        blob = _build_rotation_blob(
            count=3, quats=quats, timestamps_after_first=[1, 2],
        )
        curve = AnimCurve(0, len(blob), blob)

        out = decode_rotation_curve(curve, keyframe_count=3, compression=0)

        for actual, expected in zip(out, quats):
            for a, b in zip(actual, expected):
                assert abs(a - b) < 1e-3

    def test_sparse_with_implicit_frame_zero(self) -> None:
        """Frame 0 is keyframe 0 (implicit); frames between explicit
        timestamps are SLERP-interpolated."""
        quats = [(1.0, 0.0, 0.0, 0.0),  # frame 0 implicit
                 (0.0, 1.0, 0.0, 0.0),  # frame 2
                 (0.0, 0.0, 1.0, 0.0)]  # frame 4
        blob = _build_rotation_blob(
            count=3, quats=quats, timestamps_after_first=[2, 4],
        )
        curve = AnimCurve(0, len(blob), blob)

        out = decode_rotation_curve(curve, keyframe_count=5, compression=0)

        assert out[0] == pytest.approx(quats[0], abs=1e-4)
        # Frame 1: SLERP midpoint between quats[0] and quats[1].
        # Both are unit quats 90° apart, so the midpoint is normalized.
        mid01 = out[1]
        assert abs(mid01[0]**2 + mid01[1]**2 + mid01[2]**2 + mid01[3]**2 - 1.0) < 1e-4
        assert out[2] == pytest.approx(quats[1], abs=1e-4)
        # Frame 3: SLERP midpoint between quats[1] and quats[2].
        mid12 = out[3]
        assert abs(mid12[0]**2 + mid12[1]**2 + mid12[2]**2 + mid12[3]**2 - 1.0) < 1e-4
        assert out[4] == pytest.approx(quats[2], abs=1e-4)

    def test_empty_curve_uses_rest(self) -> None:
        out = decode_rotation_curve(
            AnimCurve(0, 0), keyframe_count=2, compression=0,
            rest_rotation=(0.5, 0.5, 0.5, 0.5),
        )
        np.testing.assert_allclose(out, [(0.5, 0.5, 0.5, 0.5)] * 2)

    def test_odd_timestamp_count_uses_2_byte_alignment(self) -> None:
        """Regression for the alignment bug.

        With ``count=4`` we have ``ts_count=3``.  ``2 + 3 = 5`` —
        rounded up to 2-byte alignment that's offset 6, but rounded up
        to 4-byte alignment it would be offset 8.  An align-4 decoder
        skips two extra bytes and reads every quaternion shifted by one
        int16 slot (identity reads as ``(0, 0, 1, 0)`` instead of
        ``(0, 0, 0, 1)``).
        """
        # All four keyframes are identity; if alignment is off, the
        # decoded W will land on the trailing pad of the next quat.
        identity = (0.0, 0.0, 0.0, 1.0)
        # Use a distinct second quat so a slot-shift would be visible
        # (and not just identity-vs-identity by coincidence).
        second = (0.1, 0.2, 0.3, 0.927)
        quats = [identity, second, identity, second]
        blob = _build_rotation_blob(
            count=4, quats=quats,
            timestamps_after_first=[1, 2, 3],   # ts_count = 3 (odd)
        )

        out = decode_rotation_curve(curve=AnimCurve(0, len(blob), blob),
                                    keyframe_count=4, compression=0)

        for actual, expected in zip(out, quats):
            for a, b in zip(actual, expected):
                assert abs(a - b) < 1e-3

    def test_truncated_per_frame_raises(self) -> None:
        good = _build_rotation_blob(
            count=3,
            quats=[(0, 0, 0, 1), (0, 1, 0, 0), (1, 0, 0, 0)],
            timestamps_after_first=[1, 2],
        )
        truncated = good[:-2]
        curve = AnimCurve(0, len(truncated), truncated)
        with pytest.raises(AnimFormatError, match="truncated"):
            decode_rotation_curve(curve, keyframe_count=3, compression=0)

    def test_compression3_uses_same_layout(self) -> None:
        """Phase 4: comp=3 rotations parse with the same layout as comp=0."""
        blob = _build_rotation_blob(count=1, quats=[(0.0, 0.0, 0.0, 1.0)])
        curve = AnimCurve(0, len(blob), blob)

        out = decode_rotation_curve(curve, keyframe_count=3, compression=3)

        assert len(out) == 3
        for q in out:
            assert q == pytest.approx((0.0, 0.0, 0.0, 1.0), abs=1e-4)

    def test_compression5_matches_compression3(self) -> None:
        """comp=5 rotation curves decode identically to comp=3."""
        quats = [(1.0, 0.0, 0.0, 0.0),
                 (0.0, 1.0, 0.0, 0.0),
                 (0.0, 0.0, 1.0, 0.0)]
        blob = _build_rotation_blob(
            count=3, quats=quats, timestamps_after_first=[2, 4],
        )
        curve = AnimCurve(0, len(blob), blob)

        out5 = decode_rotation_curve(curve, keyframe_count=5, compression=5)
        out3 = decode_rotation_curve(curve, keyframe_count=5, compression=3)

        np.testing.assert_array_equal(out5, out3)

    def test_unknown_compression_raises(self) -> None:
        curve = AnimCurve(0, 0)
        with pytest.raises(AnimFormatError, match="unknown flCompression"):
            decode_rotation_curve(curve, keyframe_count=1, compression=99)


class TestDecodeScale:
    def test_empty_returns_rest(self) -> None:
        out = decode_scale_curve(
            AnimCurve(0, 0), keyframe_count=3, compression=0,
            rest_scale=(2.0, 2.0, 2.0),
        )
        np.testing.assert_allclose(out, [(2.0, 2.0, 2.0)] * 3)

    def test_comp3_per_frame_decodes(self) -> None:
        """Comp=3 scale uses a slim 2-byte header (no 12-byte baseline).

        Layout: ``[u8 count][u8 0x04][u8 ts[count]][pad to 4]
                  [float32 vec3[count]]``.
        """
        count = 2
        timestamps = [0, 1]
        keyframes = [(0.5, 0.5, 0.5), (1.5, 1.5, 1.5)]
        blob = bytearray()
        blob.extend(struct.pack("<BB", count, 0x04))
        blob.extend(bytes(timestamps))
        while len(blob) % 4 != 0:
            blob.append(0)
        for v in keyframes:
            blob.extend(struct.pack("<3f", *v))
        curve = AnimCurve(0, len(blob), bytes(blob))

        out = decode_scale_curve(curve, keyframe_count=2, compression=3)

        assert len(out) == 2
        assert out[0] == pytest.approx(keyframes[0])
        assert out[1] == pytest.approx(keyframes[1])

    def test_comp3_constant_count1(self) -> None:
        """count=1 uses the same 16-byte layout as count=1 translation."""
        blob = struct.pack("<BBH", 1, 0x04, 0) + struct.pack(
            "<3f", 2.0, 2.0, 2.0,
        )
        curve = AnimCurve(0, len(blob), blob)

        out = decode_scale_curve(curve, keyframe_count=3, compression=3)

        np.testing.assert_allclose(out, [(2.0, 2.0, 2.0)] * 3)

    def test_comp3_truncated_raises(self) -> None:
        good = bytearray()
        good.extend(struct.pack("<BB", 2, 0x04))
        good.extend(bytes([0, 1]))
        good.extend(struct.pack("<3f", 1, 1, 1))
        good.extend(struct.pack("<3f", 2, 2, 2))
        truncated = bytes(good[:-4])
        curve = AnimCurve(0, len(truncated), truncated)
        with pytest.raises(AnimFormatError, match="truncated"):
            decode_scale_curve(curve, keyframe_count=2, compression=3)

    def test_compression5_matches_compression3(self) -> None:
        """comp=5 scale curves reuse the comp=3 slim-header layout."""
        count = 2
        blob = bytearray()
        blob.extend(struct.pack("<BB", count, 0x04))
        blob.extend(bytes([0, 1]))
        while len(blob) % 4 != 0:
            blob.append(0)
        for v in ((0.5, 0.5, 0.5), (1.5, 1.5, 1.5)):
            blob.extend(struct.pack("<3f", *v))
        curve = AnimCurve(0, len(blob), bytes(blob))

        out5 = decode_scale_curve(curve, keyframe_count=2, compression=5)
        out3 = decode_scale_curve(curve, keyframe_count=2, compression=3)

        np.testing.assert_array_equal(out5, out3)

    def test_unknown_compression_raises(self) -> None:
        with pytest.raises(AnimFormatError, match="unknown flCompression"):
            decode_scale_curve(AnimCurve(0, 0), keyframe_count=1, compression=99)

    def test_zero_count_zero_format_uses_rest(self) -> None:
        """Same short-circuit as the translation decoder.

        Zero-padded scale curves with count=0 / fmt=0x00 must not
        trip the strict format check — the curve has no payload to
        validate.
        """
        zero_blob = b"\x00" * 16
        curve = AnimCurve(0, len(zero_blob), zero_blob)
        out = decode_scale_curve(
            curve, keyframe_count=4, compression=3,
            rest_scale=(2.0, 2.0, 2.0),
        )
        np.testing.assert_allclose(out, [(2.0, 2.0, 2.0)] * 4)


class TestDecodePermutation:
    def _build_perm(
        self,
        bone_count: int = 2,
        frame_count: int = 3,
        compression: int = 0,
    ) -> AnimPermutationData:
        """Synthesize a tiny ``AnimPermutationData`` directly (no payload bytes).

        Faster than building a real binary payload — we already have
        binary-level coverage in TestParseHeaderBinary.
        """
        # Two bones: bone A is static (count=1 across the board),
        # bone B has a per-frame translation curve.
        bone_hashes = [0xAAAA0001, 0xBBBB0002][:bone_count]
        t_curves = []
        r_curves = []
        s_curves = []
        for i in range(bone_count):
            if i == 0:
                blob_t = _build_translation_blob(
                    count=1, baseline=(0.0, 0.0, 0.0),
                )
                blob_r = _build_rotation_blob(
                    count=1, quats=[(0.0, 0.0, 0.0, 1.0)],
                )
            else:
                blob_t = _build_translation_blob(
                    count=frame_count,
                    baseline=(0.0, 0.0, 0.0),
                    timestamps=list(range(frame_count)),
                    keyframes=[
                        (float(j), 2.0 * j, 3.0 * j) for j in range(frame_count)
                    ],
                )
                blob_r = _build_rotation_blob(
                    count=1, quats=[(0.0, 0.0, 0.0, 1.0)],
                )
            t_curves.append(AnimCurve(0, len(blob_t), blob_t))
            r_curves.append(AnimCurve(0, len(blob_r), blob_r))
            s_curves.append(AnimCurve(0, 0))

        header = AnimPayloadHeader(
            bone_names=bone_hashes,
            translation_curves=t_curves,
            rotation_curves=r_curves,
            scale_curves=s_curves,
        )
        return AnimPermutationData(
            frame_rate=30.0,
            compression=compression,
            bone_count=bone_count,
            keyframe_count=frame_count,
            cycle_count=1,
            payload_offset=0,
            permutation_index=0,
            anim_to_structure=None,
            header=header,
            name="Synth",
        )

    def test_decode_returns_per_bone_per_frame(self) -> None:
        perm = self._build_perm(bone_count=2, frame_count=3)

        decoded = decode_permutation(perm)

        assert isinstance(decoded, DecodedAnimation)
        assert decoded.frame_count == 3
        assert decoded.frame_rate == 30.0
        assert len(decoded.bone_animations) == 2
        for bone in decoded.bone_animations:
            assert len(bone.translations) == 3
            assert len(bone.rotations) == 3
            assert len(bone.scales) == 3

    def test_decode_uses_rest_pose_for_empty_curves(self) -> None:
        perm = self._build_perm(bone_count=2, frame_count=2)
        rest = {0xAAAA0001: ((0, 0, 0, 1), (0, 0, 0), (1.5, 1.5, 1.5))}

        decoded = decode_permutation(perm, rest_pose=rest)

        # bone 0's scale curve is empty → fills with rest scale (1.5, 1.5, 1.5).
        bone0 = decoded.bone_animations[0]
        for s in bone0.scales:
            assert s == pytest.approx((1.5, 1.5, 1.5))
        # bone 1 has no rest pose entry → defaults to (1, 1, 1).
        bone1 = decoded.bone_animations[1]
        for s in bone1.scales:
            assert s == pytest.approx((1.0, 1.0, 1.0))

    def test_compression3_decodes(self) -> None:
        """Phase 4: comp=3 flows through the same decoder as comp=0."""
        perm = self._build_perm(compression=3)

        decoded = decode_permutation(perm)

        assert decoded.compression == 3
        assert decoded.frame_count == 3
        assert len(decoded.bone_animations) == 2

    def test_compression5_decodes(self) -> None:
        """comp=5 flows through the comp=3 decode path unchanged.

        Format established byte-identical to comp=3 — see
        docs/research-flcompression5.md.
        """
        perm5 = self._build_perm(compression=5)

        decoded = decode_permutation(perm5)

        assert decoded.compression == 5
        assert decoded.frame_count == 3
        assert len(decoded.bone_animations) == 2

        # The same synthetic bytes under comp=3 must decode identically.
        decoded3 = decode_permutation(self._build_perm(compression=3))
        for b5, b3 in zip(decoded.bone_animations, decoded3.bone_animations):
            np.testing.assert_array_equal(b5.translations, b3.translations)
            np.testing.assert_array_equal(b5.rotations, b3.rotations)
            np.testing.assert_array_equal(b5.scales, b3.scales)

    def test_uncharted_compression_still_rejected(self) -> None:
        """Modes outside (0,1,2,3,4,5,6) must still be rejected.

        Widening the guard for the proven-equivalent modes 1/2/4/6 must
        not let arbitrary modes through — 7+ are unobserved in d4data
        and unsupported.
        """
        for mode in (7, 8, 99):
            perm = self._build_perm(compression=mode)
            with pytest.raises(
                AnimFormatError, match=rf"flCompression={mode}",
            ):
                decode_permutation(perm)

    def test_unknown_compression_raises(self) -> None:
        perm = self._build_perm(compression=42)
        with pytest.raises(AnimFormatError, match="unsupported flCompression"):
            decode_permutation(perm)

    def test_long_form_raises_cleanly(self) -> None:
        """nKeyframeCount > 255 must raise AnimFormatError — the u16
        long-form timestamp layout is not yet implemented.

        The message must NOT contain an 'flCompression=' substring: the
        export failure dump bucketises by that, and long-form is a
        distinct failure category from an unsupported compression mode.
        """
        # _build_perm's synthetic curves cap at u8 frame counts, so build
        # a short-form perm and override keyframe_count past the limit —
        # the guard reads only perm.keyframe_count.
        perm = self._build_perm(compression=3)
        perm.keyframe_count = 271
        with pytest.raises(AnimFormatError, match=r"long-form") as excinfo:
            decode_permutation(perm)
        assert "flCompression=" not in str(excinfo.value)

    def test_short_form_boundary_not_rejected(self) -> None:
        """nKeyframeCount == 255 is the short-form boundary — allowed."""
        perm = self._build_perm(compression=3)
        perm.keyframe_count = 255
        decoded = decode_permutation(perm)   # must not raise
        assert decoded.frame_count == 255

    def test_bad_curve_falls_back_to_rest(self) -> None:
        """One bone with a genuinely unsupported curve format → rest fill.

        Regression for: a single bad curve sinking the whole animation
        in batch export (npc_crow had 27/48 anims fail). With the
        per-bone fallback in decode_permutation, the bad bone still
        decodes (using rest pose) and the rest of the animation is
        preserved.
        """
        perm = self._build_perm(bone_count=2, frame_count=3)
        # Stomp bone 0's translation curve with an unrecognised format
        # (count=2 forces the fmt check; fmt=0x05 is genuinely unknown).
        bogus = (
            struct.pack("<BBH", 2, 0x05, 0)
            + struct.pack("<3f", 0, 0, 0)  # baseline (12 bytes)
        )
        bogus = bogus.ljust(64, b"\x00")  # pad so length checks pass
        perm.header.translation_curves[0] = AnimCurve(0, len(bogus), bogus)

        rest = {0xAAAA0001: ((0, 0, 0, 1), (9.0, 9.0, 9.0), (1, 1, 1))}
        decoded = decode_permutation(perm, rest_pose=rest)

        # Despite the bad curve on bone 0, both bones decoded.
        assert len(decoded.bone_animations) == 2
        # Bone 0 fell back to its rest translation for every frame.
        bone0 = decoded.bone_animations[0]
        np.testing.assert_allclose(bone0.translations, 9.0)
        # Bone 1's curve was untouched and still decodes normally.
        bone1 = decoded.bone_animations[1]
        assert bone1.translations[0] == pytest.approx((0.0, 0.0, 0.0))
        assert bone1.translations[2] == pytest.approx((2.0, 4.0, 6.0))


class TestValidateAgainstRestPose:
    def _decoded_with_one_match_one_drift(self) -> DecodedAnimation:
        # bone 0: identity rotation, zero translation (= rest)
        # bone 1: a 90deg-Z rotation (off-rest)
        return DecodedAnimation(
            frame_rate=30.0, frame_count=1, compression=0,
            permutation_index=0,
            bone_animations=[
                # exact rest match
                __import__(
                    "d4extract.formats.anim_parser", fromlist=["DecodedBoneAnimation"]
                ).DecodedBoneAnimation(
                    bone_hash=0xA1,
                    translations=[(0.0, 0.0, 0.0)],
                    rotations=[(0.0, 0.0, 0.0, 1.0)],
                    scales=[(1.0, 1.0, 1.0)],
                ),
                # off-rest
                __import__(
                    "d4extract.formats.anim_parser", fromlist=["DecodedBoneAnimation"]
                ).DecodedBoneAnimation(
                    bone_hash=0xA2,
                    translations=[(0.0, 0.0, 0.0)],
                    rotations=[(0.0, 0.0, 0.7071, 0.7071)],  # 90deg Z
                    scales=[(1.0, 1.0, 1.0)],
                ),
            ],
        )

    def test_summary_counts_matches(self) -> None:
        decoded = self._decoded_with_one_match_one_drift()
        skeleton_rest = {
            0xA1: ((0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),
            0xA2: ((0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),
        }

        results, summary = validate_against_rest_pose(decoded, skeleton_rest)

        assert len(results) == 2
        # T match exact (both at 0,0,0)
        assert results[0].translation_error == pytest.approx(0.0)
        assert results[1].translation_error == pytest.approx(0.0)
        # R: bone 0 matches identity, bone 1 is 90deg off.
        assert results[0].rotation_error_deg == pytest.approx(0.0, abs=0.1)
        assert results[1].rotation_error_deg == pytest.approx(90.0, abs=0.5)
        # Both quaternions are unit length (within float tolerance).
        assert results[0].rotation_unit_length == pytest.approx(1.0, abs=1e-3)
        assert results[1].rotation_unit_length == pytest.approx(1.0, abs=1e-3)
        # Summary: 1 of 2 rotations match.
        assert "R match-rest (<1.0deg): 1 / 2" in summary
        assert "R unit-length" in summary

    def test_bones_not_in_skeleton_marked(self) -> None:
        decoded = self._decoded_with_one_match_one_drift()
        results, _ = validate_against_rest_pose(decoded, skeleton_rest={})

        for r in results:
            assert r.in_skeleton is False
            assert r.translation_error is None
            assert r.rotation_error_deg is None


# ─── Integration with extracted CASC fixtures (skipped in CI) ────────


# These tests run only if Phase 4's comp=3 fixtures have been
# extracted from a local D4 install — CI doesn't have CASC access, so
# they're opt-in.  When they run, they are the strongest correctness
# signal we have for the comp=3 decoder.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SAMPLES = _REPO_ROOT / "samples"
_D4DATA = _REPO_ROOT.parent / "d4data" / "json"
_FIXTURES_AVAILABLE = (
    (_SAMPLES / "base/payload/Anim/barM_HTH_nav_idle.ani").is_file()
    and (_SAMPLES / "base/payload/Appearance/barM_base00.app").is_file()
    and (_D4DATA / "base/meta/Anim/barM_HTH_nav_idle.ani.json").is_file()
)


@pytest.mark.skipif(
    not _FIXTURES_AVAILABLE,
    reason="comp=3 CASC fixtures not extracted (run Phase 4 step 0).",
)
class TestComp3RealFixtures:
    """Drive real comp=3 payloads through the decoder."""

    def test_barM_HTH_nav_idle_decodes_unit_quaternions(self) -> None:
        from d4extract.formats.anim_parser import (
            decode_permutation, parse_anim,
        )
        from d4extract.formats.app_parser import parse_app

        perm = parse_anim(
            _D4DATA / "base/meta/Anim/barM_HTH_nav_idle.ani.json",
            _SAMPLES / "base/payload/Anim/barM_HTH_nav_idle.ani",
            permutation_index=0,
        )
        assert perm.compression == 3
        mesh = parse_app(
            _SAMPLES / "base/meta/Appearance/barM_base00.app",
            _SAMPLES / "base/payload/Appearance/barM_base00.app",
        )
        assert mesh.skeleton is not None
        rest = {
            b.name_hash: (b.local_trs.q, b.local_trs.wp, b.local_trs.scale)
            for b in mesh.skeleton.bones
        }

        decoded = decode_permutation(perm, rest_pose=rest)

        assert len(decoded.bone_animations) == 190
        assert decoded.frame_count == 61
        # Every quaternion across every frame must be unit-length.
        for bone in decoded.bone_animations:
            for q in bone.rotations:
                length = (q[0]**2 + q[1]**2 + q[2]**2 + q[3]**2) ** 0.5
                assert abs(length - 1.0) < 0.02, (
                    f"non-unit quaternion for bone 0x{bone.bone_hash:08X}: "
                    f"{q} (|q|={length})"
                )
            assert len(bone.translations) == 61
            assert len(bone.rotations) == 61
            assert len(bone.scales) == 61

    def test_amazon_chest_neutral_matches_rest_pose(self) -> None:
        """A "Neutral" pose should decode exactly to the rest pose."""
        from d4extract.formats.anim_parser import (
            decode_permutation, parse_anim, validate_against_rest_pose,
        )
        from d4extract.formats.app_parser import parse_app

        anim = (
            _D4DATA
            / "base/meta/Anim/Amazon_Prop_Chest_Common_Dyn_Neutral.ani.json"
        )
        payload = (
            _SAMPLES
            / "base/payload/Anim/Amazon_Prop_Chest_Common_Dyn_Neutral.ani"
        )
        if not (anim.is_file() and payload.is_file()):
            pytest.skip("chest fixtures missing")

        perm = parse_anim(anim, payload)
        mesh = parse_app(
            _SAMPLES / "base/meta/Appearance/Amazon_Prop_Chest_Common_Dyn.app",
            _SAMPLES / "base/payload/Appearance/Amazon_Prop_Chest_Common_Dyn.app",
        )
        rest = {
            b.name_hash: (b.local_trs.q, b.local_trs.wp, b.local_trs.scale)
            for b in mesh.skeleton.bones
        }

        decoded = decode_permutation(perm, rest_pose=rest)
        results, _ = validate_against_rest_pose(decoded, rest)

        assert len(results) == 2
        for r in results:
            assert r.translation_error is not None
            assert r.translation_error < 1e-3, (
                f"bone 0x{r.bone_hash:08X} drifted: {r.translation_error}"
            )
            assert r.rotation_error_deg is not None
            assert r.rotation_error_deg < 1.0, (
                f"bone 0x{r.bone_hash:08X} rotated:"
                f" {r.rotation_error_deg}deg from rest"
            )


# ─── Integration with the flCompression=5 research corpus ────────────


# Drives a real comp=5 payload through the decoder.  It uses the corpus
# extracted for docs/research-flcompression5.md, which is large and may
# be gitignored — so it skips cleanly when absent, exactly like
# TestComp3RealFixtures above.
_COMP5_ANIM = "warM_2HS_attk_weaponAttack"
_COMP5_PAYLOAD = (
    _REPO_ROOT / "research" / "flcompression5" / "extracted" / _COMP5_ANIM
    / "base" / "payload" / "Anim" / f"{_COMP5_ANIM}.ani"
)
_COMP5_META = _D4DATA / "base" / "meta" / "Anim" / f"{_COMP5_ANIM}.ani.json"
_COMP5_AVAILABLE = _COMP5_PAYLOAD.is_file() and _COMP5_META.is_file()


@pytest.mark.skipif(
    not _COMP5_AVAILABLE,
    reason="comp=5 research corpus not present (research/flcompression5/).",
)
class TestComp5RealFixtures:
    """Drive a real flCompression=5 payload through the decoder."""

    def test_warM_attack_decodes_unit_quaternions(self) -> None:
        perm = parse_anim(_COMP5_META, _COMP5_PAYLOAD, permutation_index=0)
        assert perm.compression == 5

        decoded = decode_permutation(perm)

        assert len(decoded.bone_animations) == perm.bone_count
        assert decoded.frame_count == perm.keyframe_count
        for bone in decoded.bone_animations:
            assert len(bone.translations) == perm.keyframe_count
            assert len(bone.rotations) == perm.keyframe_count
            assert len(bone.scales) == perm.keyframe_count
            # Every quaternion across every frame must be unit-length —
            # the decisive signal that comp=5 bytes decode correctly.
            for q in bone.rotations:
                length = (q[0]**2 + q[1]**2 + q[2]**2 + q[3]**2) ** 0.5
                assert abs(length - 1.0) < 0.02, (
                    f"non-unit quaternion for bone 0x{bone.bone_hash:08X}: "
                    f"{tuple(q)} (|q|={length})"
                )


# ─── Integration with the flCompression=1 research corpus ────────────


# Real comp=1/2/4/6 payloads + a long-form (>255-frame) anim, from the
# corpus extracted for docs/research-flcompression1.md.  The corpus is
# large and may be gitignored, so these skip cleanly when absent —
# mirroring TestComp3RealFixtures / TestComp5RealFixtures above.
_COMP1_CORPUS = (
    _REPO_ROOT / "research" / "flcompression1" / "extracted"
    / "base" / "payload" / "Anim"
)
_LONGFORM_PAYLOAD = (
    _REPO_ROOT / "research" / "flcompression1" / "extracted_long"
    / "base" / "payload" / "Anim" / "Amalgam_reac_Death.ani"
)


@pytest.mark.skipif(
    not _COMP1_CORPUS.is_dir(),
    reason="comp=1 research corpus not present (research/flcompression1/).",
)
class TestModes1246RealFixtures:
    """Real comp=1/2/4/6 payloads decode through the comp=3 path.

    Cross-mode curve-blob aliasing proved these modes byte-identical to
    comp=3 for short-form animations — see docs/research-flcompression1.md.
    """

    def _decode_and_check(self, name: str, expected_comp: int) -> None:
        meta = _D4DATA / "base" / "meta" / "Anim" / f"{name}.ani.json"
        payload = _COMP1_CORPUS / f"{name}.ani"
        if not (meta.is_file() and payload.is_file()):
            pytest.skip(f"fixture missing: {name}")
        perm = parse_anim(meta, payload, permutation_index=0)
        assert perm.compression == expected_comp
        decoded = decode_permutation(perm)
        assert len(decoded.bone_animations) == perm.bone_count
        assert decoded.frame_count == perm.keyframe_count
        # Every quaternion unit-length — the decisive signal the bytes
        # decoded correctly through the comp=3 path.
        for bone in decoded.bone_animations:
            for q in bone.rotations:
                length = (q[0]**2 + q[1]**2 + q[2]**2 + q[3]**2) ** 0.5
                assert abs(length - 1.0) < 0.02, (
                    f"non-unit quaternion, bone 0x{bone.bone_hash:08X}: "
                    f"{tuple(q)} (|q|={length})"
                )

    def test_comp1_short_form_decodes(self) -> None:
        # spiF_gla_attk_centipede_core p0 — a Spiritborn glaive attack,
        # 56 frames, comp=1; one of the 8 Spiritborn-F builder failures.
        self._decode_and_check("spiF_gla_attk_centipede_core", 1)

    def test_comp2_decodes(self) -> None:
        self._decode_and_check("bandit_sword_nav_walk", 2)

    def test_comp4_decodes(self) -> None:
        # The only comp=4 file in all of d4data.
        self._decode_and_check(
            "treasuregoblin_nav_idle_unalert_outro_still", 4)

    def test_comp6_decodes(self) -> None:
        self._decode_and_check("warlock_tailStrike_attk_basic", 6)


@pytest.mark.skipif(
    not _LONGFORM_PAYLOAD.is_file(),
    reason="long-form fixture not present (research/flcompression1/).",
)
class TestLongFormGuard:
    """A real >255-frame animation must fail cleanly, not silently."""

    def test_amalgam_reac_death_fails_cleanly(self) -> None:
        # Amalgam_reac_Death is comp=3, 271 frames. Before the long-form
        # guard the u8-timestamp decoder mis-decoded it silently; it must
        # now raise a clean long-form error (no 'flCompression=' bucket).
        meta = (
            _D4DATA / "base" / "meta" / "Anim"
            / "Amalgam_reac_Death.ani.json"
        )
        if not meta.is_file():
            pytest.skip("Amalgam_reac_Death meta JSON missing")
        perm = parse_anim(meta, _LONGFORM_PAYLOAD, permutation_index=0)
        assert perm.keyframe_count == 271
        with pytest.raises(AnimFormatError, match=r"long-form") as excinfo:
            decode_permutation(perm)
        assert "flCompression=" not in str(excinfo.value)


# ─── Synthetic rest-pose animation ───────────────────────────────────


from d4extract.formats.anim_parser import (  # noqa: E402
    DecodedBoneAnimation,
    build_rest_pose_animation,
)
from d4extract.formats.app_parser import (  # noqa: E402
    Bone,
    BoneTransform,
    Skeleton,
)


def _posed_skeleton() -> Skeleton:
    """Three-bone skeleton, a distinct non-identity rest pose per bone."""
    transforms = [
        BoneTransform(q=(0.0, 0.0, 0.0, 1.0), wp=(0.0, 0.0, 0.0),
                      scale=(1.0, 1.0, 1.0)),
        BoneTransform(q=(0.0, 0.0, 0.7071, 0.7071), wp=(1.0, 2.0, 3.0),
                      scale=(2.0, 2.0, 2.0)),
        BoneTransform(q=(0.0, 1.0, 0.0, 0.0), wp=(-4.0, 5.0, -6.0),
                      scale=(0.5, 1.0, 1.5)),
    ]
    bones = [
        Bone(index=i, parent_index=i - 1, name_hash=0x1000 + i,
             flags=0, lod=0, local_trs=t, inv_bind_trs=t)
        for i, t in enumerate(transforms)
    ]
    return Skeleton(bones=bones, base_bone_count=3, cloth_bone_count=0,
                    template_id=7)


class TestBuildRestPoseAnimation:
    def test_one_bone_animation_per_bone(self) -> None:
        skel = _posed_skeleton()
        anim = build_rest_pose_animation(skel)

        assert isinstance(anim, DecodedAnimation)
        assert anim.name == "rest_pose"
        assert anim.frame_count == 1
        assert len(anim.bone_animations) == len(skel.bones)
        # The flag is left at its default — callers opt in explicitly.
        assert anim.force_static_channels is False

    def test_keyframe_values_equal_local_trs(self) -> None:
        skel = _posed_skeleton()
        anim = build_rest_pose_animation(skel)

        for bone, ba in zip(skel.bones, anim.bone_animations):
            assert isinstance(ba, DecodedBoneAnimation)
            # Keyed by name_hash so the exporter's bone lookup matches.
            assert ba.bone_hash == bone.name_hash
            # Exactly one keyframe per channel...
            assert len(ba.translations) == 1
            assert len(ba.rotations) == 1
            assert len(ba.scales) == 1
            # ...holding the bone's rest TRS verbatim.
            assert ba.translations[0] == pytest.approx(bone.local_trs.wp)
            assert ba.rotations[0] == pytest.approx(bone.local_trs.q)
            assert ba.scales[0] == pytest.approx(bone.local_trs.scale)

    def test_name_and_frame_rate_overridable(self) -> None:
        anim = build_rest_pose_animation(
            _posed_skeleton(), name="bind", frame_rate=24.0,
        )
        assert anim.name == "bind"
        assert anim.frame_rate == 24.0
