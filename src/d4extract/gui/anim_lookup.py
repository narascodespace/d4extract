"""Discover animations targeting a given appearance in d4data.

The d4data ``json/base/meta/Anim/`` directory holds ~45,000 ``.ani.json``
files.  Each one's ``snoAppearance`` field links it back to the .app
appearance whose skeleton it animates.  We need a fast reverse lookup —
"give me every animation that targets ``barM_base00``" — and the
straightforward way is to scan once and cache the result.

Three-layer strategy for fast startup
--------------------------------------
Layer 1 — on-disk persisted JSON index (~1-2 MB).  Loaded in milliseconds
    on warm launches; invalidated by a change in the Anim/ directory mtime.
Layer 2 — prefix-glob fallback.  While the full build hasn't yet completed,
    glob ``{prefix}*.ani.json`` (a few hundred files) and return those hits
    immediately so the user sees *something* within ~1 second.
Layer 3 — background full scan.  ``ThreadPoolExecutor`` reads all 45K files
    in parallel; when done the result is written to disk (Layer 1 next time)
    and the in-memory cache is atomically swapped in.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


_ANIM_META_RELDIR = Path("base") / "meta" / "Anim"
_SCHEMA_VERSION = 1
_BATCH_SIZE = 500
_BUILD_WORKERS = 8


@dataclass(frozen=True)
class AnimationInfo:
    """One animation that can drive a given appearance."""

    name: str                       # bare stem, e.g. "barM_HTH_nav_idle"
    ani_meta_path: Path             # absolute path to <name>.ani.json
    appearance_name: str            # snoAppearance.name
    permutation_count: int          # len(ptPermutations)
    frame_counts: tuple[int, ...]   # nKeyframeCount per permutation
    frame_rate: float               # ptPermutations[0].flFrameRate (assume uniform)
    compression: int                # ptPermutations[0].flCompression


_INDEX_CACHE: dict[Path, dict[str, list[AnimationInfo]]] = {}
_INDEX_LOCK = threading.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Layer 1 — on-disk cache helpers
# ──────────────────────────────────────────────────────────────────────────────


def _anim_index_cache_path(d4data_path: Path) -> Path | None:
    """Return the on-disk cache file path, or None if Qt is unavailable."""
    try:
        from PySide6.QtCore import QStandardPaths
        base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
        slug = hashlib.md5(str(d4data_path).encode()).hexdigest()[:12]
        return Path(base) / "d4extract" / f"anim_index_{slug}.json"
    except ImportError:
        return None


def _load_cache_from_disk(
    d4data_path: Path,
) -> dict[str, list[AnimationInfo]] | None:
    """Return the persisted index if it exists and matches the live d4data state."""
    cache_path = _anim_index_cache_path(d4data_path)
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("Cache load failed: %s", exc)
        return None

    if data.get("schema_version") != _SCHEMA_VERSION:
        return None
    if str(data.get("d4data_path")) != str(d4data_path):
        return None

    anim_dir = d4data_path / _ANIM_META_RELDIR
    try:
        live_mtime = anim_dir.stat().st_mtime if anim_dir.is_dir() else 0.0
    except OSError:
        live_mtime = 0.0
    if data.get("anim_dir_mtime") != live_mtime:
        log.info("Animation index cache is stale, will rebuild")
        return None

    by_appearance: dict[str, list[AnimationInfo]] = {}
    for appearance, entries in data.get("by_appearance", {}).items():
        infos: list[AnimationInfo] = []
        for e in entries:
            try:
                infos.append(AnimationInfo(
                    name=e["name"],
                    ani_meta_path=Path(e["ani_meta_path"]),
                    appearance_name=e["appearance_name"],
                    permutation_count=int(e["permutation_count"]),
                    frame_counts=tuple(e["frame_counts"]),
                    frame_rate=float(e["frame_rate"]),
                    compression=int(e["compression"]),
                ))
            except (KeyError, TypeError, ValueError):
                log.debug("Corrupt cache entry; discarding cache")
                return None
        by_appearance[appearance] = infos

    log.info(
        "Loaded animation index from disk cache: %d appearances", len(by_appearance)
    )
    return by_appearance


def _save_cache_to_disk(
    d4data_path: Path,
    index: dict[str, list[AnimationInfo]],
) -> None:
    """Persist the index atomically to the on-disk cache."""
    cache_path = _anim_index_cache_path(d4data_path)
    if cache_path is None:
        return
    anim_dir = d4data_path / _ANIM_META_RELDIR
    try:
        live_mtime = anim_dir.stat().st_mtime if anim_dir.is_dir() else 0.0
        file_count = (
            sum(1 for e in os.scandir(anim_dir) if e.name.endswith(".ani.json"))
            if anim_dir.is_dir()
            else 0
        )
    except OSError:
        live_mtime = 0.0
        file_count = 0

    payload = {
        "schema_version": _SCHEMA_VERSION,
        "d4data_path": str(d4data_path),
        "file_count": file_count,
        "anim_dir_mtime": live_mtime,
        "by_appearance": {
            appearance: [
                {
                    "name": info.name,
                    "ani_meta_path": str(info.ani_meta_path),
                    "appearance_name": info.appearance_name,
                    "permutation_count": info.permutation_count,
                    "frame_counts": list(info.frame_counts),
                    "frame_rate": info.frame_rate,
                    "compression": info.compression,
                }
                for info in infos
            ]
            for appearance, infos in index.items()
        },
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        tmp.replace(cache_path)
        log.info("Animation index cache written: %s", cache_path)
    except OSError as exc:
        log.warning("Failed to write animation index cache: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Core per-file reader (shared by Layer 2 and Layer 3)
# ──────────────────────────────────────────────────────────────────────────────


def _read_meta_summary(path: Path) -> AnimationInfo | None:
    """Read just the bits of an ``.ani.json`` we need for the index.

    Returns ``None`` for files that don't conform — missing
    ``snoAppearance``, missing/empty ``ptPermutations``, or unreadable
    JSON.  The caller treats those as "skip" rather than "fatal".
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("Skipping %s: %s", path, exc)
        return None

    sno = meta.get("snoAppearance")
    if not isinstance(sno, dict):
        return None
    appearance = sno.get("name")
    if not isinstance(appearance, str) or not appearance:
        return None

    perms = meta.get("ptPermutations")
    if not isinstance(perms, list) or not perms:
        return None
    frame_counts: list[int] = []
    frame_rate = 30.0
    compression = 0
    for i, p in enumerate(perms):
        if not isinstance(p, dict):
            continue
        try:
            frame_counts.append(int(p.get("nKeyframeCount", 0)))
        except (TypeError, ValueError):
            frame_counts.append(0)
        if i == 0:
            try:
                frame_rate = float(p.get("flFrameRate", 30.0))
            except (TypeError, ValueError):
                frame_rate = 30.0
            try:
                compression = int(p.get("flCompression", 0))
            except (TypeError, ValueError):
                compression = 0
    if not frame_counts:
        return None

    name = path.name
    if name.endswith(".json"):
        name = name[: -len(".json")]
    if name.endswith(".ani"):
        name = name[: -len(".ani")]

    return AnimationInfo(
        name=name,
        ani_meta_path=path,
        appearance_name=appearance,
        permutation_count=len(frame_counts),
        frame_counts=tuple(frame_counts),
        frame_rate=frame_rate,
        compression=compression,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Layer 3 — full parallel build
# ──────────────────────────────────────────────────────────────────────────────


def _build_index(
    d4data_path: Path,
    *,
    progress_cb=None,
) -> dict[str, list[AnimationInfo]]:
    """Walk ``base/meta/Anim`` and bucket every ``.ani.json`` by appearance.

    Uses :class:`~concurrent.futures.ThreadPoolExecutor` to parallelise file
    reads — the bottleneck is per-file syscall overhead, not CPU, so
    threading gives a ~4-8× speedup on an SSD.
    """
    anim_dir = d4data_path / _ANIM_META_RELDIR
    if not anim_dir.is_dir():
        log.warning("Animation directory not found: %s", anim_dir)
        return {}

    paths = sorted(anim_dir.glob("*.ani.json"))
    total = len(paths)
    if total == 0:
        return {}

    by_appearance: dict[str, list[AnimationInfo]] = {}
    error_count = 0
    processed = 0
    lock = threading.Lock()
    step = max(1, total // 10)

    def _process_batch(batch: list[Path]) -> list[AnimationInfo | None]:
        return [_read_meta_summary(p) for p in batch]

    batches = [paths[i : i + _BATCH_SIZE] for i in range(0, total, _BATCH_SIZE)]

    with ThreadPoolExecutor(max_workers=_BUILD_WORKERS) as executor:
        futures = [executor.submit(_process_batch, b) for b in batches]
        for fut in as_completed(futures):
            batch_results = fut.result()
            with lock:
                prev = processed
                for info in batch_results:
                    processed += 1
                    if info is None:
                        error_count += 1
                        continue
                    by_appearance.setdefault(info.appearance_name, []).append(info)
                current = processed
            if progress_cb is not None and (
                prev // step < current // step or current >= total
            ):
                progress_cb(min(current, total), total)

    for items in by_appearance.values():
        items.sort(key=lambda a: a.name.lower())

    log.info(
        "Animation index: %d files, %d skipped, %d distinct appearances",
        total,
        error_count,
        len(by_appearance),
    )
    return by_appearance


# ──────────────────────────────────────────────────────────────────────────────
# Layer 2 — prefix-glob fast path
# ──────────────────────────────────────────────────────────────────────────────


def prefix_glob_animations(
    appearance_name: str,
    d4data_path: Path,
) -> list[AnimationInfo]:
    """Fast subset via filename prefix glob (Layer 2).

    Uses the leading token of *appearance_name* (e.g. ``barM_`` from
    ``barM_base00``) to glob a few hundred files instead of all 45K.
    Each hit is opened and its ``snoAppearance.name`` is verified to
    match exactly.  Anims whose filename doesn't share the prefix are
    absent from the result — those arrive when the full build completes.
    """
    if not appearance_name:
        return []
    anim_dir = Path(d4data_path) / _ANIM_META_RELDIR
    if not anim_dir.is_dir():
        return []
    parts = appearance_name.split("_", 1)
    prefix = (parts[0] + "_") if len(parts) > 1 else appearance_name
    results: list[AnimationInfo] = []
    for path in anim_dir.glob(f"{prefix}*.ani.json"):
        info = _read_meta_summary(path)
        if info is not None and info.appearance_name == appearance_name:
            results.append(info)
    results.sort(key=lambda a: a.name.lower())
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def get_cached_index(
    d4data_path: Path,
) -> dict[str, list[AnimationInfo]] | None:
    """Return the in-memory index if already populated, else ``None``."""
    key = Path(d4data_path).resolve()
    with _INDEX_LOCK:
        return _INDEX_CACHE.get(key)


def discover_by_prefix(
    d4data_path: Path,
    prefix: str,
) -> list[AnimationInfo]:
    """Union every cached ``AnimationInfo`` whose appearance starts with *prefix*.

    D4 indexes animations by canonical skeleton appearance
    (``barM_base00``, ``barM_combat00``, etc.), and a single
    character's animations are split across several of those
    appearances.  The Character Builder doesn't know which canonical
    appearance(s) a given assembly maps to, so it passes the
    class+gender prefix (e.g. ``"barM"``) and we collect every
    matching entry.

    Returns an empty list if the index isn't cached, *prefix* is
    empty, or nothing matches.  Deduped by ``(name, permutation_count)``;
    the first occurrence wins.
    """
    if not prefix:
        return []
    index = get_cached_index(d4data_path)
    if not index:
        return []
    needle = prefix + "_"
    merged: list[AnimationInfo] = []
    seen: set[tuple[str, int]] = set()
    for key, infos in index.items():
        if key != prefix and not key.startswith(needle):
            continue
        for info in infos:
            dedup_key = (info.name, info.permutation_count)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            merged.append(info)
    return merged


# Animation name prefixes excluded from Character Builder discovery.
#
# IGC (In-Game Cinematic) clips target the player skeleton but ship as
# pre-baked cinematic poses meant to play with a fixed camera — they're
# not motion an assembled character can usefully play back. They also
# happen to use flCompression=1, an undocumented format the decoder
# doesn't implement, so they generate decode failures on every export.
#
# This tuple is intentionally narrow. Widening it (e.g. to also drop
# ``NPC_``/``npc_``, ``conv_``, ``pvp_``, seasonal ``S\d\d_`` prefixes)
# is a one-line change when there's evidence those clips are unwanted
# noise in the builder UX. For now, only IGC clips are filtered — and
# ``test_is_player_anim_keeps_non_igc_non_player_for_now`` is a tripwire
# that must be updated alongside any widening.
_BUILDER_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "IGC_",
    "igc_",
)


def is_player_anim(name: str) -> bool:
    """True if *name* should appear in the Character Builder anim list.

    Currently filters in-game-cinematic (IGC) prefixes; see
    :data:`_BUILDER_EXCLUDED_PREFIXES` for the rationale and for
    guidance on widening the filter.

    The Model Browser does NOT apply this filter — individual file
    inspection is a different UX surface and stays unrestricted.
    """
    return not name.startswith(_BUILDER_EXCLUDED_PREFIXES)


def union_genders(
    primary_infos: list[AnimationInfo],
    other_infos: list[AnimationInfo],
    *,
    primary_prefix: str,
    other_prefix: str,
) -> list[AnimationInfo]:
    """Merge two gender-prefix discovery results, preferring primary on collisions.

    A name "collides" when stripping its discovery prefix yields the same
    suffix (case-insensitive) under both sides. Example: ``barM_HTH_nav_idle``
    in the other-gender list and ``barF_HTH_nav_idle`` in the primary list
    share the suffix ``_HTH_nav_idle`` — the primary version wins.

    The strip is case-insensitive to handle Blizzard's inconsistent
    capitalisation across animation categories (e.g. ``BarF_emotes_032_stor``
    versus lowercase ``barF_HTH_nav_idle``).

    Conv-style cutscene files with the gender marker at the END of the name
    (e.g. ``Conv_Hell_CBE_paladinf`` / ``Conv_Hell_CBE_paladinm``) don't dedup
    under this scheme — both will appear in the merged list. Acceptable
    for v1; can tighten later if it produces dropdown noise.

    *primary_prefix* and *other_prefix* should be the ``[class][gender]``
    strings used for the two :func:`discover_by_prefix` calls (e.g.
    ``"barF"`` and ``"barM"``).

    The Character Builder unions both genders because D4's runtime
    plays the other gender's animation whenever a gender-specific
    override is absent — see ``builder_page._character_prefix_pair``.
    """

    def _canonical(name: str, prefix: str) -> str:
        if name.lower().startswith(prefix.lower()):
            return name[len(prefix):].lower()
        return name.lower()

    primary_keys = {
        _canonical(info.name, primary_prefix) for info in primary_infos
    }
    merged: list[AnimationInfo] = list(primary_infos)
    for info in other_infos:
        if _canonical(info.name, other_prefix) not in primary_keys:
            merged.append(info)
    return merged


def get_or_build_index(
    d4data_path: Path,
    *,
    progress_cb=None,
) -> dict[str, list[AnimationInfo]]:
    """Return the cached index, building it on first call.

    The cache is keyed by the resolved d4data path so switching between
    multiple checkouts gives independent indices.  On first call with a
    warm disk cache, this returns in milliseconds.  On a cold cache it
    builds the full 45K-file index and persists it to disk.
    """
    key = Path(d4data_path).resolve()
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(key)
        if cached is not None:
            return cached

    disk = _load_cache_from_disk(key)
    if disk is not None:
        with _INDEX_LOCK:
            existing = _INDEX_CACHE.get(key)
            if existing is not None:
                return existing
            _INDEX_CACHE[key] = disk
            return disk

    built = _build_index(key, progress_cb=progress_cb)
    _save_cache_to_disk(key, built)
    with _INDEX_LOCK:
        existing = _INDEX_CACHE.get(key)
        if existing is not None:
            return existing
        _INDEX_CACHE[key] = built
        return built


def discover_animations(
    appearance_name: str,
    d4data_path: Path,
    *,
    progress_cb=None,
) -> list[AnimationInfo]:
    """Return every animation whose ``snoAppearance`` matches the appearance.

    Args:
        appearance_name: Bare model stem (e.g. ``"barM_base00"``).
        d4data_path: Path to the d4data ``json/`` directory.
        progress_cb: Optional ``callable(current, total)`` invoked while
            the index is being built (only fires on the first call per
            d4data path).
    """
    if not appearance_name:
        return []
    index = get_or_build_index(d4data_path, progress_cb=progress_cb)
    return list(index.get(appearance_name, ()))


def clear_cache() -> None:
    """Drop the in-memory index (primarily for tests)."""
    with _INDEX_LOCK:
        _INDEX_CACHE.clear()
