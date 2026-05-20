"""Data model for the Character Builder.

The builder distinguishes two kinds of selection:

* **Customization** — class, gender, face, hair style, facial hair.
  These pick a single mesh-loadable .app per category and cascade
  (gender depends on class, face/hair depend on gender).
* **Equipment** — helm, chest, gloves, pants, boots, main hand,
  off-hand. Each slot may hold one piece independently of the others.

Filename conventions live here too: ``CLASS_PREFIX`` / ``GENDER_SUFFIX``
build the per-class+gender prefix used to filter CASC paths
(``"barF"``, ``"necM"``, etc.). Note that the equipment-hashing skill
lists Spiritborn as ``"spt"``; the actual game data uses ``"spi"`` —
the mapping below reflects what's on disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CustomizationField(Enum):
    CLASS = "class"
    GENDER = "gender"
    FACE = "face"
    HAIR_STYLE = "hair_style"
    FACIAL_HAIR = "facial_hair"
    JEWELRY = "jewelry"


# Display order in the slot panel. Class and gender come first because
# the rest of the chain is gated on them.
CUSTOMIZATION_DISPLAY: tuple[tuple[CustomizationField, str], ...] = (
    (CustomizationField.CLASS, "Class"),
    (CustomizationField.GENDER, "Gender"),
    (CustomizationField.FACE, "Face"),
    (CustomizationField.HAIR_STYLE, "Hair Style"),
    (CustomizationField.FACIAL_HAIR, "Facial Hair"),
    (CustomizationField.JEWELRY, "Jewelry"),
)


# Customization fields whose values are loaded as separate .app files.
# Class and gender are *selectors* for the rest of the chain — they
# don't have an SNO path of their own.
OPTION_FIELDS: tuple[CustomizationField, ...] = (
    CustomizationField.FACE,
    CustomizationField.HAIR_STYLE,
    CustomizationField.FACIAL_HAIR,
    CustomizationField.JEWELRY,
)


CHARACTER_CLASSES: tuple[str, ...] = (
    "Barbarian",
    "Druid",
    "Necromancer",
    "Paladin",
    "Rogue",
    "Sorcerer",
    "Spiritborn",
    "Warlock",
)


GENDERS: tuple[str, ...] = ("Male", "Female")


CLASS_PREFIX: dict[str, str] = {
    "Barbarian": "bar",
    "Druid": "dru",
    "Necromancer": "nec",
    "Paladin": "pal",
    "Rogue": "rog",
    "Sorcerer": "sor",
    "Spiritborn": "spi",
    "Warlock": "war",
}


GENDER_SUFFIX: dict[str, str] = {"Male": "M", "Female": "F"}


EQUIPMENT_SLOTS: tuple[tuple[str, str], ...] = (
    ("hlm", "Helm"),
    ("bdy", "Chest"),
    ("glv", "Gloves"),
    ("leg", "Pants"),
    ("bts", "Boots"),
    ("mh",  "Main Hand"),
    ("oh",  "Off-Hand"),
)


@dataclass
class CustomizationOption:
    """One selectable option in a customization dropdown."""

    display_name: str
    sno_path: str
    file_key: str = ""       # H## / P## / B## identifier, when known
    mesh_required: bool = True  # False for material-only options (Clean, Stubble)


@dataclass
class CustomizationSlotState:
    """State of one option-bearing customization dropdown."""

    field: CustomizationField
    display_name: str
    available_options: list[CustomizationOption] = field(default_factory=list)
    selected_index: int = -1

    @property
    def selected_option(self) -> CustomizationOption | None:
        if 0 <= self.selected_index < len(self.available_options):
            return self.available_options[self.selected_index]
        return None

    @property
    def selected_sno(self) -> str | None:
        opt = self.selected_option
        return opt.sno_path if opt is not None else None


@dataclass
class SlotState:
    slot_key: str
    display_name: str
    equipped_sno: str | None = None
    equipped_name: str | None = None

    @property
    def is_filled(self) -> bool:
        return self.equipped_sno is not None
