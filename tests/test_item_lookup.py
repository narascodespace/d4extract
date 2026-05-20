"""Tests for the equipment display-name index (item_lookup).

The synthetic-tree tests build a tiny d4data fixture under ``tmp_path``
— no real d4data dependency, same approach as ``test_anim_lookup``. One
optional regression test runs against a real d4data checkout (the seven
items the research report verified) and is skipped when absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from d4extract.gui.item_lookup import (
    _INDEX_CACHE,
    _INDEX_LOCK,
    _SCHEMA_VERSION,
    _build_index,
    _canon_class,
    _choose,
    _item_class,
    _item_quality,
    _load_cache_from_disk,
    _save_cache_to_disk,
    _source_mtimes,
    clear_cache,
    get_cached_index,
    get_display_info,
    get_display_name,
    get_or_build_index,
)


# ──────────────────────────────────────────────────────────────────────────────
# Synthetic d4data fixture
# ──────────────────────────────────────────────────────────────────────────────

# Actors (CoreTOC group 1): id → name.
_ACTORS = {
    "100": "axe_uniq01",
    "101": "HLM_sets50",
    "102": "HLM_crft25",
}

# Items (CoreTOC group 73): id → stem.
_ITEMS = {
    "200": "1HAxe_Legendary_Generic_001",
    "201": "Helm_Legendary_Generic_050",
    "202": "Helm_Unique_Generic_003",
    "203": "Helm_Magic_Barb_025_Crafted_L9",
    "204": "Helm_Magic_Rogue_025_Crafted_L9",
    "205": "Helm_Cosmetic_Barbarian_211_stor",
    "206": "Helm_Cosmetic_Paladin_211_stor",
}

# Reverse references: actor id → [item ids that reference it].
_INCOMING = {
    "100": [200],
    "101": [201, 202],   # one mesh backs a Legendary AND a Unique
    "102": [203, 204],   # one mesh backs a Barb AND a Rogue crafted helm
}

# Item stem → in-game name (the StringList payload).
_NAMES = {
    "1HAxe_Legendary_Generic_001": "Bearded Axe",
    "Helm_Legendary_Generic_050": "Boneweave Helm",
    "Helm_Unique_Generic_003": "Godslayer Crown",
    "Helm_Magic_Barb_025_Crafted_L9": "Wanderer's Helm",
    "Helm_Magic_Rogue_025_Crafted_L9": "Drifter's Hood",
    "Helm_Cosmetic_Barbarian_211_stor": "Demonheart Horns",
    "Helm_Cosmetic_Paladin_211_stor": "Crown of the Faithful",
}

# Appearance stems to drop into base/meta/Appearance/.
_APPEARANCES = [
    "axe_uniq01",               # weapon → Bearded Axe
    "axe_uniq01_swirlMesh",     # VFX sub-mesh → no actor → unresolved
    "barM_sets50_HLM",          # armor, quality tiebreak → Godslayer Crown
    "barM_crft25_HLM",          # armor, class pref → Wanderer's Helm (Barb)
    "rogM_crft25_HLM",          # armor, class pref → Drifter's Hood (Rogue)
    "barM_stor211_HLM",         # cosmetic → Demonheart Horns
    "palM_stor211_HLM",         # cosmetic, canon-class pal→paladin
    "goatman_base",             # not equipment → skipped entirely
]


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture()
def d4data(tmp_path: Path) -> Path:
    """A minimal synthetic d4data ``json/`` tree."""
    base = tmp_path / "json"

    _write_json(base / "base" / "CoreTOC.dat.json", {"1": _ACTORS, "73": _ITEMS})
    _write_json(base / "incomingSnoReferences.json", _INCOMING)

    appearance = base / "base" / "meta" / "Appearance"
    appearance.mkdir(parents=True)
    for stem in _APPEARANCES:
        # _build_index only scandirs names — content is irrelevant.
        (appearance / f"{stem}.app.json").write_text("{}", encoding="utf-8")

    stringlist = base / "enUS_Text" / "meta" / "StringList"
    stringlist.mkdir(parents=True)
    for item_stem, name in _NAMES.items():
        _write_json(
            stringlist / f"Item_{item_stem}.stl.json",
            {"arStrings": [{"szLabel": "Name", "szText": name}]},
        )

    return base


def _warm(d4data_path: Path) -> dict:
    """Build the index and install it into ``_INDEX_CACHE``."""
    key = Path(d4data_path).resolve()
    index = _build_index(d4data_path)
    with _INDEX_LOCK:
        _INDEX_CACHE[key] = index
    return index


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────────────


def test_canon_class_normalises_every_spelling() -> None:
    assert _canon_class("bar") == "barbarian"
    assert _canon_class("Barb") == "barbarian"
    assert _canon_class("Barbarian") == "barbarian"
    # The Paladin case the research flagged: prefix "pal" and item
    # token "Paladin" must canonicalise to the same id.
    assert _canon_class("pal") == _canon_class("Paladin") == "paladin"
    assert _canon_class("Necromancer") == "necromancer"
    assert _canon_class("Generic") is None
    assert _canon_class("") is None


def test_item_class_and_quality_parse_from_stem() -> None:
    assert _item_class("Helm_Unique_Rogue_002") == "rogue"
    assert _item_class("Helm_Legendary_Generic_050") is None
    assert _item_quality("X2_Helm_Legendary_Generic_base14") == "Legendary"
    assert _item_quality("Helm_Magic_Barb_025_Crafted_L9") == "Magic"


# ──────────────────────────────────────────────────────────────────────────────
# Build + resolution
# ──────────────────────────────────────────────────────────────────────────────


def test_build_indexes_only_equipment(d4data: Path) -> None:
    """Equipment appearances are indexed; non-equipment / VFX are not."""
    clear_cache()
    index = _build_index(d4data)
    # goatman_base isn't an equipment stem — never indexed.
    assert "goatman_base" not in index
    # The VFX sub-mesh matches the weapon prefix but has no Actor.
    assert "axe_uniq01_swirlmesh" not in index
    # The seven real equipment stems that resolve.
    assert {
        "axe_uniq01", "barm_sets50_hlm", "barm_crft25_hlm",
        "rogm_crft25_hlm", "barm_stor211_hlm", "palm_stor211_hlm",
    } <= set(index)


def test_weapon_resolves_through_same_name_actor(d4data: Path) -> None:
    clear_cache()
    _warm(d4data)
    assert get_display_name(d4data, "axe_uniq01") == "Bearded Axe"


def test_cosmetic_resolves_and_canon_class_fixes_paladin(d4data: Path) -> None:
    """Cosmetic armor matches on (slot, class, number).

    Covers the research's open question: 'pal' (mesh prefix) and
    'Paladin' (item stem) must canonicalise to the same class.
    """
    clear_cache()
    _warm(d4data)
    assert get_display_name(d4data, "barM_stor211_HLM") == "Demonheart Horns"
    assert get_display_name(d4data, "palM_stor211_HLM") == "Crown of the Faithful"


def test_disambiguation_quality_tiebreak(d4data: Path) -> None:
    """One mesh, two items, both Generic → the Unique wins over Legendary."""
    clear_cache()
    _warm(d4data)
    info = get_display_info(d4data, "barM_sets50_HLM")
    assert info is not None
    assert info.display_name == "Godslayer Crown"
    assert info.quality == "Unique"


def test_disambiguation_class_preference(d4data: Path) -> None:
    """One crafted mesh backs a Barb and a Rogue helm — the class hint
    in the appearance stem decides which name each gender-class gets."""
    clear_cache()
    _warm(d4data)
    assert get_display_name(d4data, "barM_crft25_HLM") == "Wanderer's Helm"
    assert get_display_name(d4data, "rogM_crft25_HLM") == "Drifter's Hood"


def test_lookup_is_case_insensitive(d4data: Path) -> None:
    """Real d4data mixes casing (barF_ vs BarF_) — lookups must not care."""
    clear_cache()
    _warm(d4data)
    assert get_display_name(d4data, "BARM_SETS50_HLM") == "Godslayer Crown"


def test_lookup_accepts_full_sno_path(d4data: Path) -> None:
    clear_cache()
    _warm(d4data)
    full = "base/meta/Appearance/barM_sets50_HLM.app"
    assert get_display_name(d4data, full) == "Godslayer Crown"


def test_unknown_stem_returns_none(d4data: Path) -> None:
    """A made-up stem returns None; the caller falls back to the stem."""
    clear_cache()
    _warm(d4data)
    assert get_display_name(d4data, "totally_not_a_real_stem") is None


def test_vfx_submesh_returns_none(d4data: Path) -> None:
    """A VFX sub-mesh is expected to be nameless."""
    clear_cache()
    _warm(d4data)
    assert get_display_name(d4data, "axe_uniq01_swirlMesh") is None


def test_get_display_name_no_index_returns_none(d4data: Path) -> None:
    """With no cached index, lookup returns None — never triggers a build."""
    clear_cache()
    assert get_cached_index(d4data) is None
    assert get_display_name(d4data, "axe_uniq01") is None


def test_choose_drops_unnamed_candidates() -> None:
    """A candidate whose StringList is missing is dropped, not chosen."""
    # Only the second candidate has a name.
    info = _choose(
        "barM_sets50_HLM",
        ["Helm_Unique_Generic_003", "Helm_Legendary_Generic_050"],
        {"Helm_Legendary_Generic_050": "Boneweave Helm"},
    )
    assert info is not None
    assert info.display_name == "Boneweave Helm"
    # No named candidate at all → None.
    assert _choose("barM_sets50_HLM", ["Helm_Unique_Generic_003"], {}) is None


# ──────────────────────────────────────────────────────────────────────────────
# Disk cache
# ──────────────────────────────────────────────────────────────────────────────


def test_disk_cache_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, d4data: Path,
) -> None:
    """save → load reproduces every ItemDisplayInfo field exactly."""
    clear_cache()
    cache_path = tmp_path / "item_cache.json"
    monkeypatch.setattr(
        "d4extract.gui.item_lookup._item_index_cache_path",
        lambda p: cache_path,
    )

    index = _build_index(d4data)
    _save_cache_to_disk(d4data, index)

    loaded = _load_cache_from_disk(d4data)
    assert loaded is not None
    assert set(loaded) == set(index)
    for stem, info in index.items():
        back = loaded[stem]
        assert back.display_name == info.display_name
        assert back.item_stem == info.item_stem
        assert back.quality == info.quality
        assert back.item_class == info.item_class


def test_disk_cache_stale_mtimes_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, d4data: Path,
) -> None:
    """A cache whose source mtimes don't match live d4data is discarded."""
    clear_cache()
    cache_path = tmp_path / "item_cache.json"
    monkeypatch.setattr(
        "d4extract.gui.item_lookup._item_index_cache_path",
        lambda p: cache_path,
    )

    stale = dict.fromkeys(_source_mtimes(d4data), 1.0)
    cache_path.write_text(json.dumps({
        "schema_version": _SCHEMA_VERSION,
        "d4data_path": str(d4data),
        "source_mtimes": stale,
        "entries": {},
    }), encoding="utf-8")

    assert _load_cache_from_disk(d4data) is None


