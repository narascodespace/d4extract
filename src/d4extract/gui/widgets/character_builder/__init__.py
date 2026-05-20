"""Character Builder tab widgets.

The builder page wires the slot panel's signals to the piece browser
and populates customization dropdowns from static definition tables.
"""

from d4extract.gui.widgets.character_builder.assembly_parts_panel import (
    AssemblyPartsPanel,
    PieceEntry,
)
from d4extract.gui.widgets.character_builder.builder_page import (
    CharacterBuilderPage,
)
from d4extract.gui.widgets.character_builder.customization_defs import (
    FACE_DEFS,
    FACIAL_HAIR_DEFS,
    HAIR_DEFS,
    CustomizationDef,
    get_options_for_class,
)
from d4extract.gui.widgets.character_builder.models import (
    CHARACTER_CLASSES,
    CLASS_PREFIX,
    CUSTOMIZATION_DISPLAY,
    EQUIPMENT_SLOTS,
    GENDER_SUFFIX,
    GENDERS,
    OPTION_FIELDS,
    CustomizationField,
    CustomizationOption,
    CustomizationSlotState,
    SlotState,
)
from d4extract.gui.widgets.character_builder.piece_browser import PieceBrowser
from d4extract.gui.widgets.character_builder.slot_panel import SlotPanel

__all__ = [
    "AssemblyPartsPanel",
    "CHARACTER_CLASSES",
    "CLASS_PREFIX",
    "CUSTOMIZATION_DISPLAY",
    "CharacterBuilderPage",
    "CustomizationDef",
    "CustomizationField",
    "CustomizationOption",
    "CustomizationSlotState",
    "EQUIPMENT_SLOTS",
    "FACE_DEFS",
    "FACIAL_HAIR_DEFS",
    "GENDER_SUFFIX",
    "GENDERS",
    "HAIR_DEFS",
    "OPTION_FIELDS",
    "PieceBrowser",
    "PieceEntry",
    "SlotPanel",
    "SlotState",
    "get_options_for_class",
]
