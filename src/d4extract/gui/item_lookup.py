"""Equipment ``.app`` stem → in-game display name lookup.

D4 does not carry a display name on its ``ItemDefinition`` JSON at all.
Resolution goes through a 2-hop name-convention join:

    .app stem  →  Actor name  →  Item stem  →  StringList

* The display name lives at ``<d4data>/enUS_Text/meta/StringList/
  Item_<itemStem>.stl.json`` — the ``szText`` of the ``arStrings[]``
  entry whose ``szLabel == "Name"``.
* **Weapons / off-hands** have an Actor named the *same* as the
  appearance stem; ``incomingSnoReferences[actorId]`` yields the Item.
* **Non-cosmetic armor** (style tokens ``base`` / ``crft`` / ``sets`` /
  ``gnrc`` / ``uniq``): the appearance ``<classGender>_<tok><num>_<SLOT>``
  has a matching Item-actor named ``<SLOT>_<tok><num>`` (e.g.
  ``HLM_sets51``). The player mesh itself is never in the SNO reference
  graph, so the Actor's *name* is the bridge.
* **Cosmetic armor** (tokens ``stor`` / ``dlux`` / ``pvpa``) has no such
  Actor — the cosmetic Item is matched by name on ``(slot, class,
  number)``.

When one mesh backs several items the pick is disambiguated by
**class preference** (the class hint in the appearance stem) then a
**quality tiebreak** (Unique > Set > Legendary > Rare > Magic > Normal).

See ``docs/research-equipment-display-names.md`` for the full chain
analysis, the cross-slot sample table, and the ~95 % hit-rate
measurement. The ~5 % that don't resolve are legitimately nameless
assets (VFX sub-meshes, test meshes, unreleased cosmetics) — callers
fall back to the SNO stem for those.

Caching mirrors :mod:`d4extract.gui.anim_lookup`: an on-disk JSON cache
(warm launch, ~milliseconds) backed by a full cold build (~8-12 s) over
``CoreTOC.dat.json`` + ``incomingSnoReferences.json`` + the per-item
StringList files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_BUILD_WORKERS = 8

# SNO group ids in CoreTOC.dat.json.
_GROUP_ACTOR = "1"
_GROUP_ITEM = "73"


@dataclass(frozen=True)
class ItemDisplayInfo:
    """Resolved display data for one equipment ``.app`` stem.

    ``stem`` is the lookup key (the appearance filename without
    directory or ``.app``). ``item_stem`` / ``quality`` / ``item_class``
    record which Item the name came from and are kept so a future
    PieceBrowser filter ("show only Uniques") can use them without
    re-deriving the chain.
    """

    stem: str
    display_name: str
    item_stem: str
    quality: str | None
    item_class: str | None


# In-memory index cache, keyed by the resolved d4data path. Same shape
# and locking discipline as ``anim_lookup._INDEX_CACHE``.
_INDEX_CACHE: dict[Path, dict[str, ItemDisplayInfo]] = {}
_INDEX_LOCK = threading.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Name-parsing tables
# ──────────────────────────────────────────────────────────────────────────────

# Every spelling of a class that appears in appearance prefixes or item
# stems, mapped to one canonical id. Both sides of the cosmetic join and
# the class-preference tiebreak normalise through this so ``barM`` (mesh
# prefix), ``Barb`` and ``Barbarian`` (item stem segments) all agree.
_CLASS_FORMS: dict[str, tuple[str, ...]] = {
    "barbarian": ("bar", "barb", "barbarian"),
    "druid": ("dru", "druid"),
    "necromancer": ("nec", "necro", "necromancer"),
    "paladin": ("pal", "paladin"),
    "rogue": ("rog", "rogue"),
    "sorcerer": ("sor", "sorc", "sorcerer"),
    "spiritborn": ("spi", "spirit", "spiritborn"),
    "warlock": ("war", "warlock"),
}
_CLASS_CANON: dict[str, str] = {
    form: canon for canon, forms in _CLASS_FORMS.items() for form in forms
}

# Appearance slot suffix → cosmetic-item slot word / armor Actor prefix.
_SLOT_WORD: dict[str, str] = {
    "hlm": "helm", "trs": "chest", "bdy": "chest",
    "glv": "gloves", "leg": "pants", "bts": "boots",
}
_SLOT_ACTOR: dict[str, str] = {
    "hlm": "HLM", "trs": "TRS", "bdy": "TRS",
    "glv": "GLV", "leg": "LEG", "bts": "BTS",
}

# Style tokens that have no ``<SLOT>_<tok><num>`` Actor — cosmetics,
# resolved by the cosmetic-item name index instead.
_COSMETIC_TOKENS: frozenset[str] = frozenset({"stor", "dlux", "dulx", "pvpa"})

# Quality words found in item stems, ranked for the disambiguation
# tiebreak. A recognisable Unique name beats a generic Legendary one.
_QUALITY_RANK: dict[str, int] = {
    "unique": 6, "mythic": 6, "set": 5, "legendary": 4,
    "rare": 3, "magic": 2, "normal": 1, "cosmetic": 0,
}

# Armor appearance stem: ``<cls><gender>_<token><number>_<SLOT>``.
_ARMOR_APP_RE = re.compile(
    r"^([A-Za-z]{3})([MF])_([A-Za-z]+?)(\d+)_(HLM|TRS|BDY|GLV|LEG|BTS)$",
    re.IGNORECASE,
)
# Weapon / off-hand appearance stem — same prefix families the
# PieceBrowser browses for the mh/oh slots.
_WEAPON_APP_RE = re.compile(
    r"^(?:Axe|Sword|Dagger|Mace|Scythe|Wand|Polearm|Staff|Bow|Crossbow|"
    r"2HAxe|2HSword|2HMace|Shield|offHandFocus|offHandsDruid|offHandsNecro|"
    r"OffHandsSorc|OffHandTotem|Totem)_",
    re.IGNORECASE,
)
# Cosmetic item stem: ``<Slot>_Cosmetic_<Class>_[<token>]<number>...``.
_COSMETIC_ITEM_RE = re.compile(
    r"^(Helm|Chest|Gloves|Pants|Boots)_Cosmetic_([A-Za-z]+)_(?:[A-Za-z]+)?(\d+)",
    re.IGNORECASE,
)


def _canon_class(token: str | None) -> str | None:
    """Return the canonical class id for *token*, or ``None``.

    ``None`` for the empty string, for ``"Generic"``, and for any
    3-letter prefix that isn't a known class.
    """
    if not token:
        return None
    return _CLASS_CANON.get(token.lower())


def _item_class(item_stem: str) -> str | None:
    """Canonical class of an item, scanned from its stem segments.

    ``Helm_Unique_Rogue_002`` → ``"rogue"``;
    ``Helm_Legendary_Generic_050`` → ``None`` (Generic is not a class).
    """
    for seg in item_stem.split("_"):
        canon = _CLASS_CANON.get(seg.lower())
        if canon is not None:
            return canon
    return None


def _item_quality(item_stem: str) -> str | None:
    """Quality word of an item, scanned from its stem segments.

    ``X2_Helm_Legendary_Generic_base14`` → ``"Legendary"``.
    """
    for seg in item_stem.split("_"):
        if seg.lower() in _QUALITY_RANK:
            return seg.capitalize()
    return None


def _app_stem_class(app_stem: str) -> str | None:
    """Canonical class hinted by an appearance stem's ``<cls><gender>``."""
    m = _ARMOR_APP_RE.match(app_stem)
    if m is None:
        # Weapons/off-hands are class-agnostic at the mesh level.
        return None
    return _canon_class(m.group(1))