def test_disk_cache_wrong_schema_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, d4data: Path,
) -> None:
    clear_cache()
    cache_path = tmp_path / "item_cache.json"
    monkeypatch.setattr(
        "d4extract.gui.item_lookup._item_index_cache_path",
        lambda p: cache_path,
    )
    cache_path.write_text(json.dumps({
        "schema_version": _SCHEMA_VERSION + 99,
        "d4data_path": str(d4data),
        "source_mtimes": _source_mtimes(d4data),
        "entries": {},
    }), encoding="utf-8")
    assert _load_cache_from_disk(d4data) is None


def test_get_or_build_caches_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, d4data: Path,
) -> None:
    """get_or_build_index installs the result into the in-memory cache."""
    clear_cache()
    monkeypatch.setattr(
        "d4extract.gui.item_lookup._item_index_cache_path",
        lambda p: tmp_path / "item_cache.json",
    )
    built = get_or_build_index(d4data)
    assert get_cached_index(d4data) is built
    clear_cache()


# ──────────────────────────────────────────────────────────────────────────────
# Regression against a real d4data checkout (skipped when absent)
# ──────────────────────────────────────────────────────────────────────────────

_REAL_D4DATA = Path(__file__).resolve().parents[2] / "d4data" / "json"

# The seven items the research report verified end-to-end.
_RESEARCH_SAMPLE = [
    ("barM_crft25_HLM", "Wanderer's Helm"),
    ("barM_uniq101_HLM", "Deathmask of Nirmitruq"),
    ("BarM_stor211_TRS", "Demonheart Carapace"),
    ("barM_uniq101_GLV", "Bane of\xa0Ahjad-Den"),
    ("barM_dlux100_LEG", "Warm Regards"),
    ("axe_uniq01", "Bearded Axe"),
    ("shield_uniq06", "Lidless Wall"),
]


@pytest.mark.skipif(
    not (_REAL_D4DATA / "base" / "CoreTOC.dat.json").is_file(),
    reason="real d4data checkout not present",
)
def test_research_sample_resolves_against_real_d4data() -> None:
    """Regression: the seven research-verified items must still resolve."""
    clear_cache()
    try:
        _warm(_REAL_D4DATA)
        for stem, expected in _RESEARCH_SAMPLE:
            assert get_display_name(_REAL_D4DATA, stem) == expected, stem
    finally:
        clear_cache()
