"""Tests for the Diablo IV .app format parser."""

import struct
from pathlib import Path

import pytest

from d4extract.formats.app_parser import (
    APP_META_MAGIC,
    INDEX_FORMAT_U16,
    VERTEX_STRIDE_SIMPLE,
    AppFormatError,
    ConfigBlock,
    MetaHeader,
    MeshData,
    PayloadHeader,
    Vertex,
    _decode_half_float,
    _decode_indices_u16,
    _decode_indices_u32,
    _decode_vertices,
    _find_vertex_and_index_buffers,
    _scan_index_buffer_backward,
    _unpack_normal,
    _validate_vertex_region,
    identify_app_version,
    parse_app,
    parse_config_block,
    parse_meta_header,
    parse_payload_header,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _build_meta(
    *,
    magic: int = APP_META_MAGIC,
    link_id: int = 0x1234,
    struct_table_offset: int = 0x138,
    struct_entry_size: int = 88,
    config_block_offset: int = 0,
    config_block_size: int = 0,
    unknown_68: int = 0,
    lod_count: int = 4,
    config_block: bytes | None = None,
    total_size: int = 0x200,
) -> bytes:
    """Build a synthetic meta file."""
    buf = bytearray(total_size)
    struct.pack_into("<I", buf, 0x00, magic)
    struct.pack_into("<I", buf, 0x10, link_id)
    struct.pack_into("<I", buf, 0x40, struct_table_offset)
    struct.pack_into("<I", buf, 0x44, struct_entry_size)
    struct.pack_into("<I", buf, 0x60, config_block_offset)
    struct.pack_into("<I", buf, 0x64, config_block_size)
    struct.pack_into("<I", buf, 0x68, unknown_68)
    struct.pack_into("<I", buf, 0xBC, lod_count)
    if config_block and config_block_offset > 0:
        buf[config_block_offset : config_block_offset + len(config_block)] = config_block
    return bytes(buf)


def _build_config_block(
    *,
    index_buffer_offset: int = 0,
    index_buffer_size: int = 0,
    sentinel: int = 0xFFFFFFFF,
    flags: int = 0,
    geometry_data_size: int = 0,
) -> bytes:
    """Build a 416-byte config block."""
    buf = bytearray(416)
    struct.pack_into("<I", buf, 0x00, index_buffer_offset)
    struct.pack_into("<I", buf, 0x04, index_buffer_size)
    struct.pack_into("<I", buf, 0x08, sentinel)
    struct.pack_into("<I", buf, 0x0C, flags)
    struct.pack_into("<I", buf, 0x24, geometry_data_size)
    return bytes(buf)


def _build_payload(
    *,
    link_id: int = 0x1234,
    group_count: int = 0,
    header_data_size: int = 0x20,
    vertices: list[tuple[float, float, float]] | None = None,
    indices: list[int] | None = None,
    extra_prefix: bytes = b"",
) -> bytes:
    """Build a synthetic payload file with vertex and index data."""
    hdr = bytearray(header_data_size)
    struct.pack_into("<I", hdr, 0x00, link_id)
    struct.pack_into("<I", hdr, 0x08, group_count)
    struct.pack_into("<I", hdr, 0x0C, header_data_size)

    vbuf = bytearray()
    if vertices:
        for x, y, z in vertices:
            v = bytearray(VERTEX_STRIDE_SIMPLE)
            struct.pack_into("<fff", v, 0, x, y, z)
            # Normal: (0, 1, 0, 0) packed
            v[12], v[13], v[14], v[15] = 127, 255, 127, 127
            # Color: white
            v[16], v[17], v[18], v[19] = 255, 255, 255, 255
            # Tangent at +32
            v[32], v[33], v[34], v[35] = 255, 127, 127, 127
            vbuf.extend(v)

    ibuf = bytearray()
    if indices:
        for idx in indices:
            ibuf.extend(struct.pack("<H", idx))

    return bytes(hdr) + extra_prefix + bytes(vbuf) + bytes(ibuf)


# ── parse_meta_header ────────────────────────────────────────────────


class TestParseMetaHeader:
    def test_valid_header(self):
        data = _build_meta(link_id=0xABCD, unknown_68=0x801, lod_count=8)
        hdr = parse_meta_header(data)
        assert hdr.magic == APP_META_MAGIC
        assert hdr.link_id == 0xABCD
        assert hdr.struct_table_offset == 0x138
        assert hdr.struct_entry_size == 88
        assert hdr.unknown_68 == 0x801
        assert hdr.lod_or_submesh_count == 8

    def test_wrong_magic_raises(self):
        data = _build_meta(magic=0x12345678)
        with pytest.raises(AppFormatError, match="Invalid meta magic"):
            parse_meta_header(data)

    def test_too_small_raises(self):
        with pytest.raises(AppFormatError, match="too small"):
            parse_meta_header(b"\x00" * 100)

    def test_config_block_fields(self):
        data = _build_meta(config_block_offset=0x150, config_block_size=0x1A0)
        hdr = parse_meta_header(data)
        assert hdr.config_block_offset == 0x150
        assert hdr.config_block_size == 0x1A0


# ── parse_config_block ───────────────────────────────────────────────


class TestParseConfigBlock:
    def test_absent_when_offset_zero(self):
        hdr = MetaHeader(config_block_offset=0, config_block_size=0)
        assert parse_config_block(b"\x00" * 512, hdr) is None

    def test_parses_fields(self):
        blk = _build_config_block(
            index_buffer_offset=1000,
            index_buffer_size=500,
            sentinel=0xFFFFFFFF,
            flags=1,
            geometry_data_size=2000,
        )
        meta = _build_meta(
            config_block_offset=0x100,
            config_block_size=416,
            total_size=0x100 + 416,
            config_block=blk,
        )
        hdr = parse_meta_header(meta)
        config = parse_config_block(meta, hdr)
        assert config is not None
        assert config.index_buffer_offset == 1000
        assert config.index_buffer_size == 500
        assert config.sentinel == 0xFFFFFFFF
        assert config.flags == 1
        assert config.geometry_data_size == 2000

    def test_out_of_bounds_raises(self):
        hdr = MetaHeader(config_block_offset=500, config_block_size=416)
        with pytest.raises(AppFormatError, match="out of bounds"):
            parse_config_block(b"\x00" * 100, hdr)


# ── parse_payload_header ─────────────────────────────────────────────


class TestParsePayloadHeader:
    def test_valid(self):
        data = _build_payload(link_id=0xBEEF, group_count=4, header_data_size=0x4520)
        hdr = parse_payload_header(data)
        assert hdr.link_id == 0xBEEF
        assert hdr.group_count == 4
        assert hdr.header_data_size == 0x4520

    def test_too_small_raises(self):
        with pytest.raises(AppFormatError, match="too small"):
            parse_payload_header(b"\x00" * 10)


# ── _unpack_normal ───────────────────────────────────────────────────


class TestUnpackNormal:
    """eFormat 8 packs each axis as a signed 8-bit integer (DXGI
    R8G8B8A8_SNORM): byte 0x00 → 0.0, 0x7F → +1.0, 0x80 → -1.008.
    The earlier ``b/127.5 - 1`` formula scored mean dot ``-0.51``
    against geometric face normals on Goatman; the signed-byte
    formula scores ``+0.94``."""

    def test_center_value(self):
        # Byte 0x00 is the neutral value under signed interpretation.
        nx, ny, nz, nw = _unpack_normal(0, 0, 0, 0)
        assert abs(nx) < 0.01
        assert abs(ny) < 0.01
        assert abs(nz) < 0.01

    def test_max_value(self):
        # 0x7F (127) is the most-positive signed byte.
        nx, _, _, _ = _unpack_normal(127, 0, 0, 0)
        assert nx == pytest.approx(1.0, abs=0.01)

    def test_min_value(self):
        # 0x80 (128) is -128 under 2's complement → -128/127 ≈ -1.008.
        nx, _, _, _ = _unpack_normal(128, 0, 0, 0)
        assert nx == pytest.approx(-1.0, abs=0.01)

    def test_negative_quarter(self):
        # 0xC0 (192) is -64 under 2's complement → -64/127 ≈ -0.504.
        nx, _, _, _ = _unpack_normal(192, 0, 0, 0)
        assert nx == pytest.approx(-0.5, abs=0.01)


# ── _decode_half_float ───────────────────────────────────────────────


class TestDecodeHalfFloat:
    def test_zero(self):
        assert _decode_half_float(0) == 0.0

    def test_one(self):
        assert _decode_half_float(0x3C00) == pytest.approx(1.0)

    def test_negative_one(self):
        assert _decode_half_float(0xBC00) == pytest.approx(-1.0)

    def test_half(self):
        assert _decode_half_float(0x3800) == pytest.approx(0.5)

    def test_inf(self):
        import math
        assert math.isinf(_decode_half_float(0x7C00))

    def test_nan(self):
        import math
        assert math.isnan(_decode_half_float(0x7C01))


# ── _decode_vertices ─────────────────────────────────────────────────


class TestDecodeVertices:
    def _make_vertex_buf(self, positions):
        buf = bytearray()
        for x, y, z in positions:
            v = bytearray(VERTEX_STRIDE_SIMPLE)
            struct.pack_into("<fff", v, 0, x, y, z)
            # Bytes are signed (DXGI R8G8B8A8_SNORM): byte 0x7F = +1.
            # Encode Y-up normal: (0, +1, 0, 0).
            v[12:16] = bytes([0, 127, 0, 0])
            v[16:20] = bytes([255, 0, 0, 255])  # color: red
            # Tangent +X with handedness w=+1.
            v[32:36] = bytes([127, 0, 0, 127])
            buf.extend(v)
        return bytes(buf)

    def test_single_vertex(self):
        buf = self._make_vertex_buf([(1.0, 2.0, 3.0)])
        pos, norm, col, tan, uv = _decode_vertices(buf, 0, 1)
        assert len(pos) == 1
        assert pos[0] == pytest.approx((1.0, 2.0, 3.0))
        assert norm[0][1] == pytest.approx(1.0, abs=0.01)  # Y-up normal
        assert col[0] == pytest.approx((1.0, 0.0, 0.0, 1.0))
        assert len(uv) == 1

    def test_multiple_vertices(self):
        verts = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
        buf = self._make_vertex_buf(verts)
        pos, norm, col, tan, uv = _decode_vertices(buf, 0, 3)
        assert len(pos) == 3
        for i, expected in enumerate(verts):
            assert pos[i] == pytest.approx(expected)
        assert len(uv) == 3

    def test_offset(self):
        """Vertices can be read from a non-zero offset."""
        prefix = b"\x00" * 100
        buf = prefix + self._make_vertex_buf([(5.0, 6.0, 7.0)])
        pos, _, _, _, _ = _decode_vertices(buf, 100, 1)
        assert pos[0] == pytest.approx((5.0, 6.0, 7.0))

    def test_overflow_raises(self):
        buf = self._make_vertex_buf([(0, 0, 0)])
        with pytest.raises(AppFormatError, match="overflow"):
            _decode_vertices(buf, 0, 10)


# ── _decode_indices ──────────────────────────────────────────────────


class TestDecodeIndices:
    def test_u16(self):
        buf = struct.pack("<6H", 0, 1, 2, 2, 1, 3)
        result = _decode_indices_u16(buf, 0, 6)
        assert result == [0, 1, 2, 2, 1, 3]

    def test_u16_offset(self):
        prefix = b"\xFF" * 10
        buf = prefix + struct.pack("<3H", 10, 20, 30)
        result = _decode_indices_u16(buf, 10, 3)
        assert result == [10, 20, 30]

    def test_u16_overflow(self):
        buf = struct.pack("<3H", 0, 1, 2)
        with pytest.raises(AppFormatError, match="overflow"):
            _decode_indices_u16(buf, 0, 100)

    def test_u32(self):
        buf = struct.pack("<3I", 100, 200, 300)
        result = _decode_indices_u32(buf, 0, 3)
        assert result == [100, 200, 300]

    def test_u32_overflow(self):
        with pytest.raises(AppFormatError, match="overflow"):
            _decode_indices_u32(b"\x00" * 8, 0, 100)


# ── _validate_vertex_region ──────────────────────────────────────────


class TestValidateVertexRegion:
    def test_valid_region(self):
        buf = bytearray()
        for i in range(20):
            v = bytearray(VERTEX_STRIDE_SIMPLE)
            struct.pack_into("<fff", v, 0, float(i), float(i) * 0.5, float(i) * -0.3)
            buf.extend(v)
        assert _validate_vertex_region(bytes(buf), 0, 20) is True

    def test_invalid_region(self):
        # Fill with large values that don't look like positions
        buf = b"\xFF" * (20 * VERTEX_STRIDE_SIMPLE)
        assert _validate_vertex_region(buf, 0, 20) is False

    def test_too_short(self):
        assert _validate_vertex_region(b"\x00" * 10, 0, 20) is False


# ── _scan_index_buffer_backward ──────────────────────────────────────


class TestScanIndexBufferBackward:
    def test_finds_index_buffer(self):
        # Build: some junk + index buffer + padding zeros
        junk = b"\xFF\xFF\xFF\xFF" * 100
        indices = struct.pack("<9H", 0, 1, 2, 2, 1, 3, 0, 3, 1)
        padding = b"\x00" * 8
        data = junk + indices + padding

        result = _scan_index_buffer_backward(data)
        assert result is not None
        off, count, max_idx, fmt = result
        assert count == 9
        assert max_idx == 3
        assert fmt == INDEX_FORMAT_U16

    def test_empty_data(self):
        assert _scan_index_buffer_backward(b"\x00" * 100) is None

    def test_too_small(self):
        assert _scan_index_buffer_backward(b"\x01\x00") is None


# ── parse_app (integration) ──────────────────────────────────────────


class TestParseApp:
    def _make_simple_model(self, tmp_path):
        """Create a valid simple model (group_count=0) with config block."""
        verts = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0), (1.5, 2.5, 3.5)]
        indices = [0, 1, 2, 2, 1, 3]

        # Build payload: header + vertex data + index data
        hdr_size = 0x20
        vbuf_size = len(verts) * VERTEX_STRIDE_SIMPLE
        ibuf_size = len(indices) * 2
        idx_offset = hdr_size + vbuf_size

        payload = _build_payload(
            link_id=0x9999,
            group_count=0,
            header_data_size=hdr_size,
            vertices=verts,
            indices=indices,
        )

        # Build config block with index buffer location
        config = _build_config_block(
            index_buffer_offset=idx_offset,
            index_buffer_size=ibuf_size,
        )

        meta = _build_meta(
            link_id=0x9999,
            config_block_offset=0x100,
            config_block_size=416,
            total_size=0x100 + 416,
            config_block=config,
        )

        meta_path = tmp_path / "test_model.app"
        payload_path = tmp_path / "test_model_payload.app"
        meta_path.write_bytes(meta)
        payload_path.write_bytes(payload)
        return meta_path, payload_path, verts, indices

    def test_roundtrip(self, tmp_path):
        meta_path, payload_path, verts, indices = self._make_simple_model(tmp_path)
        mesh = parse_app(meta_path, payload_path)

        assert mesh.name == "test_model"
        assert mesh.vertex_count == 4
        assert mesh.index_count == 6
        assert len(mesh.positions) == 4
        assert len(mesh.indices) == 6
        for i, expected in enumerate(verts):
            assert mesh.positions[i] == pytest.approx(expected)
        assert mesh.indices == indices

    def test_link_id_mismatch_raises(self, tmp_path):
        meta = _build_meta(link_id=0x1111)
        payload = _build_payload(link_id=0x2222)
        meta_path = tmp_path / "m.app"
        payload_path = tmp_path / "p.app"
        meta_path.write_bytes(meta)
        payload_path.write_bytes(payload)

        with pytest.raises(AppFormatError, match="Link ID mismatch"):
            parse_app(meta_path, payload_path)

    def test_link_id_mismatch_allowed_when_flagged(self, tmp_path, caplog):
        """Shared-payload models intentionally have differing link IDs."""
        meta_path, payload_path, _, _ = self._make_simple_model(tmp_path)
        # Rewrite the payload with a different link_id while keeping
        # buffers identical, so parsing can succeed past the link check.
        verts = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0), (1.5, 2.5, 3.5)]
        indices = [0, 1, 2, 2, 1, 3]
        payload_path.write_bytes(_build_payload(
            link_id=0xDEAD,  # meta is 0x9999
            group_count=0,
            header_data_size=0x20,
            vertices=verts,
            indices=indices,
        ))

        with caplog.at_level("WARNING"):
            mesh = parse_app(
                meta_path, payload_path, allow_link_mismatch=True,
            )

        assert mesh.vertex_count == 4
        assert any("Link ID mismatch" in rec.message for rec in caplog.records)

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_app(tmp_path / "nope.app", tmp_path / "nope2.app")

    def test_mesh_data_has_normals_and_tangents(self, tmp_path):
        meta_path, payload_path, _, _ = self._make_simple_model(tmp_path)
        mesh = parse_app(meta_path, payload_path)
        assert len(mesh.normals) == 4
        assert len(mesh.tangents) == 4
        assert len(mesh.colors) == 4

    def test_meta_header_attached(self, tmp_path):
        meta_path, payload_path, _, _ = self._make_simple_model(tmp_path)
        mesh = parse_app(meta_path, payload_path)
        assert mesh.meta_header is not None
        assert mesh.meta_header.magic == APP_META_MAGIC
        assert mesh.payload_header is not None
        assert mesh.payload_header.link_id == 0x9999


# ── identify_app_version ─────────────────────────────────────────────


class TestIdentifyAppVersion:
    def test_d4_v1(self, tmp_path):
        meta = _build_meta()
        path = tmp_path / "test.app"
        path.write_bytes(meta)
        assert identify_app_version(path) == "d4_v1"

    def test_unknown_version(self, tmp_path):
        meta = _build_meta(struct_table_offset=999, struct_entry_size=999)
        path = tmp_path / "test.app"
        path.write_bytes(meta)
        assert identify_app_version(path) == "d4_unknown"

    def test_invalid_magic_raises(self, tmp_path):
        path = tmp_path / "test.app"
        path.write_bytes(b"\x00" * 256)
        with pytest.raises(AppFormatError):
            identify_app_version(path)