# ──────────────────────────────────────────────────────────────────────────────
# On-disk cache helpers (mirrors anim_lookup)
# ──────────────────────────────────────────────────────────────────────────────


def _item_index_cache_path(d4data_path: Path) -> Path | None:
    """Return the on-disk cache file path, or ``None`` if Qt is absent."""
    try:
        from PySide6.QtCore import QStandardPaths
        base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
        slug = hashlib.md5(str(d4data_path).encode()).hexdigest()[:12]
        return Path(base) / "d4extract" / f"item_index_{slug}.json"
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
) -> dict[str, ItemDisplayInfo] | None:
    """Return the persisted index if it exists and matches live d4data."""
    cache_path = _item_index_cache_path(d4data_path)
    if cache_path is None or not cache_path.is_file():
        return None
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("Item index cache load failed: %s", exc)
        return None

    if data.get("schema_version") != _SCHEMA_VERSION:
        return None
    if str(data.get("d4data_path")) != str(d4data_path):
        return None
    if data.get("source_mtimes") != _source_mtimes(d4data_path):
        log.info("Item name index cache is stale, will rebuild")
        return None

    index: dict[str, ItemDisplayInfo] = {}
    for stem, fields in data.get("entries", {}).items():
        try:
            display_name, item_stem, quality, item_class = fields
            index[stem] = ItemDisplayInfo(
                stem=stem,
                display_name=display_name,
                item_stem=item_stem,
                quality=quality,
                item_class=item_class,
            )
        except (ValueError, TypeError):
            log.debug("Corrupt item-index cache entry; discarding cache")
            return None

    log.info("Loaded item name index from disk cache: %d entries", len(index))
    return index


