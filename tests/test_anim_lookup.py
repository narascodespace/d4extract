"""Tests for the three-layer animation index (anim_lookup)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from d4extract.gui.anim_lookup import (
    AnimationInfo,
    _INDEX_CACHE,
    _INDEX_LOCK,
    _SCHEMA_VERSION,
    _build_index,
    _load_cache_from_disk,
    _read_meta_summary,
    _save_cache_to_disk,
    clear_cache,
    discover_animations,
    discover_by_prefix,
    is_player_anim,
    prefix_glob_animations,
    union_genders,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


def _make_ani_json(
    path: Path,
    appearance: str,
    frame_counts: list[int] | None = None,
    frame_rate: float = 30.0,
    compression: int = 0,
) -> None:
    """Write a minimal .ani.json fixture to *path*."""
    if frame_counts is None:
        frame_counts = [60]
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "snoAppearance": {"name": appearance},
        "ptPermutations": [
            {
                "nKeyframeCount": fc,
                "flFrameRate": frame_rate,
                "flCompression": compression,
            }
            for fc in frame_counts
        ],
    }
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture()
def anim_dir(tmp_path: Path) -> Path:
    """A synthetic d4data tree with a handful of .ani.json files."""
    base = tmp_path / "json"
    anim = base / "base" / "meta" / "Anim"
    anim.mkdir(parents=True)

    files = [
        # barM appearances
        ("barM_HTH_nav_idle.ani.json", "barM_base00", [90], 30.0, 0),
        ("barM_HTH_nav_run.ani.json", "barM_base00", [45], 30.0, 3),
        ("barM_2HT_atk_heavy.ani.json", "barM_combat00", [120], 30.0, 0),
        # necM appearance — different prefix
        ("necM_summon_idle.ani.json", "necM_base00", [80], 30.0, 0),
        # shared/generic that doesn't start with barM_
        ("ambient_shared_rest.ani.json", "barM_base00", [200], 30.0, 0),
    ]
    for fname, appearance, fc, fr, comp in files:
        _make_ani_json(anim / fname, appearance, fc, fr, comp)

    return base  # caller uses this as d4data_path


# ──────────────────────────────────────────────────────────────────────────────
# Layer 2: prefix_glob_animations
# ──────────────────────────────────────────────────────────────────────────────


def test_prefix_glob_returns_matching_subset(anim_dir: Path) -> None:
    """prefix_glob_animations must return all files whose prefix matches AND
    whose snoAppearance.name equals the requested appearance."""
    results = prefix_glob_animations("barM_base00", anim_dir)
    names = {r.name for r in results}
    # These two start with "barM_" and target barM_base00 — expect them.
    assert "barM_HTH_nav_idle" in names
    assert "barM_HTH_nav_run" in names
    # barM_2HT starts with "barM_" but targets barM_combat00 — must be absent.
    assert "barM_2HT_atk_heavy" not in names
    # necM doesn't start with "barM_" — absent.
    assert "necM_summon_idle" not in names
    # ambient_shared starts with "ambient_" not "barM_" — absent (Layer 3 gap).
    assert "ambient_shared_rest" not in names


def test_prefix_glob_result_is_sorted(anim_dir: Path) -> None:
    results = prefix_glob_animations("barM_base00", anim_dir)
    names = [r.name for r in results]
    assert names == sorted(names, key=str.lower)


def test_prefix_glob_empty_appearance(anim_dir: Path) -> None:
    assert prefix_glob_animations("", anim_dir) == []


def test_prefix_glob_unknown_appearance(anim_dir: Path) -> None:
    assert prefix_glob_animations("zzz_unknown00", anim_dir) == []


# ──────────────────────────────────────────────────────────────────────────────
# Layer 1: on-disk cache header validation
# ──────────────────────────────────────────────────────────────────────────────


def _write_raw_cache(cache_path: Path, data: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data), encoding="utf-8")


def test_load_cache_stale_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache with a wrong anim_dir_mtime must be discarded."""
    d4data = tmp_path / "d4data"
    anim_dir = d4data / "base" / "meta" / "Anim"
    anim_dir.mkdir(parents=True)

    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(
        "d4extract.gui.anim_lookup._anim_index_cache_path",
        lambda p: cache_path,
    )

    live_mtime = anim_dir.stat().st_mtime
    wrong_mtime = live_mtime - 9999.0

    _write_raw_cache(cache_path, {
        "schema_version": _SCHEMA_VERSION,
        "d4data_path": str(d4data),
        "file_count": 0,
        "anim_dir_mtime": wrong_mtime,
        "by_appearance": {},
    })

    assert _load_cache_from_disk(d4data) is None


