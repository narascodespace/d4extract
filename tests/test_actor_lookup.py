"""Tests for the actor display-name index (actor_lookup).

The synthetic-tree tests build a tiny d4data fixture under ``tmp_path``
— no real d4data dependency, same approach as ``test_item_lookup``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from d4extract.gui.actor_lookup import (
    _INDEX_CACHE,
    _INDEX_LOCK,
    _SCHEMA_VERSION,
    _build_index,
    _canon,
    _load_cache_from_disk,
    _pick_actor,
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
# - "fallen_shaman" is the clean canonical actor for the FallenShaman
#   appearance.
# - "fallen_shaman_lightning" is a variant — same appearance but a
#   different display name ("Vile Shaman").
# - "BSK_Miniboss" is a noise-prefixed sibling that would win the
#   alphabetical tiebreak in the naive picker.
# - "fallen_shaman_Dyn" carries a noise suffix and must be dropped if a
#   cleaner sibling exists.
_ACTORS = {
    "100": "fallen_shaman",
    "101": "fallen_shaman_lightning",
    "102": "BSK_Miniboss",
    "103": "fallen_shaman_Dyn",
    # An actor referenced by NoActorApp, with no StringList — must not
    # contribute a label.
    "104": "ghost_only_actor",
    # A bare actor that matches a different appearance exactly (modulo
    # casing) — covers the case-insensitive lookup path.
    "105": "SoloHero",
}

# Appearances (CoreTOC group 9): id → stem.
_APPEARANCES = {
    "200": "FallenShaman",
    "201": "NoActorApp",
    "202": "Solohero",          # lowercased to differ from actor case
    "203": "OrphanApp",         # no incoming refs at all
}

# Reverse references: appearance id → [actor ids that reference it].
_INCOMING = {
    "200": [100, 101, 102, 103],
    "201": [104],
    "202": [105],
    # "203" (OrphanApp) intentionally absent.
}

# Actor name → Name string in Actor_<name>.stl.json.
_NAMES = {
    "fallen_shaman": "Fallen Shaman",
    "fallen_shaman_lightning": "Vile Shaman",
    "BSK_Miniboss": "Ank'ton",
    "fallen_shaman_Dyn": "Fallen Shaman",
    # ghost_only_actor (104) intentionally has no StringList — appearances
    # whose only candidate is unnamed must return None.
    "SoloHero": "Solo Hero",
}


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture()
def d4data(tmp_path: Path) -> Path:
    """A minimal synthetic d4data ``json/`` tree."""
    base = tmp_path / "json"

    _write_json(
        base / "base" / "CoreTOC.dat.json",
        {"1": _ACTORS, "9": _APPEARANCES},
    )
    _write_json(base / "incomingSnoReferences.json", _INCOMING)

    # _build_index doesn't read .app.json contents — but the source
    # mtime probe stats the directory, so it must exist.
    appearance = base / "base" / "meta" / "Appearance"
    appearance.mkdir(parents=True)
    for stem in _APPEARANCES.values():
        (appearance / f"{stem}.app.json").write_text("{}", encoding="utf-8")

    stringlist = base / "enUS_Text" / "meta" / "StringList"
    stringlist.mkdir(parents=True)
    for actor_name, display_name in _NAMES.items():
        _write_json(
            stringlist / f"Actor_{actor_name}.stl.json",
            {"arStrings": [{"szLabel": "Name", "szText": display_name}]},
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


def test_canon_strips_case_and_underscores() -> None:
    """The CamelCase appearance ↔ snake_case actor join requires both."""
    assert _canon("FallenShaman") == _canon("fallen_shaman") == "fallenshaman"
    assert _canon("BSK_Miniboss") == "bskminiboss"
    assert _canon("") == ""


def test_pick_actor_drops_noise_suffix_when_clean_sibling_exists() -> None:
    """``_Dyn`` is rejected when a cleaner candidate is available."""
    chosen = _pick_actor(
        "FallenShaman",
        ["fallen_shaman_Dyn", "fallen_shaman"],
        {"fallen_shaman_Dyn": "Decoy", "fallen_shaman": "Fallen Shaman"},
    )
    assert chosen == ("fallen_shaman", "Fallen Shaman")


def test_pick_actor_keeps_only_noise_when_nothing_clean_remains() -> None:
    """If every candidate is noise-tagged, fall back to those candidates."""
    chosen = _pick_actor(
        "Foo",
        ["foo_Dyn", "foo_Spawner"],
        {"foo_Dyn": "Foo Decoy"},
    )
    assert chosen == ("foo_Dyn", "Foo Decoy")


def test_pick_actor_prefers_normalised_exact_match() -> None:
    """``fallen_shaman`` wins over alphabetically-earlier ``BSK_Miniboss``."""
    chosen = _pick_actor(
        "FallenShaman",
        ["BSK_Miniboss", "fallen_shaman", "fallen_shaman_lightning"],
        {
            "BSK_Miniboss": "Ank'ton",
            "fallen_shaman": "Fallen Shaman",
            "fallen_shaman_lightning": "Vile Shaman",
        },
    )
    assert chosen == ("fallen_shaman", "Fallen Shaman")


def test_pick_actor_drops_unnamed_candidates() -> None:
    """A candidate with no StringList Name can't win even on exact match."""
    chosen = _pick_actor(
        "GhostApp",
        ["ghost_only_actor"],
        {},  # no name for the only candidate
    )
    assert chosen is None


