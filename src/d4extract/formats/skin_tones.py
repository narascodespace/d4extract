"""Parametric skin-tone palette (``PersonaSkinColor``).

D4's skin customization is a **parametric HSV** system — four shader
floats per tone, not texture swaps. Each ``PersonaSkinColor`` entry
(struct hash ``1904447052``) carries:

* ``eBase``       — base skin-family enum
* ``flHue``       — hue-rotation fraction (treated as [0, 1])
* ``flSaturation``— saturation delta
* ``flValue``     — value delta
* ``flDarken``    — brightness multiplier [0, 1] (1.0 = unchanged,
                    lower = darker)
* ``rgbaUIDisplayColor`` — the swatch shown in the customization UI
* ``szLabel``     — a human label, e.g. ``"5-Ivory STANDARD-European"``

**Source of truth.** The *player-character* skin palette is the
``arSkinColorChoices`` array under ``ptPlayerData`` in the per-class
Actor definitions, ``base/meta/Actor/<class><gender>.acr.json``. The
list is **identical across all 12 class/gender Actors**, so this module
reads whichever canonical player Actor file it finds first — no
per-class handling is needed.

(The original task brief pointed at ``GlobalNPCCustomizationData``'s
``arSkinColorList`` — that field is the 7-entry *NPC* darkness scale,
not the player palette. The player list, found during verification, has
32 raw entries; the trailing 3 ``Virtiligo`` and 1
``HairStylePreviewColor`` entries are filtered out — see
:data:`_EXCLUDED_PREFIXES` — leaving **28** standard skin tones.)

Regenerate the checked-in cache with::

    d4extract extract-skin-tones --d4data-path <d4data/json>

The cache (``d4extract/data/skin_tones.json``) is committed so callers
without a d4data checkout still resolve the palette.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Canonical player-class Actor stems. The arSkinColorChoices palette is
# identical across all of them, so the first file that exists wins.
_PLAYER_ACTOR_STEMS: tuple[str, ...] = tuple(
    f"{cls}{gender}"
    for cls in (
        "sorcerer", "barbarian", "druid", "rogue",
        "necromancer", "spiritborn", "paladin", "warlock",
    )
    for gender in ("M", "F")
)

# ``szLabel`` markers for entries that are not standard skin tones:
# the 3 vitiligo skin-marking variants (spelled "Virtiligo" in the data)
# and the hair-UI preview helper. Filtered out of the palette.
_EXCLUDED_PREFIXES: tuple[str, ...] = ("virtiligo",)
_EXCLUDED_EXACT: frozenset[str] = frozenset({"hairstylepreviewcolor"})

# Checked-in cache: d4extract/data/skin_tones.json. ``skin_tones.py`` is
# at d4extract/src/d4extract/formats/ — parents[3] is the repo root.
DEFAULT_CACHE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "skin_tones.json"
)


@dataclass(frozen=True, slots=True)
class SkinTone:
    """One ``PersonaSkinColor`` palette entry.

    ``id`` is a stable ``"S00"``..``"S<n>"`` index (assigned after
    filtering, in source order). ``label`` is the in-data ``szLabel``.
    ``ui_color`` is the swatch, RGBA in 0–1.
    """

    id: str
    label: str
    ebase: int
    hue: float
    saturation: float
    value: float
    darken: float
    ui_color: tuple[float, float, float, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "ebase": self.ebase,
            "hue": self.hue,
            "saturation": self.saturation,
            "value": self.value,
            "darken": self.darken,
            "ui_color": list(self.ui_color),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SkinTone":
        col = d.get("ui_color") or [1.0, 1.0, 1.0, 1.0]
        return cls(
            id=str(d["id"]),
            label=str(d.get("label", d["id"])),
            ebase=int(d.get("ebase", 0)),
            hue=float(d.get("hue", 0.0)),
            saturation=float(d.get("saturation", 0.0)),
            value=float(d.get("value", 0.0)),
            darken=float(d.get("darken", 1.0)),
            ui_color=(
                float(col[0]), float(col[1]), float(col[2]),
                float(col[3]) if len(col) > 3 else 1.0,
            ),
        )


class SkinToneError(Exception):
    """Raised when the PersonaSkinColor palette cannot be located."""


# ─── Public API ──────────────────────────────────────────────────────

def load_skin_tones(
    d4data_path: Path | str | None = None,
    cache_path: Path | str | None = None,
) -> list[SkinTone]:
    """Return the player skin-tone palette, in source order.

    ``id`` is ``"S00"``..``"S<n>"`` by position (after the non-tone
    entries are filtered out).

    Resolution order:

    1. If ``cache_path`` is given and exists, load from there.
    2. Otherwise, if ``d4data_path`` is given, read
       ``arSkinColorChoices`` from a player Actor definition, write the
       cache (when ``cache_path`` is given), and return.
    3. If neither is available, raise :class:`FileNotFoundError`.

    Raises :class:`SkinToneError` if a d4data checkout is present but no
    player Actor file with a skin palette can be found.
    """
    if cache_path is not None:
        cp = Path(cache_path)
        if cp.is_file():
            return _load_from_cache(cp)

    if d4data_path is not None:
        tones = _load_from_d4data(Path(d4data_path))
        if cache_path is not None:
            save_skin_tones(tones, Path(cache_path))
        return tones

    raise FileNotFoundError(
        "load_skin_tones: no existing cache_path and no d4data_path — "
        "cannot locate the PersonaSkinColor palette. Run "
        "`d4extract extract-skin-tones --d4data-path <d4data/json>` once "
        "to generate data/skin_tones.json."
    )


def save_skin_tones(tones: list[SkinTone], path: Path | str) -> None:
    """Write ``tones`` to a JSON cache file (list of dicts)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([t.to_dict() for t in tones], f, indent=2, ensure_ascii=False)


