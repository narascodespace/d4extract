"""Tests for the ``.tex`` decoder.

The decoder synthesises a DDS header around a raw block-compressed
payload and hands it to Pillow. These tests cover the codec dispatch
logic (which is pure Python) and the DDS header layout. We also
exercise Pillow's BC1 / BC3 / BC7 decoders end-to-end on synthetic
all-zero block patterns — they're small enough to construct inline
and exercise the full pipeline without checking real game payloads
into the repo.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

import pytest

from d4extract.formats.texture_parser import (
    TEX_FORMATS,
    TextureDecodeError,
    _build_dds_header,
    _resolve_codec,
    decode_tex,
)


# ---------------------------------------------------------------------------
# Codec dispatch — eTexFormat → TexCodec
# ---------------------------------------------------------------------------


class TestResolveCodec:
    def test_unknown_format_returns_none(self):
        assert _resolve_codec(999, 64, 64, 1234) is None

    def test_format_46_is_bc1(self):
        # 1024x1024 BC1 = 524288 bytes. Either size resolves to BC1
        # (fmt 46 is unambiguous).
        codec = _resolve_codec(46, 1024, 1024, 524288)
        assert codec is not None
        assert codec.fourcc == b"DXT1"
        assert codec.bytes_per_pixel == 0.5

    def test_format_42_is_bc5(self):
        codec = _resolve_codec(42, 2048, 2048, 4194304)
        assert codec.fourcc == b"ATI2"
        assert codec.bytes_per_pixel == 1.0

    def test_format_50_is_bc7_dx10(self):
        codec = _resolve_codec(50, 1024, 1024, 1048576)
        assert codec.fourcc == b"DX10"
        assert codec.bytes_per_pixel == 1.0
        # DXGI_FORMAT_BC7_UNORM_SRGB = 99
        assert codec.dxgi_format == 99

    def test_format_49_bc3_payload_size_keeps_bc3(self):
        """Emissive/Translucency variant: payload is exactly BC3-sized,
        so the dispatch must keep the BC3 (DXT5) codec it advertises."""
        # 512x512 BC3 = 262144 bytes.
        codec = _resolve_codec(49, 512, 512, 262144)
        assert codec.fourcc == b"DXT5"
        assert codec.bytes_per_pixel == 1.0

    def test_format_49_bc1_payload_size_downgrades_to_bc1(self):
        """Character body Colour variant: payload is half the BC3 size,
        so the dispatch must downgrade to BC1."""
        # 1024x1024 BC1 = 524288 bytes (half of the 1048576 BC3 size).
        codec = _resolve_codec(49, 1024, 1024, 524288)
        assert codec.fourcc == b"DXT1"
        assert codec.bytes_per_pixel == 0.5

    def test_format_49_off_size_keeps_bc3(self):
        """A size that matches neither BC1 nor BC3 exactly should keep
        the advertised BC3 codec — decode_tex will emit a warning and
        let Pillow try."""
        codec = _resolve_codec(49, 1024, 1024, 700000)
        assert codec.fourcc == b"DXT5"


# ---------------------------------------------------------------------------
# DDS header layout
# ---------------------------------------------------------------------------


class TestBuildDdsHeader:
    def test_legacy_fourcc_header_is_128_bytes(self):
        h = _build_dds_header(64, 64, b"DXT1", 2048)
        # 4-byte magic + 124-byte DDS_HEADER.
        assert len(h) == 128
        assert h[:4] == b"DDS "

    def test_dx10_header_appends_extension(self):
        h = _build_dds_header(64, 64, b"DX10", 4096, dxgi_format=99)
        # 4-byte magic + 124-byte DDS_HEADER + 20-byte DXT10 extension.
        assert len(h) == 148
        # DXGI format ID is the first DWORD of the DXT10 ext.
        dxgi = struct.unpack("<I", h[128:132])[0]
        assert dxgi == 99
        # resourceDimension = 3 (TEXTURE2D).
        assert struct.unpack("<I", h[132:136])[0] == 3

    def test_rejects_bad_fourcc(self):
        with pytest.raises(ValueError, match="FourCC must be 4 bytes"):
            _build_dds_header(64, 64, b"BC1", 2048)


# ---------------------------------------------------------------------------
# decode_tex — end-to-end via Pillow on synthetic payloads
# ---------------------------------------------------------------------------


def _make_payload(tmp_path: Path, name: str, payload: bytes) -> Path:
    """Write a ``.tex``-style raw payload to disk and return its path."""
    p = tmp_path / name
    p.write_bytes(payload)
    return p


class TestDecodeTexEndToEnd:
    """End-to-end smoke tests against Pillow's DDS decoder.

    These don't ship real game payloads — they construct the smallest
    legal all-zero BC block pattern for each codec, which Pillow
    happily decodes into an opaque solid-color image. The point is to
    catch regressions in the DDS header construction / dispatch wiring,
    not to validate the codec output itself.
    """

    def test_bc1_round_trip(self, tmp_path: Path):
        # 4x4 BC1 = 1 block = 8 bytes. All-zero block decodes to
        # alpha=1.0, RGB=(0,0,0) — i.e. opaque black.
        payload = b"\x00" * 8
        p = _make_payload(tmp_path, "bc1.tex", payload)
        img = decode_tex(p, 4, 4, fmt=46)
        assert img.size == (4, 4)
        # BC1 produces RGBA (the format carries 1-bit punch-through alpha).
        assert img.mode == "RGBA"

    def test_bc7_via_format_50(self, tmp_path: Path):
        # 4x4 BC7 = 1 block = 16 bytes. Mode-0 block with all-zero
        # payload decodes to an all-black 4x4 tile.
        block = bytes([0x01]) + b"\x00" * 15
        p = _make_payload(tmp_path, "bc7.tex", block)
        img = decode_tex(p, 4, 4, fmt=50)
        assert img.size == (4, 4)
        # BC7 carries 8-bit alpha; expect RGBA.
        assert img.mode == "RGBA"

    def test_format_49_bc3_path(self, tmp_path: Path):
        # 4x4 BC3 = 16 bytes (8-byte alpha block + 8-byte BC1 colour).
        # All-zero alpha block has endpoints (0,0) which decodes to a
        # fully *transparent* block — fine for the parser-level test,
        # we just need a non-throwing decode.
        payload = b"\x00" * 16
        p = _make_payload(tmp_path, "fmt49_bc3.tex", payload)
        img = decode_tex(p, 4, 4, fmt=49)
        # The parser strips alpha for fmt 49 BC3 because the alpha
        # channel encodes engine data, not pixel transparency.
        assert img.mode == "RGB"

    def test_format_49_bc1_downgrade_preserves_alpha(self, tmp_path: Path):
        # 4x4 BC1 = 8 bytes (half the BC3 size). Resolver must pick BC1
        # and the alpha-strip branch must NOT fire.
        payload = b"\x00" * 8
        p = _make_payload(tmp_path, "fmt49_bc1.tex", payload)
        img = decode_tex(p, 4, 4, fmt=49)
        # BC1 alpha is real (1-bit punch-through). Keep it.
        assert img.mode == "RGBA"

    def test_missing_payload_raises(self, tmp_path: Path):
        with pytest.raises(TextureDecodeError, match="payload not found"):
            decode_tex(tmp_path / "ghost.tex", 64, 64, fmt=46)

    def test_unsupported_format_raises(self, tmp_path: Path):
        p = _make_payload(tmp_path, "weird.tex", b"\x00" * 64)
        with pytest.raises(TextureDecodeError, match="Unsupported eTexFormat"):
            decode_tex(p, 16, 16, fmt=999)


# ---------------------------------------------------------------------------
# Format table sanity
# ---------------------------------------------------------------------------


class TestFormatTable:
    def test_known_formats_have_entries(self):
        for fmt in (9, 10, 41, 42, 46, 47, 49, 50):
            assert fmt in TEX_FORMATS, f"fmt {fmt} missing from table"

    def test_bc7_entry_has_dx10_marker(self):
        """fmt 50 must use DX10 because Pillow only decodes BC7 via the
        extended header."""
        assert TEX_FORMATS[50].fourcc == b"DX10"
        assert TEX_FORMATS[50].dxgi_format != 0