def test_load_cache_wrong_schema_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d4data = tmp_path / "d4data"
    anim_dir = d4data / "base" / "meta" / "Anim"
    anim_dir.mkdir(parents=True)

    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(
        "d4extract.gui.anim_lookup._anim_index_cache_path",
        lambda p: cache_path,
    )

    _write_raw_cache(cache_path, {
        "schema_version": _SCHEMA_VERSION + 99,
        "d4data_path": str(d4data),
        "file_count": 0,
        "anim_dir_mtime": anim_dir.stat().st_mtime,
        "by_appearance": {},
    })

    assert _load_cache_from_disk(d4data) is None


def test_save_and_load_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anim_dir: Path) -> None:
    """save → load roundtrip must reproduce all AnimationInfo fields exactly."""
    clear_cache()

    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(
        "d4extract.gui.anim_lookup._anim_index_cache_path",
        lambda p: cache_path,
    )

    index = _build_index(anim_dir)
    _save_cache_to_disk(anim_dir, index)

    loaded = _load_cache_from_disk(anim_dir)
    assert loaded is not None

    for appearance, infos in index.items():
        loaded_infos = loaded[appearance]
        assert len(loaded_infos) == len(infos)
        for orig, back in zip(infos, loaded_infos):
            assert orig.name == back.name
            assert orig.appearance_name == back.appearance_name
            assert orig.permutation_count == back.permutation_count
            assert orig.frame_counts == back.frame_counts
            assert orig.frame_rate == back.frame_rate
            assert orig.compression == back.compression
            assert Path(back.ani_meta_path).is_absolute()


def test_save_writes_file_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anim_dir: Path) -> None:
    """The saved cache header must contain the correct file_count."""
    clear_cache()

    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(
        "d4extract.gui.anim_lookup._anim_index_cache_path",
        lambda p: cache_path,
    )

    index = _build_index(anim_dir)
    _save_cache_to_disk(anim_dir, index)

    with cache_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    anim = anim_dir / "base" / "meta" / "Anim"
    actual_count = sum(1 for e in os.scandir(anim) if e.name.endswith(".ani.json"))
    assert data["file_count"] == actual_count


# ──────────────────────────────────────────────────────────────────────────────
# Layer 3: full build parity with the original behaviour
# ──────────────────────────────────────────────────────────────────────────────


def test_full_build_matches_discover_animations(anim_dir: Path) -> None:
    """Full build must return the same set as discover_animations (old API)."""
    clear_cache()
    direct = discover_animations("barM_base00", anim_dir)
    clear_cache()
    via_build = _build_index(anim_dir).get("barM_base00", [])
    assert {i.name for i in direct} == {i.name for i in via_build}


def test_full_build_includes_shared_prefix(anim_dir: Path) -> None:
    """The full build must include ambient_shared_rest (no barM_ prefix)."""
    clear_cache()
    index = _build_index(anim_dir)
    names = {i.name for i in index.get("barM_base00", [])}
    assert "ambient_shared_rest" in names


def test_full_build_result_is_sorted(anim_dir: Path) -> None:
    """Each appearance list must be alphabetically sorted (case-insensitive)."""
    clear_cache()
    index = _build_index(anim_dir)
    for appearance, infos in index.items():
        names = [i.name for i in infos]
        assert names == sorted(names, key=str.lower), (
            f"Unsorted list for appearance {appearance!r}"
        )


def test_full_build_vs_prefix_glob_subset(anim_dir: Path) -> None:
    """Every result from prefix_glob must also appear in the full build."""
    clear_cache()
    fast = prefix_glob_animations("barM_base00", anim_dir)
    full = _build_index(anim_dir).get("barM_base00", [])
    fast_names = {i.name for i in fast}
    full_names = {i.name for i in full}
    assert fast_names.issubset(full_names)


# ──────────────────────────────────────────────────────────────────────────────
# discover_by_prefix — class+gender prefix union (Character Builder lookup)
# ──────────────────────────────────────────────────────────────────────────────


def _warm_cache(d4data_path: Path) -> None:
    """Build the index for *d4data_path* and install it into _INDEX_CACHE.

    discover_by_prefix reads the in-memory cache via get_cached_index, so
    the cache must be warm — done here without touching the disk cache.
    """
    key = Path(d4data_path).resolve()
    with _INDEX_LOCK:
        _INDEX_CACHE[key] = _build_index(d4data_path)


