"""Tests for the parametric skin-tone palette extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from d4extract.formats.skin_tones import (
    SkinTone,
    load_skin_tones,
    save_skin_tones,
)

# Tests run with cwd = d4extract/. d4data is the sibling checkout.
D4DATA = Path("../d4data/json")
CACHE = Path("data/skin_tones.json")


def test_skintone_dict_roundtrip():
    tone = SkinTone(
        id="S00", label="Example", ebase=1,
        hue=0.1, saturation=-0.2, value=0.3, darken=0.5,
        ui_color=(0.1, 0.2, 0.3, 1.0),
    )
    assert SkinTone.from_dict(tone.to_dict()) == tone


def test_load_no_source_raises():
    with pytest.raises(FileNotFoundError):
        load_skin_tones(d4data_path=None, cache_path=None)


def test_cache_save_load_roundtrip(tmp_path):
    src = [
        SkinTone("S00", "a", 0, 0.0, 0.0, 0.0, 1.0, (1.0, 1.0, 1.0, 1.0)),
        SkinTone("S01", "b", 1, 0.5, -0.1, 0.2, 0.3, (0.5, 0.4, 0.3, 1.0)),
    ]
    path = tmp_path / "tones.json"
    save_skin_tones(src, path)
    assert path.is_file()
    assert load_skin_tones(cache_path=path) == src


@pytest.mark.skipif(not CACHE.is_file(), reason="skin_tones.json cache absent")
def test_load_from_checked_in_cache():
    tones = load_skin_tones(cache_path=CACHE)
    assert len(tones) >= 1
    assert all(isinstance(t, SkinTone) for t in tones)
    # ids are stable, source-ordered "S00".."S<n>".
    assert [t.id for t in tones] == [f"S{i:02d}" for i in range(len(tones))]


@pytest.mark.skipif(not D4DATA.is_dir(), reason="d4data not available")
def test_load_from_d4data():
    tones = load_skin_tones(d4data_path=D4DATA)
    assert len(tones) >= 1
    for t in tones:
        assert t.id.startswith("S")
        assert len(t.ui_color) == 4
        # flDarken is a brightness multiplier in [0, 1].
        assert 0.0 <= t.darken <= 1.0


@pytest.mark.skipif(not D4DATA.is_dir(), reason="d4data not available")
def test_cache_matches_d4data(tmp_path):
    """A fresh d4data read and a cache round-trip agree."""
    fresh = load_skin_tones(d4data_path=D4DATA)
    cache = tmp_path / "c.json"
    save_skin_tones(fresh, cache)
    assert load_skin_tones(cache_path=cache) == fresh