# ──────────────────────────────────────────────────────────────────────────────
# Build + resolution
# ──────────────────────────────────────────────────────────────────────────────


def test_build_index_resolves_clean_actor(d4data: Path) -> None:
    """Both the noise-suffix drop and the normalised-match preference work."""
    clear_cache()
    _warm(d4data)
    info = get_display_info(d4data, "FallenShaman")
    assert info is not None
    assert info.display_name == "Fallen Shaman"
    assert info.actor_name == "fallen_shaman"


def test_get_display_name_accepts_full_path_and_filename(d4data: Path) -> None:
    """Full SNO path, bare filename, lowercase stem all hit the same entry."""
    clear_cache()
    _warm(d4data)
    expected = "Fallen Shaman"
    assert get_display_name(d4data, "FallenShaman") == expected
    assert get_display_name(d4data, "FallenShaman.app") == expected
    assert get_display_name(d4data, "fallenshaman") == expected
    assert get_display_name(
        d4data, "base/meta/Appearance/FallenShaman.app",
    ) == expected


def test_unresolved_returns_none(d4data: Path) -> None:
    """An appearance with no usable actor candidate returns ``None``."""
    clear_cache()
    _warm(d4data)
    # NoActorApp's only actor has no StringList.
    assert get_display_name(d4data, "NoActorApp") is None
    # OrphanApp has no incoming refs at all.
    assert get_display_name(d4data, "OrphanApp") is None
    # A wholly unknown stem returns None too — caller falls back to stem.
    assert get_display_name(d4data, "totally_not_a_real_stem") is None


def test_get_display_name_no_index_returns_none(d4data: Path) -> None:
    """With no cached index, lookup never triggers a build — returns None."""
    clear_cache()
    assert get_cached_index(d4data) is None
    assert get_display_name(d4data, "FallenShaman") is None


# ──────────────────────────────────────────────────────────────────────────────
# Disk cache
# ──────────────────────────────────────────────────────────────────────────────


def test_disk_cache_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, d4data: Path,
) -> None:
    """save → load reproduces every ActorDisplayInfo field exactly."""
    clear_cache()
    cache_path = tmp_path / "actor_cache.json"
    monkeypatch.setattr(
        "d4extract.gui.actor_lookup._actor_index_cache_path",
        lambda p: cache_path,
    )

    index = _build_index(d4data)
    _save_cache_to_disk(d4data, index)

    # Drop the in-memory cache so the load path actually exercises disk.
    clear_cache()

    loaded = _load_cache_from_disk(d4data)
    assert loaded is not None
    assert set(loaded) == set(index)
    for stem, info in index.items():
        back = loaded[stem]
        assert back.display_name == info.display_name
        assert back.actor_name == info.actor_name
        assert back.stem == info.stem


def test_disk_cache_stale_mtimes_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, d4data: Path,
) -> None:
    """A cache whose source mtimes don't match live d4data is discarded."""
    clear_cache()
    cache_path = tmp_path / "actor_cache.json"
    monkeypatch.setattr(
        "d4extract.gui.actor_lookup._actor_index_cache_path",
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
    cache_path = tmp_path / "actor_cache.json"
    monkeypatch.setattr(
        "d4extract.gui.actor_lookup._actor_index_cache_path",
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
        "d4extract.gui.actor_lookup._actor_index_cache_path",
        lambda p: tmp_path / "actor_cache.json",
    )
    built = get_or_build_index(d4data)
    assert get_cached_index(d4data) is built
    clear_cache()