# ─── Internal ────────────────────────────────────────────────────────

def _load_from_cache(cache_path: Path) -> list[SkinTone]:
    with open(cache_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SkinToneError(f"{cache_path}: cache is not a JSON list")
    return [SkinTone.from_dict(d) for d in data]


def _load_from_d4data(d4data_path: Path) -> list[SkinTone]:
    actor_dir = d4data_path / "base" / "meta" / "Actor"
    raw_list: list[Any] | None = None
    for stem in _PLAYER_ACTOR_STEMS:
        actor_file = actor_dir / f"{stem}.acr.json"
        if not actor_file.is_file():
            continue
        data = _read_json(actor_file)
        if data is None:
            continue
        choices = _find_skin_color_choices(data)
        if choices:
            raw_list = choices
            break

    if raw_list is None:
        raise SkinToneError(
            f"No player skin palette found under {actor_dir}. Expected "
            f"'arSkinColorChoices' in a player Actor "
            f"(e.g. {_PLAYER_ACTOR_STEMS[0]}.acr.json). Check that "
            f"--d4data-path points at the d4data 'json/' directory."
        )

    # Filter out the non-tone entries (vitiligo / hair preview), then
    # index the survivors in source order.
    tones: list[SkinTone] = []
    for entry in raw_list:
        label = str(entry.get("szLabel") or "")
        if _is_excluded(label):
            continue
        tones.append(_parse_persona(entry, len(tones)))
    return tones


def _is_excluded(label: str) -> bool:
    """Whether ``label`` names a non-standard (filtered) palette entry."""
    norm = label.strip().lower()
    if norm in _EXCLUDED_EXACT:
        return True
    return any(norm.startswith(p) for p in _EXCLUDED_PREFIXES)


def _find_skin_color_choices(obj: Any) -> list[Any] | None:
    """Recursively locate the ``arSkinColorChoices`` array in the tree.

    It is nested under ``ptPlayerData[]``; a recursive search keeps this
    robust to dump-layout changes.
    """
    if isinstance(obj, dict):
        found = obj.get("arSkinColorChoices")
        if isinstance(found, list):
            return found
        for value in obj.values():
            hit = _find_skin_color_choices(value)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for value in obj:
            hit = _find_skin_color_choices(value)
            if hit is not None:
                return hit
    return None


def _parse_persona(entry: dict[str, Any], index: int) -> SkinTone:
    rgba = entry.get("rgbaUIDisplayColor") or {}
    ui_color = (
        float(rgba.get("r", 255)) / 255.0,
        float(rgba.get("g", 255)) / 255.0,
        float(rgba.get("b", 255)) / 255.0,
        float(rgba.get("a", 255)) / 255.0,
    )
    return SkinTone(
        id=f"S{index:02d}",
        label=str(entry.get("szLabel") or f"Tone {index + 1}"),
        ebase=int(entry.get("eBase", 0)),
        hue=float(entry.get("flHue", 0.0)),
        saturation=float(entry.get("flSaturation", 0.0)),
        value=float(entry.get("flValue", 0.0)),
        darken=float(entry.get("flDarken", 1.0)),
        ui_color=ui_color,
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
