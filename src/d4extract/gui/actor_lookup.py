"""Appearance ``.app`` stem → actor display name lookup.

Mirror of :mod:`d4extract.gui.item_lookup`, one chain hop shorter:

    .app stem  →  Appearance SNO ID
               →  candidate Actor SNO refs (from incomingSnoReferences)
               →  Actor name (CoreTOC group 1)
               →  Actor_<name>.stl.json → arStrings["Name"].szText

Covers monsters, NPCs, mercenaries, bosses, named quest characters, and
some props. About 11,940 ``Actor_*.stl.json`` files ship in D4 today, so
the build's parallel StringList scan is roughly the same shape as
``item_lookup``'s.

Multiple actors typically reference the same appearance — e.g. the base
``fallen_shaman`` mesh is the same SNO as ``fallen_shaman_cold``,
``fallen_shaman_unique_DGN_Frac_LostArchives`` ("Panca"), etc. The
disambiguator prefers the actor whose stem normalises (lowercase,
alphanumeric only) to the appearance stem, then the shortest remaining
named actor, alphabetical for determinism. That picks ``fallen_shaman``
over ``BSK_Miniboss_QaraYisuFallen`` for the appearance ``FallenShaman``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_BUILD_WORKERS = 8

# SNO group ids in CoreTOC.dat.json.
_GROUP_ACTOR = "1"
_GROUP_APPEARANCE = "9"

# Suffix tokens that mark an actor as a variant/spawner/projectile clone
# rather than the canonical character. Dropped when a cleaner sibling
# exists. Case-insensitive.
_NOISE_SUFFIXES: tuple[str, ...] = (
    "_dyn",
    "_multistage",
    "_projectile",
    "_clienteffect",
    "_spawner",
    "_arrangement",
    "_sp",
)


@dataclass(frozen=True)
class ActorDisplayInfo:
    """Resolved display data for one appearance ``.app`` stem.

    ``stem`` is the lookup key (lowercased appearance filename without
    directory or ``.app``). ``actor_name`` is the actor whose StringList
    supplied the name — kept as an audit trail for future filters.
    """

    stem: str
    display_name: str
    actor_name: str


# In-memory index cache, keyed by the resolved d4data path. Same shape
# and locking discipline as ``item_lookup._INDEX_CACHE``.
_INDEX_CACHE: dict[Path, dict[str, ActorDisplayInfo]] = {}
_INDEX_LOCK = threading.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# On-disk cache helpers (mirrors item_lookup)
# ──────────────────────────────────────────────────────────────────────────────


def _actor_index_cache_path(d4data_path: Path) -> Path | None:
    """Return the on-disk cache file path, or ``None`` if Qt is absent."""
    try:
        from PySide6.QtCore import QStandardPaths
        base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
        slug = hashlib.md5(str(d4data_path).encode()).hexdigest()[:12]
        return Path(base) / "d4extract" / f"actor_index_{slug}.json"
    except ImportError:
        return None


def _source_paths(d4data_path: Path) -> dict[str, Path]:
    """The four inputs whose mtimes invalidate a cached index."""
    return {
        "coretoc": d4data_path / "base" / "CoreTOC.dat.json",
        "incoming": d4data_path / "incomingSnoReferences.json",
        "appearance": d4data_path / "base" / "meta" / "Appearance",
        "stringlist": d4data_path / "enUS_Text" / "meta" / "StringList",
    }


def _source_mtimes(d4data_path: Path) -> dict[str, float]:
    """Snapshot the mtimes of the index's source files/dirs."""
    out: dict[str, float] = {}
    for key, path in _source_paths(d4data_path).items():
        try:
            out[key] = path.stat().st_mtime
        except OSError:
            out[key] = 0.0
    return out


def _load_cache_from_disk(
    d4data_path: Path,
) -> dict[str, ActorDisplayInfo] | None:
    """Return the persisted index if it exists and matches live d4data."""
    cache_path = _actor_index_cache_path(d4data_path)
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("Actor index cache load failed: %s", exc)
        return None

    if data.get("schema_version") != _SCHEMA_VERSION:
        return None
    if str(data.get("d4data_path")) != str(d4data_path):
        return None
    if data.get("source_mtimes") != _source_mtimes(d4data_path):
        log.info("Actor name index cache is stale, will rebuild")
        return None

    index: dict[str, ActorDisplayInfo] = {}
    for stem, fields in data.get("entries", {}).items():
        try:
            display_name, actor_name = fields
            index[stem] = ActorDisplayInfo(
                stem=stem,
                display_name=display_name,
                actor_name=actor_name,
            )
        except (ValueError, TypeError):
            log.debug("Corrupt actor-index cache entry; discarding cache")
            return None

    log.info("Loaded actor name index from disk cache: %d entries", len(index))
    return index


