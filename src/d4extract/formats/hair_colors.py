"""Parametric hair-colour palette (``HairColorDefinition``).

D4 ships **31 hair colours** as individual SNO files under
``base/meta/HairColor/`` — ``001DeepBrown`` through ``031BlackWhite``.
Each ``HairColorDefinition`` (struct hash ``3914528908``) carries:

* ``nSortOrder``           — palette sort position
* ``fUsableByClass``       — 8 ints, one per class (all ``1`` — universal)
* ``rgbaColors``           — 3 RGBA tints (primary / secondary / accent)
* ``rgbaColors2``          — 3 alternate / highlight RGBA tints
* ``flHairColorInfluence`` — runtime blend factor (typically ``1.0``)

Unlike the parametric *skin* system (HSV shader floats — see
:mod:`d4extract.formats.skin_tones`), hair colour is a straight **RGBA
tint**: the hair BASE_COLOR texture ships near-white as a tint target,
and the runtime multiplies it by the chosen preset's colour.

**Source of truth.** ``d4data/json/base/meta/HairColor/NNN<Name>.hcl.json``.
The directory also contains a dev-garbage ``Axe Bad Data.hcl.json`` —
that file (and anything not matching the ``NNN<Name>`` pattern) is
filtered out, leaving exactly 31 entries.

The visibly two-colour swatches in the in-game UI use ``rgbaColors[0]``
and ``rgbaColors[1]`` as primary + secondary (e.g. ``025BlackTeal`` is
black + teal). This module's MVP consumer tints with ``rgbaColors[0]``
only; the secondary / tertiary / ``rgbaColors2`` tones are plumbed
through for future duotone work.

Regenerate the checked-in cache with::

    d4extract extract-hair-colors --d4data-path <d4data/json>

The cache (``d4extract/data/hair_colors.json``) is committed so callers
without a d4data checkout still resolve the palette.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ``NNN<Name>.hcl.json`` — the 3-digit prefix is the palette position,
# the remainder is the display name. ``Axe Bad Data.hcl.json`` (and any
# other oddly-named file) fails this match and is skipped.
_HCL_NAME_RE = re.compile(r"^(\d{3})(.+)\.hcl\.json$")

# Diablo IV ships exactly this many hair colours. A short read means the
# d4data tree is incomplete — fail loudly rather than emit a partial list.
_EXPECTED_COUNT = 31

# Checked-in cache: d4extract/data/hair_colors.json. ``hair_colors.py``
# is at d4extract/src/d4extract/formats/ — parents[3] is the repo root.
DEFAULT_CACHE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "hair_colors.json"
)


@dataclass(frozen=True, slots=True)
class HairColor:
    """One ``HairColorDefinition`` palette entry.

    ``id`` is a stable ``"H01"``..``"H31"`` index assigned by sort
    position (1-indexed). ``rgba_colors`` / ``rgba_colors2`` are each
    three RGBA tuples in 0–1 floats.
    """

    id: str
    display_name: str
    sort_order: int
    usable_by: tuple[int, ...]
    rgba_colors: tuple[tuple[float, float, float, float], ...]
    rgba_colors2: tuple[tuple[float, float, float, float], ...]
    influence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "sort_order": self.sort_order,
            "usable_by": list(self.usable_by),
            "rgba_colors": [list(c) for c in self.rgba_colors],
            "rgba_colors2": [list(c) for c in self.rgba_colors2],
            "influence": self.influence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HairColor":
        return cls(
            id=str(d["id"]),
            display_name=str(d.get("display_name", d["id"])),
            sort_order=int(d.get("sort_order", 0)),
            usable_by=tuple(int(v) for v in d.get("usable_by", ())),
            rgba_colors=_triple(d.get("rgba_colors")),
            rgba_colors2=_triple(d.get("rgba_colors2")),
            influence=float(d.get("influence", 1.0)),
        )


class HairColorError(RuntimeError):
    """Raised when the HairColorDefinition palette cannot be located."""


# ─── Public API ──────────────────────────────────────────────────────

def load_hair_colors(
    d4data_path: Path | str | None,
    cache_path: Path | str | None = None,
) -> list[HairColor]:
    """Return all 31 ``HairColorDefinition`` entries, in ``nSortOrder``.

    ``id`` is ``"H01"``..``"H31"`` by sort position (1-indexed);
    ``display_name`` is the filename's ``<Name>`` part.

    Resolution order (mirrors :func:`skin_tones.load_skin_tones`):

    1. If ``cache_path`` is given and exists, load from there.
    2. Otherwise, if ``d4data_path`` is given, read every
       ``.hcl.json`` under ``<d4data>/base/meta/HairColor/``, write the
       cache (when ``cache_path`` is given), and return.
    3. If neither is available, raise :class:`FileNotFoundError`.

    Raises :class:`HairColorError` if a d4data checkout is present but
    fewer than 31 valid entries are found.
    """
    if cache_path is not None:
        cp = Path(cache_path)
        if cp.is_file():
            return _load_from_cache(cp)

    if d4data_path is not None:
        colors = _load_from_d4data(Path(d4data_path))
        if cache_path is not None:
            save_hair_colors(colors, Path(cache_path))
        return colors

    raise FileNotFoundError(
        "load_hair_colors: no existing cache_path and no d4data_path — "
        "cannot locate the HairColorDefinition palette. Run "
        "`d4extract extract-hair-colors --d4data-path <d4data/json>` once "
        "to generate data/hair_colors.json."
    )


def save_hair_colors(colors: list[HairColor], path: Path | str) -> None:
    """Write ``colors`` to a JSON cache file (list of dicts)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            [c.to_dict() for c in colors], f, indent=2, ensure_ascii=False,
        )