def test_discover_by_prefix_unions_appearances(anim_dir: Path) -> None:
    """All barM_* appearances must be unioned under the 'barM' prefix."""
    clear_cache()
    _warm_cache(anim_dir)
    results = discover_by_prefix(anim_dir, "barM")
    names = {r.name for r in results}
    # barM_base00 entries.
    assert "barM_HTH_nav_idle" in names
    assert "barM_HTH_nav_run" in names
    # barM_combat00 entry — unioned in despite its different appearance.
    assert "barM_2HT_atk_heavy" in names
    # ambient_shared_rest targets barM_base00, so it rides along.
    assert "ambient_shared_rest" in names
    # necM appearance must not leak into a barM lookup.
    assert "necM_summon_idle" not in names
    clear_cache()


def test_discover_by_prefix_excludes_other_classes(anim_dir: Path) -> None:
    """A 'necM' prefix must return only necM appearances."""
    clear_cache()
    _warm_cache(anim_dir)
    results = discover_by_prefix(anim_dir, "necM")
    assert {r.name for r in results} == {"necM_summon_idle"}
    clear_cache()


def test_discover_by_prefix_empty_prefix(anim_dir: Path) -> None:
    """An empty prefix returns nothing — never union the whole index."""
    clear_cache()
    _warm_cache(anim_dir)
    assert discover_by_prefix(anim_dir, "") == []
    clear_cache()


def test_discover_by_prefix_uncached_returns_empty(anim_dir: Path) -> None:
    """With no cached index, discovery returns an empty list (no build)."""
    clear_cache()
    assert discover_by_prefix(anim_dir, "barM") == []


def test_discover_by_prefix_exact_match(tmp_path: Path) -> None:
    """An appearance keyed exactly as the prefix (no '_') is matched too."""
    clear_cache()
    fake = (tmp_path / "exact").resolve()
    info = AnimationInfo(
        name="barM_bare", ani_meta_path=tmp_path / "barM_bare.ani.json",
        appearance_name="barM", permutation_count=1,
        frame_counts=(20,), frame_rate=30.0, compression=0,
    )
    with _INDEX_LOCK:
        _INDEX_CACHE[fake] = {"barM": [info]}
    assert [r.name for r in discover_by_prefix(fake, "barM")] == ["barM_bare"]
    clear_cache()


def test_discover_by_prefix_dedupes(tmp_path: Path) -> None:
    """Duplicate (name, permutation_count) across appearances → first wins."""
    clear_cache()
    fake = (tmp_path / "dedup").resolve()
    dup_a = AnimationInfo(
        name="barM_shared", ani_meta_path=tmp_path / "a.ani.json",
        appearance_name="barM_base00", permutation_count=2,
        frame_counts=(30, 40), frame_rate=30.0, compression=0,
    )
    dup_b = AnimationInfo(
        name="barM_shared", ani_meta_path=tmp_path / "b.ani.json",
        appearance_name="barM_combat00", permutation_count=2,
        frame_counts=(30, 40), frame_rate=30.0, compression=0,
    )
    uniq = AnimationInfo(
        name="barM_unique", ani_meta_path=tmp_path / "c.ani.json",
        appearance_name="barM_combat00", permutation_count=1,
        frame_counts=(50,), frame_rate=30.0, compression=0,
    )
    with _INDEX_LOCK:
        _INDEX_CACHE[fake] = {
            "barM_base00": [dup_a],
            "barM_combat00": [dup_b, uniq],
        }
    results = discover_by_prefix(fake, "barM")
    shared = [r for r in results if r.name == "barM_shared"]
    assert len(shared) == 1
    assert shared[0] is dup_a  # first occurrence wins
    assert len(results) == 2   # barM_shared + barM_unique
    clear_cache()


# ──────────────────────────────────────────────────────────────────────────────
# is_player_anim — Character Builder discovery filter
# ──────────────────────────────────────────────────────────────────────────────


def test_is_player_anim_keeps_player_clips() -> None:
    assert is_player_anim("barM_HTH_nav_idle")
    assert is_player_anim("barM_2HM_attk_whirlwind")
    assert is_player_anim("barM_mount_horse_nav_gallop")


def test_is_player_anim_drops_igc_clips() -> None:
    assert not is_player_anim("IGC_CBH_t3barbarianm_1010")
    assert not is_player_anim("IGC_CPD_t3barbarianm_1050")
    assert not is_player_anim("igc_lowercase_variant_anim")


