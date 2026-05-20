"""Tests for the shared payload override resolver."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from d4extract.casc import payload_resolver
from d4extract.casc.payload_resolver import (
    clear_cache,
    resolve_payload_path,
)


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Each test gets a fresh in-memory cache."""
    clear_cache()
    yield
    clear_cache()


def _write_mapping(d4data_root: Path, payload: dict[str, str]) -> Path:
    """Place a CoreTOCSharedPayloadsMapping.dat.json under ``d4data_root``."""
    base_dir = d4data_root / "base"
    base_dir.mkdir(parents=True, exist_ok=True)
    mapping_file = base_dir / "CoreTOCSharedPayloadsMapping.dat.json"
    mapping_file.write_text(json.dumps(payload), encoding="utf-8")
    return mapping_file


class TestResolvePayloadPath:
    def test_hit_returns_aliased_path(self, tmp_path: Path):
        """Stem with an override returns the aliased payload path."""
        d4data = tmp_path / "json"
        _write_mapping(d4data, {
            "base/payload/Appearance/druid_windShear_motionBlurActor_inverseSpin.app":
                "base/payload/Appearance/druid_windShear_motionBlurActor.app",
        })

        result = resolve_payload_path(
            "druid_windShear_motionBlurActor_inverseSpin", d4data,
        )

        assert result == "base/payload/Appearance/druid_windShear_motionBlurActor.app"

    def test_miss_returns_none(self, tmp_path: Path):
        """A stem not present in the mapping returns None."""
        d4data = tmp_path / "json"
        _write_mapping(d4data, {
            "base/payload/Appearance/some_other_model.app":
                "base/payload/Appearance/another_model.app",
        })

        assert resolve_payload_path("Sorc_FemaleA", d4data) is None

    def test_key_construction(self, tmp_path: Path):
        """Lookup key must be exactly base/payload/Appearance/<stem>.app.

        Texture/Anim entries with the same stem must NOT match — the
        resolver is Appearance-only.
        """
        d4data = tmp_path / "json"
        _write_mapping(d4data, {
            # Right prefix, right stem -> hit
            "base/payload/Appearance/foo.app":
                "base/payload/Appearance/bar.app",
            # Wrong prefix, same stem -> must be ignored
            "base/payload/Texture/foo.tex":
                "base/payload/Texture/baz.tex",
            "base/payload/Anim/foo.ani":
                "base/payload/Anim/qux.ani",
        })

        assert resolve_payload_path("foo", d4data) == "base/payload/Appearance/bar.app"

    def test_empty_stem_returns_none(self, tmp_path: Path):
        d4data = tmp_path / "json"
        _write_mapping(d4data, {
            "base/payload/Appearance/foo.app":
                "base/payload/Appearance/bar.app",
        })

        assert resolve_payload_path("", d4data) is None

    def test_missing_mapping_file_returns_none(self, tmp_path: Path):
        """A d4data root without the mapping file is non-fatal."""
        d4data = tmp_path / "json"
        d4data.mkdir()
        # Note: no mapping file written.

        assert resolve_payload_path("foo", d4data) is None

    def test_malformed_mapping_falls_back_to_empty(self, tmp_path: Path):
        """A non-dict JSON payload is treated as an empty mapping."""
        d4data = tmp_path / "json"
        base_dir = d4data / "base"
        base_dir.mkdir(parents=True)
        (base_dir / "CoreTOCSharedPayloadsMapping.dat.json").write_text(
            json.dumps(["not", "a", "dict"]), encoding="utf-8",
        )

        assert resolve_payload_path("foo", d4data) is None

    def test_only_appearance_entries_are_loaded(self, tmp_path: Path):
        """Texture/Anim entries are filtered out at load time."""
        d4data = tmp_path / "json"
        _write_mapping(d4data, {
            "base/payload/Appearance/a.app": "base/payload/Appearance/b.app",
            "base/payload/Texture/c.tex": "base/payload/Texture/d.tex",
            "base/payload/Anim/e.ani": "base/payload/Anim/f.ani",
        })

        # Hit the cache loader once via a successful lookup.
        resolve_payload_path("a", d4data)
        cached = payload_resolver._CACHE[(d4data.resolve(), "Appearance")]

        assert set(cached.keys()) == {"base/payload/Appearance/a.app"}

    def test_mapping_is_cached_after_first_load(self, tmp_path: Path):
        """The JSON file is read once per d4data root, then cached."""
        d4data = tmp_path / "json"
        mapping_file = _write_mapping(d4data, {
            "base/payload/Appearance/foo.app":
                "base/payload/Appearance/bar.app",
        })

        # First call loads the file.
        assert resolve_payload_path("foo", d4data) == \
            "base/payload/Appearance/bar.app"

        # Mutate the file on disk and confirm subsequent calls return the
        # cached value, not the new content.
        mapping_file.write_text(json.dumps({
            "base/payload/Appearance/foo.app":
                "base/payload/Appearance/CHANGED.app",
        }), encoding="utf-8")

        assert resolve_payload_path("foo", d4data) == \
            "base/payload/Appearance/bar.app"

    def test_cache_keyed_by_resolved_path(self, tmp_path: Path):
        """Different relative spellings of the same dir share a cache slot."""
        d4data = tmp_path / "json"
        _write_mapping(d4data, {
            "base/payload/Appearance/foo.app":
                "base/payload/Appearance/bar.app",
        })

        # Prime the cache via an unresolved (but equivalent) path.
        equivalent = d4data / "."
        assert resolve_payload_path("foo", equivalent) == \
            "base/payload/Appearance/bar.app"

        # Cache should be keyed by the *resolved* form.
        assert (d4data.resolve(), "Appearance") in payload_resolver._CACHE