def _save_cache_to_disk(
    d4data_path: Path,
    index: dict[str, ActorDisplayInfo],
) -> None:
    """Persist the index atomically to the on-disk cache."""
    cache_path = _actor_index_cache_path(d4data_path)
    if cache_path is None:
        return
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "d4data_path": str(d4data_path),
        "source_mtimes": _source_mtimes(d4data_path),
        "entry_count": len(index),
        "entries": {
            stem: [info.display_name, info.actor_name]
            for stem, info in index.items()
        },
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
        tmp.replace(cache_path)
        log.info("Actor name index cache written: %s", cache_path)
    except OSError as exc:
        log.warning("Failed to write actor name index cache: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Build
# ──────────────────────────────────────────────────────────────────────────────


def _canon(name: str) -> str:
    """Lowercase + alphanumeric-only, for fuzzy actor↔appearance match.

    The brief's "case-insensitive exact match" fails for the canonical
    case (appearance ``FallenShaman`` vs actor ``fallen_shaman``) because
    the appearance stem is CamelCase and the actor name is snake_case.
    Stripping non-alphanumerics lets both sides agree on ``fallenshaman``.
    """
    return "".join(c for c in name.lower() if c.isalnum())


def _read_actor_name(stringlist_dir: Path, actor_name: str) -> str | None:
    """Read one ``Actor_<name>.stl.json`` → its ``"Name"`` string."""
    path = stringlist_dir / f"Actor_{actor_name}.stl.json"
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    for entry in data.get("arStrings", ()):
        if isinstance(entry, dict) and entry.get("szLabel") == "Name":
            text = entry.get("szText")
            if isinstance(text, str) and text:
                return text
    return None


def _read_all_actor_names(
    stringlist_dir: Path,
    progress_cb=None,
) -> dict[str, str]:
    """Thread-read every ``Actor_*.stl.json`` → ``actor_name → display_name``.

    Enumerates the directory once, then dispatches the per-file syscalls
    through a thread pool — the same pattern ``item_lookup._read_item_names``
    uses for ``Item_*.stl.json``. The dict key is the actor name as
    encoded in the filename (between ``Actor_`` and ``.stl.json``).
    """
    result: dict[str, str] = {}
    if not stringlist_dir.is_dir():
        return result

    actor_names: list[str] = []
    for entry in os.scandir(stringlist_dir):
        name = entry.name
        if not name.startswith("Actor_") or not name.endswith(".stl.json"):
            continue
        actor_names.append(name[len("Actor_"): -len(".stl.json")])

    total = len(actor_names)
    if total == 0:
        return result

    step = max(1, total // 10)
    processed = 0
    with ThreadPoolExecutor(max_workers=_BUILD_WORKERS) as executor:
        for actor_name, display_name in zip(
            actor_names,
            executor.map(
                lambda n: _read_actor_name(stringlist_dir, n),
                actor_names,
            ),
        ):
            if display_name:
                result[actor_name] = display_name
            processed += 1
            if progress_cb is not None and (
                processed % step == 0 or processed >= total
            ):
                progress_cb(processed, total)
    return result


def _pick_actor(
    app_stem: str,
    candidates: list[str],
    name_by_actor: dict[str, str],
) -> tuple[str, str] | None:
    """Pick one actor for *app_stem* and return ``(actor_name, name)``.

    Returns ``None`` when no candidate has a usable display name.
    """
    if not candidates:
        return None

    # 1. Drop noise-suffixed variants if cleaner siblings exist.
    cleaned = [
        c for c in candidates
        if not any(c.lower().endswith(t) for t in _NOISE_SUFFIXES)
    ]
    pool = cleaned or candidates

    # Only candidates with a non-empty StringList Name can win.
    named = [(c, name_by_actor[c]) for c in pool if name_by_actor.get(c)]
    if not named:
        return None

    # 2. Prefer an exact normalised match (``FallenShaman`` ↔ ``fallen_shaman``).
    target = _canon(app_stem)
    exact = [(c, n) for c, n in named if _canon(c) == target]
    if exact:
        exact.sort(key=lambda p: p[0])  # determinism if multiple ties
        return exact[0]

    # 3. Otherwise prefer the shortest actor name (least specific →
    #    usually the base character), alphabetical tiebreak.
    named.sort(key=lambda p: (len(p[0]), p[0]))
    return named[0]


def _build_index(
    d4data_path: Path,
    *,
    progress_cb=None,
) -> dict[str, ActorDisplayInfo]:
    """Build the ``app-stem → ActorDisplayInfo`` index from d4data JSON.

    Loads ``CoreTOC.dat.json``, ``incomingSnoReferences.json`` and every
    ``Actor_*.stl.json``, then resolves each appearance through the
    actor-reference chain. See the module docstring for the chain shape.
    """
    d4 = Path(d4data_path)
    paths = _source_paths(d4)
    coretoc_path = paths["coretoc"]

    if not coretoc_path.is_file():
        log.warning("CoreTOC not found, actor name index empty: %s", coretoc_path)
        return {}

    try:
        with coretoc_path.open("r", encoding="utf-8") as f:
            toc = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("CoreTOC load failed: %s", exc)
        return {}

    actor_id_to_name: dict[str, str] = {}
    appearance_id_to_stem: dict[str, str] = {}
    for group, entries in toc.items():
        if not isinstance(entries, dict):
            continue
        if group == _GROUP_ACTOR:
            for sno_id, name in entries.items():
                if isinstance(name, str):
                    actor_id_to_name[sno_id] = name
        elif group == _GROUP_APPEARANCE:
            for sno_id, name in entries.items():
                if isinstance(name, str):
                    appearance_id_to_stem[sno_id] = name

    incoming: dict = {}
    incoming_path = paths["incoming"]
    if incoming_path.is_file():
        try:
            with incoming_path.open("r", encoding="utf-8") as f:
                incoming = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("incomingSnoReferences load failed: %s", exc)
    else:
        log.warning(
            "incomingSnoReferences.json absent — actor name index will be empty",
        )
        return {}

    # Bulk-read every Actor_*.stl.json once. ~12K files; the per-file
    # syscall dominates, so threading is a big win. After this we never
    # touch the StringList dir again.
    name_by_actor = _read_all_actor_names(
        paths["stringlist"], progress_cb=progress_cb,
    )

    # Walk every appearance, collect its actor-group references, pick one.
    index: dict[str, ActorDisplayInfo] = {}
    for app_id, app_stem in appearance_id_to_stem.items():
        refs = incoming.get(str(app_id), ())
        candidates: list[str] = []
        for ref in refs:
            actor_name = actor_id_to_name.get(str(ref))
            if actor_name is not None:
                candidates.append(actor_name)
        chosen = _pick_actor(app_stem, candidates, name_by_actor)
        if chosen is None:
            continue
        actor_name, display_name = chosen
        index[app_stem.lower()] = ActorDisplayInfo(
            stem=app_stem.lower(),
            display_name=display_name,
            actor_name=actor_name,
        )

    log.info(
        "Actor name index: %d appearances, %d resolved to a name",
        len(appearance_id_to_stem), len(index),
    )
    return index


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def get_cached_index(
    d4data_path: Path,
) -> dict[str, ActorDisplayInfo] | None:
    """Return the in-memory index if already populated, else ``None``.

    Never builds — :func:`get_display_name` relies on this so a GUI-thread
    lookup can't trigger the full build. The build runs in
    ``ActorIndexBuildWorker``.
    """
    key = Path(d4data_path).resolve()
    with _INDEX_LOCK:
        return _INDEX_CACHE.get(key)


def get_or_build_index(
    d4data_path: Path,
    *,
    progress_cb=None,
) -> dict[str, ActorDisplayInfo]:
    """Return the cached index, building it on first call.

    Warm disk cache → returns in milliseconds. Cold → full build, then
    persisted to disk. Keyed by the resolved d4data path so multiple
    checkouts get independent indices.
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


def _normalise_stem(app_stem: str) -> str:
    """Reduce a SNO path or filename to a bare lowercase appearance stem."""
    stem = app_stem.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if stem.lower().endswith(".app"):
        stem = stem[:-4]
    return stem.lower()


def get_display_info(
    d4data_path: Path,
    app_stem: str,
) -> ActorDisplayInfo | None:
    """Return the :class:`ActorDisplayInfo` for an appearance, or ``None``.

    Accepts a bare stem, a filename (``…​.app``) or a full SNO path.
    Returns ``None`` when the index isn't built yet or the stem has no
    resolvable actor name — callers fall back to the stem in both cases.
    """
    if not app_stem:
        return None
    index = get_cached_index(d4data_path)
    if not index:
        return None
    return index.get(_normalise_stem(app_stem))


def get_display_name(
    d4data_path: Path,
    app_stem: str,
) -> str | None:
    """Return the actor display name for an appearance, or ``None``."""
    info = get_display_info(d4data_path, app_stem)
    return info.display_name if info is not None else None


def clear_cache() -> None:
    """Drop the in-memory index (primarily for tests)."""
    with _INDEX_LOCK:
        _INDEX_CACHE.clear()
