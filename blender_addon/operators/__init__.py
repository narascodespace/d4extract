"""Operators for the D4Extract importer.

``register``/``unregister`` here register every operator class so the
addon's top-level ``__init__`` only has to call these two.
"""

from __future__ import annotations

import bpy

from .apply_variants import D4_OT_apply_variants
from .import_d4 import IMPORT_SCENE_OT_d4
from .set_dithered import D4_OT_set_dithered
from .setup_materials import D4_OT_setup_materials

_CLASSES = (
    IMPORT_SCENE_OT_d4,
    D4_OT_setup_materials,
    D4_OT_set_dithered,
    D4_OT_apply_variants,
)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
