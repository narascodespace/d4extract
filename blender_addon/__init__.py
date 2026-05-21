"""D4Extract Importer — Blender addon entry point.

Imports Diablo IV models exported by d4extract (.glb + .materials.json
sidecar), rebuilds their PBR materials, forces dithered alpha, and
exposes the Skin / Hair Color customization dropdowns in the 3D View
N-panel ("D4 Tools" tab).

Packaged as a Blender 4.2+ extension (see ``blender_manifest.toml``).
The ``bl_info`` block below is kept only as a legacy-addon fallback.
"""

from __future__ import annotations

import bpy

from . import preferences, properties
from . import operators as _operators
from . import panels as _panels
from .operators.import_d4 import menu_func_import

bl_info = {
    "name": "D4Extract Importer",
    "blender": (4, 2, 0),          # surface_render_method requires 4.2+
    "version": (0, 2, 0),
    "category": "Import-Export",
    "description": "Import and tune Diablo IV models exported by d4extract.",
    "author": "diablo4analyzer",
    "doc_url": "",
}


def register() -> None:
    preferences.register()
    properties.register()
    _operators.register()
    _panels.register()
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister() -> None:
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    _panels.unregister()
    _operators.unregister()
    properties.unregister()
    preferences.unregister()


if __name__ == "__main__":
    register()
