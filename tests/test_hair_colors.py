"""Tests for the parametric hair-colour palette extractor."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from d4extract.formats.hair_colors import (
    _HCL_NAME_RE,
    HairColor,
    HairColorError,
    load_hair_colors,
    save_hair_colors,
)

# Tests run with cwd = d4extract/. d4data is the sibling checkout.
D4DATA = Path("../d4data/json")
CACHE = Path("data/hair_colors.json")
_HAIR_DIR = D4DATA / "base" / "meta" / "HairColor"


def _hc(id: str = "H01", **kw) -> HairColor:
    """Build a :class:`HairColor` with sensible defaults for tests."""
    base = dict(
        id=id,
        display_name="X",
        sort_order=0,
        usable_by=(1, 1, 1, 1, 1, 1, 1, 1),
        rgba_colors=((0.0, 0.0, 0.0, 1.0),) * 3,
        rgba_colors2=((0.5, 0.5, 0.5, 1.0),) * 3,
        influence=1.0,
    )
    base.update(kw)
    return HairColor(**base)


def _minimal_hcl(sort_order: int) -> str:
    """A minimal HairColorDefinition JSON document."""
    return json.dumps({
        "nSortOrder": sort_order,
        "fUsableByClass": [1] * 8,
        "rgbaColors": [{"r": 0, "g": 0, "b": 0, "a": 255}] * 3,
        "rgbaColors2": [{"r": 0, "g": 0, "b": 0, "a": 255}] * 3,
        "flHairColorInfluence": 1,
    })


def test_haircolor_dict_roundtrip():
    hc = _hc(
        id="H07", display_name="ReddishBlonde", sort_order=6, influence=0.5,
        rgba_colors=(
            (0.1, 0.2, 0.3, 1.0), (0.4, 0.5, 0.6, 1.0), (0.7, 0.8, 0.9, 1.0),
        ),
    )
    assert HairColor.from_dict(hc.to_dict()) == hc


def test_load_no_source_raises():
    with pytest.raises(FileNotFoundError):
        load_hair_colors(d4data_path=None, cache_path=None)


def test_cache_save_load_roundtrip(tmp_path):
    src = [_hc(id="H01"), _hc(id="H02", display_name="b", influence=0.5)]
    path = tmp_path / "hc.json"
    save_hair_colors(src, path)
    assert path.is_file()
    assert load_hair_colors(d4data_path=None, cache_path=path) == src


def test_name_regex_excludes_axe_bad_data():
    """The NNN<Name> regex filters 'Axe Bad Data' and off-pattern files."""
    assert _HCL_NAME_RE.match("Axe Bad Data.hcl.json") is None
    assert _HCL_NAME_RE.match("junk.hcl.json") is None
    assert _HCL_NAME_RE.match("01Short.hcl.json") is None
    m = _HCL_NAME_RE.match("001DeepBrown.hcl.json")
    assert m is not None
    assert m.group(1) == "001"
    assert m.group(2) == "DeepBrown"


def test_short_list_raises(tmp_path):
    """Fewer than 31 valid entries -> HairColorError, not a short list."""
    hair_dir = tmp_path / "base" / "meta" / "HairColor"
    hair_dir.mkdir(parents=True)
    for i in range(1, 4):
        (hair_dir / f"{i:03d}Colour{i}.hcl.json").write_text(
            _minimal_hcl(i - 1), encoding="utf-8",
        )
    with pytest.raises(HairColorError) as exc:
        load_hair_colors(d4data_path=tmp_path, cache_path=None)
    # The error names the count and the directory it searched.
    assert "3" in str(exc.value)


@pytest.mark.skipif(not CACHE.is_file(), reason="hair_colors.json cache absent")
def test_load_from_checked_in_cache():
    colors = load_hair_colors(d4data_path=None, cache_path=CACHE)
    assert len(colors) == 31
    assert all(isinstance(c, HairColor) for c in colors)
    # ids are stable, sort-ordered "H01".."H31".
    assert [c.id for c in colors] == [f"H{i:02d}" for i in range(1, 32)]
    # Entries are returned in non-decreasing nSortOrder.
    orders = [c.sort_order for c in colors]
    assert orders == sorted(orders)


@pytest.mark.skipif(not _HAIR_DIR.is_dir(), reason="d4data not available")
def test_load_from_d4data():
    colors = load_hair_colors(d4data_path=D4DATA, cache_path=None)
    assert len(colors) == 31
    for c in colors:
        assert c.id.startswith("H")
        assert len(c.rgba_colors) == 3
        assert len(c.rgba_colors2) == 3
        for rgba in c.rgba_colors + c.rgba_colors2:
            assert len(rgba) == 4
            assert all(0.0 <= ch <= 1.0 for ch in rgba)
    # 001DeepBrown's primary tint is uint8 zero -> float zero.
    deep_brown = next(c for c in colors if c.display_name == "DeepBrown")
    assert deep_brown.rgba_colors[0] == (0.0, 0.0, 0.0, 1.0)


@pytest.mark.skipif(not _HAIR_DIR.is_dir(), reason="d4data not available")
def test_axe_bad_data_and_junk_excluded(tmp_path):
    """A real HairColor dir + a junk file -> the loader still yields 31.

    The directory already contains the dev-garbage ``Axe Bad Data.hcl.json``;
    an extra off-pattern ``junk.hcl.json`` is added. Exactly 31 surviving
    entries proves both are filtered.
    """
    hair_dir = tmp_path / "base" / "meta" / "HairColor"
    shutil.copytree(_HAIR_DIR, hair_dir)
    assert (hair_dir / "Axe Bad Data.hcl.json").is_file()
    (hair_dir / "junk.hcl.json").write_text("{}", encoding="utf-8")

    colors = load_hair_colors(d4data_path=tmp_path, cache_path=None)
    assert len(colors) == 31
    assert all("Axe" not in c.display_name for c in colors)
    assert all("junk" not in c.display_name.lower() for c in colors)


@pytest.mark.skipif(not _HAIR_DIR.is_dir(), reason="d4data not available")
def test_sort_order_drives_id_not_filename(tmp_path):
    """``id`` follows nSortOrder — not the filename prefix.

    The d4data filenames and the nSortOrder field disagree (e.g.
    ``015White`` sorts before ``012DarkGray``), so this checks the
    loader honours the JSON field.
    """
    fresh = load_hair_colors(d4data_path=D4DATA, cache_path=None)
    cache = tmp_path / "c.json"
    save_hair_colors(fresh, cache)
    assert load_hair_colors(d4data_path=None, cache_path=cache) == fresh
    # ids are assigned by sort position, 1-indexed.
    for index, c in enumerate(fresh):
        assert c.id == f"H{index + 1:02d}"