class TestResolveAnimPayloadPath:
    """The ``sno_group="Anim"`` branch resolves animation aliases."""

    def test_anim_hit_returns_aliased_path(self, tmp_path: Path):
        """A known aliased animation resolves to its real payload path."""
        d4data = tmp_path / "json"
        _write_mapping(d4data, {
            "base/payload/Anim/morlu_swarmer_death_holy.ani":
                "base/payload/Anim/spider_adult_reac_death_crushed.ani",
        })

        result = resolve_payload_path(
            "morlu_swarmer_death_holy", d4data, sno_group="Anim",
        )

        assert result == \
            "base/payload/Anim/spider_adult_reac_death_crushed.ani"

    def test_anim_lookup_ignores_appearance_entries(self, tmp_path: Path):
        """An Anim-group lookup must not match Appearance entries."""
        d4data = tmp_path / "json"
        _write_mapping(d4data, {
            "base/payload/Appearance/foo.app":
                "base/payload/Appearance/bar.app",
            "base/payload/Anim/foo.ani":
                "base/payload/Anim/qux.ani",
        })

        anim = resolve_payload_path("foo", d4data, sno_group="Anim")
        app = resolve_payload_path("foo", d4data, sno_group="Appearance")

        assert anim == "base/payload/Anim/qux.ani"
        assert app == "base/payload/Appearance/bar.app"

    def test_anim_and_appearance_caches_are_independent(self, tmp_path: Path):
        """Each (root, group) pair gets its own cache slot."""
        d4data = tmp_path / "json"
        _write_mapping(d4data, {
            "base/payload/Appearance/foo.app":
                "base/payload/Appearance/bar.app",
            "base/payload/Anim/foo.ani":
                "base/payload/Anim/qux.ani",
        })

        resolve_payload_path("foo", d4data, sno_group="Appearance")
        resolve_payload_path("foo", d4data, sno_group="Anim")

        root = d4data.resolve()
        assert (root, "Appearance") in payload_resolver._CACHE
        assert (root, "Anim") in payload_resolver._CACHE
        assert set(payload_resolver._CACHE[(root, "Appearance")]) == {
            "base/payload/Appearance/foo.app",
        }
        assert set(payload_resolver._CACHE[(root, "Anim")]) == {
            "base/payload/Anim/foo.ani",
        }

    def test_unknown_sno_group_raises(self, tmp_path: Path):
        """Bogus sno_group values fail loudly rather than silently miss."""
        d4data = tmp_path / "json"
        _write_mapping(d4data, {
            "base/payload/Appearance/foo.app":
                "base/payload/Appearance/bar.app",
        })

        with pytest.raises(ValueError):
            resolve_payload_path("foo", d4data, sno_group="Texture")