def test_is_player_anim_keeps_non_igc_non_player_for_now() -> None:
    # NPC, conv, pvp, and seasonal prefixes are *not* in the current
    # exclusion list. If we later widen the filter, these tests need to
    # be updated alongside _BUILDER_EXCLUDED_PREFIXES — they're a
    # tripwire to make the widening intentional rather than accidental.
    assert is_player_anim("NPC_Mercenary_Crone_2HM_attk_hammeroftheancients")
    assert is_player_anim("npc_barM_reac_death")
    assert is_player_anim("conv_haw_chc_barM")
    assert is_player_anim("pvp_openworld_turnhostile_cast_NEWBarbM_STF")
    assert is_player_anim("S02_BloodSeeker_barM_2HM_attk_bash")


# ──────────────────────────────────────────────────────────────────────────────
# union_genders — Character Builder both-gender discovery merge
# ──────────────────────────────────────────────────────────────────────────────


def _anim_info(name: str) -> AnimationInfo:
    """Minimal AnimationInfo for union_genders tests — only ``name`` matters.

    union_genders dedups purely on the (prefix-stripped) name, so the
    other fields are filled with harmless dummies.
    """
    return AnimationInfo(
        name=name,
        ani_meta_path=Path(f"{name}.ani.json"),
        appearance_name="",
        permutation_count=1,
        frame_counts=(1,),
        frame_rate=30.0,
        compression=3,
    )


def test_union_genders_no_collisions_keeps_everything() -> None:
    primary = [_anim_info("barF_combat_finisher")]
    other = [_anim_info("barM_HTH_nav_idle")]
    merged = union_genders(
        primary, other,
        primary_prefix="barF", other_prefix="barM",
    )
    assert len(merged) == 2
    assert {info.name for info in merged} == {
        "barF_combat_finisher", "barM_HTH_nav_idle",
    }


def test_union_genders_collision_prefers_primary() -> None:
    primary = [_anim_info("barF_HTH_nav_idle")]
    other = [_anim_info("barM_HTH_nav_idle")]
    merged = union_genders(
        primary, other,
        primary_prefix="barF", other_prefix="barM",
    )
    assert len(merged) == 1
    assert merged[0].name == "barF_HTH_nav_idle"


def test_union_genders_case_insensitive_collision() -> None:
    # Real data: emote files are inconsistently capitalized.
    primary = [_anim_info("BarF_emotes_032_stor")]
    other = [_anim_info("BarM_emotes_032_stor")]
    merged = union_genders(
        primary, other,
        primary_prefix="barF", other_prefix="barM",
    )
    assert len(merged) == 1
    assert merged[0].name == "BarF_emotes_032_stor"


def test_union_genders_other_unique_keeps_other() -> None:
    primary = [_anim_info("warF_some_female_only_clip")]
    other = [
        _anim_info("warM_HTH_nav_idle"),
        _anim_info("warM_2HS_attk_weaponAttack"),
    ]
    merged = union_genders(
        primary, other,
        primary_prefix="warF", other_prefix="warM",
    )
    assert len(merged) == 3
    # Ordering: primary first, then other-unique.
    assert merged[0].name == "warF_some_female_only_clip"
    assert {info.name for info in merged[1:]} == {
        "warM_HTH_nav_idle", "warM_2HS_attk_weaponAttack",
    }


def test_union_genders_conv_files_do_not_dedup() -> None:
    # Conv files have the gender at the END of the name, not the start.
    # By design, v1 doesn't dedup them — they'll appear twice.
    primary = [_anim_info("Conv_Hell_CBE_paladinf")]
    other = [_anim_info("Conv_Hell_CBE_paladinm")]
    merged = union_genders(
        primary, other,
        primary_prefix="palF", other_prefix="palM",
    )
    assert len(merged) == 2  # both kept


def test_union_genders_empty_primary_keeps_everything_in_other() -> None:
    primary: list[AnimationInfo] = []
    other = [_anim_info("warM_HTH_nav_idle")]
    merged = union_genders(
        primary, other,
        primary_prefix="warF", other_prefix="warM",
    )
    assert len(merged) == 1
    assert merged[0].name == "warM_HTH_nav_idle"


def test_union_genders_empty_other_keeps_primary() -> None:
    primary = [_anim_info("barF_HTH_nav_idle")]
    other: list[AnimationInfo] = []
    merged = union_genders(
        primary, other,
        primary_prefix="barF", other_prefix="barM",
    )
    assert len(merged) == 1
