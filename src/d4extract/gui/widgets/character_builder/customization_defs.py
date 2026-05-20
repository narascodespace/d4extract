"""Static customization definitions extracted from D4 data files.

The in-game character creator's Face / Hair Style / Facial Hair options
are driven by definition files (``Face/*.fac.json``,
``HairStyle/*.har.json``, ``FacialHair/*.fhr.json``). Each definition
specifies:

* ``dwSubObjectStyle`` (or ``unk_2ab2122`` for facial hair) — the
  numeric index that maps to the appearance file suffix (P##, H##, B##).
* ``fUsableByClass`` — an 8-element array gating visibility per class.
  Indices: 0=Sorcerer, 1=Druid, 2=Barbarian, 3=Rogue, 4=Necromancer,
  5=Spiritborn, 6=Paladin, 7=Warlock.

Some options are **material-only** (no separate ``.app`` mesh) — notably
"Clean" and "Average Stubble" for facial hair. These still appear in the
dropdown but won't trigger a mesh load.

This module bakes the definition data so the GUI can build accurate
dropdown lists without reading the JSON files at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass


# fUsableByClass index per class name.
_CLASS_INDEX: dict[str, int] = {
    "Sorcerer": 0,
    "Druid": 1,
    "Barbarian": 2,
    "Rogue": 3,
    "Necromancer": 4,
    "Spiritborn": 5,
    "Paladin": 6,
    "Warlock": 7,
}


@dataclass(frozen=True, slots=True)
class CustomizationDef:
    """One definition-file entry for a customization option."""

    display_name: str
    style_index: int          # dwSubObjectStyle / unk_2ab2122
    file_suffix: str          # "P00", "H09", "B02", etc.
    usable_by: tuple[int, ...]  # fUsableByClass (8 ints, 0 or 1)
    has_mesh: bool = True     # False for material-only (Clean, Stubble)


# -----------------------------------------------------------------------
# Face definitions (from Face/*.fac.json)
# All faces are usable by all classes. dwSubObjectStyle → P##.
# -----------------------------------------------------------------------

FACE_DEFS: tuple[CustomizationDef, ...] = (
    CustomizationDef("Caucasian",  0, "P00", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Asian",      1, "P01", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("African",    2, "P02", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Persian",    3, "P03", (1, 1, 1, 1, 1, 1, 1, 1)),
)

# -----------------------------------------------------------------------
# Hair style definitions (from HairStyle/*.har.json)
# H00–H08 + H11–H19 are global. H09 is class-exclusive (one per class).
# H10 is Rogue-only.
# -----------------------------------------------------------------------

HAIR_DEFS: tuple[CustomizationDef, ...] = (
    CustomizationDef("Buzz",                  0, "H00", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Wedge",                 1, "H01", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Square",                2, "H02", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Short Forward Fringe",  3, "H03", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Classic Midi",          4, "H04", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Curly Afro",            5, "H05", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Samurai",               6, "H06", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Dread Bun",             7, "H07", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Ponytail",              8, "H08", (1, 1, 1, 1, 1, 1, 1, 1)),
    # Class-exclusive H09 variants — each one only usable by its class.
    CustomizationDef("Braided Tail",          9, "H09", (1, 0, 0, 0, 0, 0, 0, 0)),  # Sorcerer
    CustomizationDef("Dreadlocks",            9, "H09", (0, 1, 0, 0, 0, 0, 0, 0)),  # Druid
    CustomizationDef("Mohawk",                9, "H09", (0, 0, 1, 0, 0, 0, 0, 0)),  # Barbarian
    CustomizationDef("Mohawk",                9, "H09", (0, 0, 0, 1, 0, 0, 0, 0)),  # Rogue
    CustomizationDef("Long Half Shaved",      9, "H09", (0, 0, 0, 0, 1, 0, 0, 0)),  # Necromancer
    CustomizationDef("Afro Bun",              9, "H09", (0, 0, 0, 0, 0, 1, 0, 0)),  # Spiritborn
    CustomizationDef("Short Hair",            9, "H09", (0, 0, 0, 0, 0, 0, 1, 0)),  # Paladin
    CustomizationDef("Warlock PH",            9, "H09", (0, 0, 0, 0, 0, 0, 0, 1)),  # Warlock
    # H10 — Rogue-only.
    CustomizationDef("Long Braided Tail",    10, "H10", (0, 0, 0, 1, 0, 0, 0, 0)),  # Rogue
    # Global styles continued.
    CustomizationDef("Young Locs",           11, "H11", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Champion's Roots",     12, "H12", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Full Straight Bun",    13, "H13", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Pulled Back Curly",    14, "H14", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Pulled Back Long",     15, "H15", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Plaited Vines",        16, "H16", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Rider's Mane",         17, "H17", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Full Bloom",           18, "H18", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Long",                 19, "H19", (1, 1, 1, 1, 1, 1, 1, 1)),
)

# -----------------------------------------------------------------------
# Facial hair definitions (from FacialHair/*.fhr.json)
# Clean and Stubble are material-only (no mesh). B02–B08 are global.
# B09 is class-exclusive. Males only — the SlotPanel hides this row for
# Female characters.
# -----------------------------------------------------------------------

FACIAL_HAIR_DEFS: tuple[CustomizationDef, ...] = (
    CustomizationDef("No Facial Hair",    0, "",   (1, 1, 1, 1, 1, 1, 1, 1), has_mesh=False),
    CustomizationDef("Average Stubble",   0, "",   (1, 1, 1, 1, 1, 1, 1, 1), has_mesh=False),
    CustomizationDef("Fine Fu Manchu",    2, "B02", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Thin Mustache",     3, "B03", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Scar Chin Goatee",  4, "B04", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Arched Full Goatee", 5, "B05", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Narrow Beard",      6, "B06", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Thick Full Beard",  7, "B07", (1, 1, 1, 1, 1, 1, 1, 1)),
    CustomizationDef("Bushy Long Beard",  8, "B08", (1, 1, 1, 1, 1, 1, 1, 1)),
    # Class-exclusive B09 variants.
    CustomizationDef("Sorcerer Beard",    9, "B09", (1, 0, 0, 0, 0, 0, 0, 0)),
    CustomizationDef("Druid Beard",       9, "B09", (0, 1, 0, 0, 0, 0, 0, 0)),
    CustomizationDef("Barbarian Beard",   9, "B09", (0, 0, 1, 0, 0, 0, 0, 0)),
    CustomizationDef("Rogue Beard",       9, "B09", (0, 0, 0, 1, 0, 0, 0, 0)),
    CustomizationDef("Necromancer Beard",  9, "B09", (0, 0, 0, 0, 1, 0, 0, 0)),
    CustomizationDef("Spiritborn Beard",  9, "B09", (0, 0, 0, 0, 0, 1, 0, 0)),
    CustomizationDef("Paladin Beard",     9, "B09", (0, 0, 0, 0, 0, 0, 1, 0)),
    CustomizationDef("Warlock Beard",     9, "B09", (0, 0, 0, 0, 0, 0, 0, 1)),
)


def get_options_for_class(
    defs: tuple[CustomizationDef, ...],
    class_name: str,
) -> list[CustomizationDef]:
    """Return the definitions available to ``class_name``, sorted by style index."""
    idx = _CLASS_INDEX.get(class_name)
    if idx is None:
        return []
    return sorted(
        (d for d in defs if d.usable_by[idx]),
        key=lambda d: (d.style_index, d.display_name),
    )
