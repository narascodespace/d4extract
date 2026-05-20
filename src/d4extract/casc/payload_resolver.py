"""Resolve shared/aliased payload paths via CoreTOCSharedPayloadsMapping.

About 15% of D4 Appearance entries store their payload under a different
filename than their meta — the engine calls these "shared payloads."
``base/json/CoreTOCSharedPayloadsMapping.dat.json`` is a flat map from
virtual payload paths to the real payload paths the CASC archive holds.
The same file also covers Animations (``base/payload/Anim/*.ani``), where
roughly 7,500 entries reuse another animation's payload.

This module loads that mapping (filtered to one SNO group at a time, since
the file mixes Appearance, Anim and Texture entries) and exposes a lookup
helper that the CASC extraction layer can call when a same-name payload is
missing.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_MAPPING_RELPATH = Path("base") / "CoreTOCSharedPayloadsMapping.dat.json"

# SNO group → (payload-path prefix, file extension). Only groups whose
# payloads ever appear in the shared-payload mapping are listed here.
_GROUPS: dict[str, tuple[str, str]] = {
    "Appearance": ("base/payload/Appearance/", ".app"),
    "Anim": ("base/payload/Anim/", ".ani"),
}

# Cache: (d4data root resolved, sno_group) -> {virtual_path: real_path}.
_CACHE: dict[tuple[Path, str], dict[str, str]] = {}
_CACHE_LOCK = threading.Lock()


def _load_mapping(d4data_path: Path, sno_group: str) -> dict[str, str]:
    """Read the mapping JSON and return only entries for ``sno_group``."""
    prefix, _ext = _GROUPS[sno_group]
    mapping_file = d4data_path / _MAPPING_RELPATH
    if not mapping_file.is_file():
        log.warning(
            "Shared payload mapping not found at %s — overrides disabled",
            mapping_file,
        )
        return {}

    with mapping_file.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        log.warning(
            "Shared payload mapping at %s is not a JSON object; got %s",
            mapping_file, type(raw).__name__,
        )
        return {}

    filtered = {
        k: v for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str)
        and k.startswith(prefix)
    }
    log.debug(
        "Loaded %d %s shared-payload entries from %s",
        len(filtered), sno_group, mapping_file,
    )
    return filtered


def _get_mapping(
    d4data_path: Path, sno_group: str = "Appearance",
) -> dict[str, str]:
    """Return the cached mapping for ``d4data_path`` × ``sno_group``."""
    if sno_group not in _GROUPS:
        raise ValueError(
            f"Unsupported sno_group {sno_group!r}; "
            f"expected one of {sorted(_GROUPS)}"
        )
    key = (d4data_path.resolve(), sno_group)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        mapping = _load_mapping(key[0], sno_group)
        _CACHE[key] = mapping
        return mapping


def resolve_payload_path(
    meta_stem: str, d4data_path: Path,
    sno_group: str = "Appearance",
) -> str | None:
    """Return the real payload CASC path for ``meta_stem`` if aliased.

    Args:
        meta_stem: Bare model/animation name (no extension, no directory)
            — e.g. ``"druid_windShear_motionBlurActor_inverseSpin"`` or
            ``"morlu_swarmer_death_holy"``.
        d4data_path: Path to the d4data ``json/`` directory.
        sno_group: Which SNO group's mapping to consult — ``"Appearance"``
            (default, ``.app``) or ``"Anim"`` (``.ani``).

    Returns:
        The aliased payload path (e.g.
        ``"base/payload/Appearance/druid_windShear_motionBlurActor.app"``
        or ``"base/payload/Anim/spider_adult_reac_death_crushed.ani"``),
        or *None* when the asset uses a same-name payload and no override
        is needed.
    """
    if not meta_stem:
        return None
    if sno_group not in _GROUPS:
        raise ValueError(
            f"Unsupported sno_group {sno_group!r}; "
            f"expected one of {sorted(_GROUPS)}"
        )
    prefix, ext = _GROUPS[sno_group]
    mapping = _get_mapping(d4data_path, sno_group)
    if not mapping:
        return None
    key = f"{prefix}{meta_stem}{ext}"
    return mapping.get(key)


def clear_cache() -> None:
    """Drop the in-memory mapping cache (primarily for tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()
