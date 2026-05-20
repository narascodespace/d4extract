"""Addon preferences — optional defaults for the D4Extract importer."""

from __future__ import annotations

import bpy

# ``__package__`` of this submodule is the addon's top-level package
# name, which is exactly what ``AddonPreferences.bl_idname`` and the
# ``context.preferences.addons[...]`` lookup both expect.
ADDON_PACKAGE = __package__


class D4AddonPreferences(bpy.types.AddonPreferences):
    """User-configurable defaults, surfaced under Edit > Preferences."""

    bl_idname = ADDON_PACKAGE

    apply_backface_culling: bpy.props.BoolProperty(
        name="Enable backface culling on setup",
        description=(
            "When Setup PBR Materials runs, also turn on backface "
            "culling for each material. Off by default — D4 cloth and "
            "hair are authored double-sided"
        ),
        default=False,
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.prop(self, "apply_backface_culling")


def get_prefs(context: bpy.types.Context) -> D4AddonPreferences | None:
    """Return this addon's preferences, or ``None`` if unavailable."""
    addon = context.preferences.addons.get(ADDON_PACKAGE)
    return addon.preferences if addon is not None else None


def register() -> None:
    bpy.utils.register_class(D4AddonPreferences)


def unregister() -> None:
    bpy.utils.unregister_class(D4AddonPreferences)
