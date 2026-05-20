"""N-panel UI for the D4Extract importer."""

from __future__ import annotations

import bpy

from .n_panel import D4_PT_main

_CLASSES = (D4_PT_main,)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