# ─── Internal ────────────────────────────────────────────────────────

def _load_from_cache(cache_path: Path) -> list[HairColor]:
    with open(cache_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise HairColorError(f"{cache_path}: cache is not a JSON list")
    return [HairColor.from_dict(d) for d in data]


def _load_from_d4data(d4data_path: Path) -> list[HairColor]:
    hair_dir = d4data_path / "base" / "meta" / "HairColor"
    if not hair_dir.is_dir():
        raise HairColorError(
            f"No HairColor directory at {hair_dir}. Check that "
            f"--d4data-path points at the d4data 'json/' directory."
        )

    # (sort_order, display_name, HairColorDefinition dict) before sorting.
    parsed: list[tuple[int, str, dict[str, Any]]] = []
    for hcl_path in sorted(hair_dir.glob("*.hcl.json")):
        m = _HCL_NAME_RE.match(hcl_path.name)
        if m is None:
            # "Axe Bad Data.hcl.json" and any other off-pattern file.
            continue
        data = _read_json(hcl_path)
        if data is None:
            continue
        display_name = m.group(2)
        sort_order = int(data.get("nSortOrder", 0))
        parsed.append((sort_order, display_name, data))

    if len(parsed) < _EXPECTED_COUNT:
        raise HairColorError(
            f"Found only {len(parsed)} hair colour(s) under {hair_dir}; "
            f"expected {_EXPECTED_COUNT}. The d4data tree looks incomplete."
        )

    # Sort by nSortOrder, then display name for a deterministic tie-break.
    parsed.sort(key=lambda t: (t[0], t[1]))
    return [
        _parse_hair_color(display_name, sort_order, data, index)
        for index, (sort_order, display_name, data) in enumerate(parsed)
    ]


def _parse_hair_color(
    display_name: str, sort_order: int, data: dict[str, Any], index: int,
) -> HairColor:
    return HairColor(
        id=f"H{index + 1:02d}",
        display_name=display_name,
        sort_order=sort_order,
        usable_by=_usable_by(data),
        rgba_colors=_rgba_triple(data.get("rgbaColors")),
        rgba_colors2=_rgba_triple(data.get("rgbaColors2")),
        influence=float(data.get("flHairColorInfluence", 1.0)),
    )


def _usable_by(data: dict[str, Any]) -> tuple[int, ...]:
    raw = data.get("fUsableByClass")
    if isinstance(raw, list):
        try:
            return tuple(int(v) for v in raw)
        except (TypeError, ValueError):
            return ()
    return ()


def _rgba_triple(
    raw: Any,
) -> tuple[tuple[float, float, float, float], ...]:
    """Convert a list of ``{r,g,b,a}`` uint8 dicts to three 0–1 RGBA tuples.

    Always returns exactly three entries — padded with opaque black when
    the source has fewer — so callers can index ``[0]``/``[1]``/``[2]``.
    """
    colors: list[tuple[float, float, float, float]] = []
    if isinstance(raw, list):
        for c in raw[:3]:
            if not isinstance(c, dict):
                continue
            colors.append((
                float(c.get("r", 0)) / 255.0,
                float(c.get("g", 0)) / 255.0,
                float(c.get("b", 0)) / 255.0,
                float(c.get("a", 255)) / 255.0,
            ))
    while len(colors) < 3:
        colors.append((0.0, 0.0, 0.0, 1.0))
    return tuple(colors)


def _triple(
    raw: Any,
) -> tuple[tuple[float, float, float, float], ...]:
    """Tuple-ize cached ``[[r,g,b,a], ...]`` lists into three RGBA tuples."""
    colors: list[tuple[float, float, float, float]] = []
    if isinstance(raw, list):
        for c in raw[:3]:
            if not isinstance(c, (list, tuple)) or len(c) < 3:
                continue
            colors.append((
                float(c[0]), float(c[1]), float(c[2]),
                float(c[3]) if len(c) > 3 else 1.0,
            ))
    while len(colors) < 3:
        colors.append((0.0, 0.0, 0.0, 1.0))
    return tuple(colors)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