def _save_cache_to_disk(
    d4data_path: Path,
    index: dict[str, ItemDisplayInfo],
) -> None:
    """Persist the index atomically to the on-disk cache."""
    cache_path = _item_index_cache_path(d4data_path)
    if cache_path is None:
        return
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "d4data_path": str(d4data_path),
        "source_mtimes": _source_mtimes(d4data_path),
        "entry_count": len(index),
        "entries": {
            stem: [
                info.display_name,
                info.item_stem,
                info.quality,
                info.item_class,
            ]
            for stem, info in index.items()
        },
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
        tmp.replace(cache_path)
        log.info("Item name index cache written: %s", cache_path)
    except OSError as exc:
        log.warning("Failed to write item name index cache: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Resolution
# ──────────────────────────────────────────────────────────────────────────────


def _items_referencing(
    actor_id: str,
    incoming: dict,
    item_id_to_stem: dict[str, str],
) -> list[str]:
    """Item stems that reference the Actor with id *actor_id*."""
    out: list[str] = []
    for ref in incoming.get(str(actor_id), ()):
        stem = item_id_to_stem.get(str(ref))
        if stem is not None:
            out.append(stem)
    return out


def _resolve_app_stem(
    stem: str,
    *,
    actor_by_name: dict[str, str],
    incoming: dict,
    item_id_to_stem: dict[str, str],
    cosmetic_index: dict[tuple[str, str | None, int], str],
) -> list[str] | None:
    """Return candidate item stems for an appearance stem.

    ``None`` — *stem* is not an equipment appearance (skip it).
    ``[]``   — equipment, but nothing resolved (caller falls back).
    """
    m = _ARMOR_APP_RE.match(stem)
    if m is not None:
        cls_prefix, _gender, token, num_s, slot = m.groups()
        token = token.lower()
        slot = slot.lower()
        num = int(num_s)

        if token in _COSMETIC_TOKENS:
            slot_word = _SLOT_WORD[slot]
            cls = _canon_class(cls_prefix)
            stem_hit = cosmetic_index.get((slot_word, cls, num))
            if stem_hit is None:
                # Generic (non-class) cosmetics back every class's mesh.
                stem_hit = cosmetic_index.get((slot_word, None, num))
            return [stem_hit] if stem_hit else []

        # Non-cosmetic armor: bridge through the <SLOT>_<tok><num> Actor.
        actor_prefix = _SLOT_ACTOR[slot]
        for actor in (
            f"{actor_prefix}_{token}{num:02d}",
            f"{actor_prefix}_{token}{num}",
        ):
            actor_id = actor_by_name.get(actor.lower())
            if actor_id is not None:
                return _items_referencing(actor_id, incoming, item_id_to_stem)
        return []

    if _WEAPON_APP_RE.match(stem) is not None:
        # Weapons / off-hands: the Actor is named like the appearance.
        actor_id = actor_by_name.get(stem.lower())
        if actor_id is not None:
            return _items_referencing(actor_id, incoming, item_id_to_stem)
        return []

    return None


def _choose(
    app_stem: str,
    candidates: list[str],
    name_by_item: dict[str, str],
) -> ItemDisplayInfo | None:
    """Pick one item for *app_stem* and build its :class:`ItemDisplayInfo`.

    Drops candidates with no display name. With several survivors,
    prefers the one whose class matches the appearance, then the
    highest quality; ties break alphabetically for determinism.
    """
    named = [(c, name_by_item[c]) for c in candidates if name_by_item.get(c)]
    if not named:
        return None

    if len(named) > 1:
        app_cls = _app_stem_class(app_stem)

        def _key(pair: tuple[str, str]) -> tuple[int, int, str]:
            item_stem, _name = pair
            cls_match = (
                1 if app_cls is not None and _item_class(item_stem) == app_cls
                else 0
            )
            quality_rank = _QUALITY_RANK.get(
                (_item_quality(item_stem) or "").lower(), 0,
            )
            return (-cls_match, -quality_rank, item_stem)

        named.sort(key=_key)

    item_stem, display_name = named[0]
    return ItemDisplayInfo(
        stem=app_stem,
        display_name=display_name,
        item_stem=item_stem,
        quality=_item_quality(item_stem),
        item_class=_item_class(item_stem),
    )


def _read_item_name(stringlist_dir: Path, item_stem: str) -> str | None:
    """Read one ``Item_<stem>.stl.json`` → its ``"Name"`` string."""
    path = stringlist_dir / f"Item_{item_stem}.stl.json"
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


def _read_item_names(
    stringlist_dir: Path,
    item_stems: set[str],
    progress_cb=None,
) -> dict[str, str]:
    """Threaded read of every needed item StringList → ``stem → name``.

    The per-file syscall cost dominates (each ``.stl.json`` is tiny), so
    a thread pool gives the same multi-× speedup ``anim_lookup`` relies
    on for its 45K-file scan.
    """
    result: dict[str, str] = {}
    if not stringlist_dir.is_dir():
        return result
    stems = sorted(item_stems)
    total = len(stems)
    if total == 0:
        return result

    step = max(1, total // 10)
    processed = 0
    with ThreadPoolExecutor(max_workers=_BUILD_WORKERS) as executor:
        for item_stem, name in zip(
            stems,
            executor.map(lambda s: _read_item_name(stringlist_dir, s), stems),
        ):
            if name:
                result[item_stem] = name
            processed += 1
            if progress_cb is not None and (
                processed % step == 0 or processed >= total
            ):
                progress_cb(processed, total)
    return result


def _build_index(
    d4data_path: Path,
    *,
    progress_cb=None,
) -> dict[str, ItemDisplayInfo]:
    """Build the ``app-stem → ItemDisplayInfo`` index from d4data JSON.

    Loads ``CoreTOC.dat.json`` (id↔name) and ``incomingSnoReferences``
    (the reverse-reference graph), enumerates equipment appearances,
    resolves each through the name-convention chain, then threads the
    per-item StringList reads. See the module docstring for the chain.
    """
    d4 = Path(d4data_path)
    paths = _source_paths(d4)
    coretoc_path = paths["coretoc"]
    appearance_dir = paths["appearance"]

    if not coretoc_path.is_file():
        log.warning("CoreTOC not found, item name index empty: %s", coretoc_path)
        return {}
    if not appearance_dir.is_dir():
        log.warning("Appearance dir not found: %s", appearance_dir)
        return {}

    try:
        with coretoc_path.open("r", encoding="utf-8") as f:
            toc = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("CoreTOC load failed: %s", exc)
        return {}

    actor_by_name: dict[str, str] = {}
    item_id_to_stem: dict[str, str] = {}
    for group, entries in toc.items():
        if not isinstance(entries, dict):
            continue
        if group == _GROUP_ACTOR:
            for sno_id, name in entries.items():
                if isinstance(name, str):
                    actor_by_name[name.lower()] = sno_id
        elif group == _GROUP_ITEM:
            for sno_id, name in entries.items():
                if isinstance(name, str):
                    item_id_to_stem[sno_id] = name

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
            "incomingSnoReferences.json absent — only cosmetic armor will "
            "resolve; weapons and base/crafted armor fall back to stems",
        )

    # Cosmetic-item index, built purely from item stems (no file reads).
    cosmetic_index: dict[tuple[str, str | None, int], str] = {}
    for item_stem in item_id_to_stem.values():
        cm = _COSMETIC_ITEM_RE.match(item_stem)
        if cm is not None:
            slot_word = cm.group(1).lower()
            cls = _canon_class(cm.group(2))
            number = int(cm.group(3))
            cosmetic_index[(slot_word, cls, number)] = item_stem

    # Resolve every equipment appearance to its candidate item stems.
    app_to_items: dict[str, list[str]] = {}
    needed_items: set[str] = set()
    for entry in os.scandir(appearance_dir):
        name = entry.name
        if not name.endswith(".app.json"):
            continue
        app_stem = name[: -len(".app.json")]
        candidates = _resolve_app_stem(
            app_stem,
            actor_by_name=actor_by_name,
            incoming=incoming,
            item_id_to_stem=item_id_to_stem,
            cosmetic_index=cosmetic_index,
        )
        if candidates:
            app_to_items[app_stem] = candidates
            needed_items.update(candidates)

    # Read the StringLists for every referenced item (threaded).
    name_by_item = _read_item_names(
        paths["stringlist"], needed_items, progress_cb=progress_cb,
    )

    # Disambiguate and build the final index, keyed by lowercased stem.
    index: dict[str, ItemDisplayInfo] = {}
    for app_stem, candidates in app_to_items.items():
        info = _choose(app_stem, candidates, name_by_item)
        if info is not None:
            index[app_stem.lower()] = info

    log.info(
        "Item name index: %d equipment appearances, %d resolved to a name",
        len(app_to_items), len(index),
    )
    return index


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def get_cached_index(
    d4data_path: Path,
) -> dict[str, ItemDisplayInfo] | None:
    """Return the in-memory index if already populated, else ``None``.

    Never builds — :func:`get_display_name` relies on this so a GUI-thread
    lookup can't trigger an 8-12 s build. The build runs in
    ``ItemIndexBuildWorker``.
    """
    key = Path(d4data_path).resolve()
    with _INDEX_LOCK:
        return _INDEX_CACHE.get(key)


def get_or_build_index(
    d4data_path: Path,
    *,
    progress_cb=None,
) -> dict[str, ItemDisplayInfo]:
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
) -> ItemDisplayInfo | None:
    """Return the :class:`ItemDisplayInfo` for an appearance, or ``None``.

    Accepts a bare stem, a filename (``…​.app``) or a full SNO path.
    Returns ``None`` when the index isn't built yet or the stem has no
    resolvable name — callers fall back to the stem in both cases.
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
    """Return the in-game display name for an appearance, or ``None``."""
    info = get_display_info(d4data_path, app_stem)
    return info.display_name if info is not None else None


def clear_cache() -> None:
    """Drop the in-memory index (primarily for tests)."""
    with _INDEX_LOCK:
        _INDEX_CACHE.clear()
