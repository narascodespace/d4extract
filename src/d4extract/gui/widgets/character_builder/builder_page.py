"""Top-level page widget for the Character Builder tab.

Three-panel splitter mirroring the Model Browser's layout: a slot
panel on the left, a dedicated PyVistaQt viewport in the centre, and a
piece browser on the right. The viewport here is a *separate*
``ViewportWidget`` instance from the browser's — the Character Builder
needs its own scene that won't be clobbered when the user clicks
around in the Model Browser.

This page is also the wiring hub that connects the SlotPanel's
selectors to the PieceBrowser's filters and routes piece clicks back
into the SlotPanel as ``set_equipped`` calls. Multi-piece assembly
runs here too: each equipped piece is loaded via ``LoadModelWorker``,
cached, and merged into a single ``MeshData`` rendered in the
builder's dedicated viewport.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

from d4extract.formats.app_parser import MeshData, Skeleton
from d4extract.gui import anim_lookup, item_lookup
from d4extract.gui.widgets.animation_bar import AnimationBar, REST_POSE_LABEL
from d4extract.gui.widgets.character_builder.assembly import (
    merge_pieces,
    select_assembly_group,
)
from d4extract.gui.widgets.character_builder.assembly_parts_panel import (
    AssemblyPartsPanel,
    PieceEntry,
)
from d4extract.gui.widgets.character_builder.customization_defs import (
    FACE_DEFS,
    FACIAL_HAIR_DEFS,
    HAIR_DEFS,
    get_options_for_class,
)
from d4extract.gui.widgets.character_builder.models import (
    CLASS_PREFIX,
    CUSTOMIZATION_DISPLAY,
    EQUIPMENT_SLOTS,
    GENDER_SUFFIX,
    CustomizationField,
    CustomizationOption,
)
from d4extract.gui.widgets.character_builder.piece_browser import PieceBrowser
from d4extract.gui.widgets.character_builder.slot_panel import SlotPanel
from d4extract.gui.widgets.viewport import ViewportWidget
from d4extract.gui.workers.anim_worker import AnimLoadWorker
from d4extract.gui.workers.load_worker import LoadModelWorker
from d4extract.gui.workers.texture_worker import TextureWorker

if TYPE_CHECKING:
    from d4extract.formats.anim_parser import DecodedAnimation
    from d4extract.gui.anim_lookup import AnimationInfo

log = logging.getLogger(__name__)

# Maps each option-bearing field to its definition table.
_FIELD_DEFS = {
    CustomizationField.FACE: FACE_DEFS,
    CustomizationField.HAIR_STYLE: HAIR_DEFS,
    CustomizationField.FACIAL_HAIR: FACIAL_HAIR_DEFS,
}


class _ViewportContainer(QWidget):
    """Viewport + animation bar stacked vertically for the builder.

    Mirrors the Model Browser's container (``main_window._ViewportContainer``)
    minus the PropertiesBar — the builder doesn't surface per-model
    properties. The :class:`AnimationBar` sits under the viewport and
    stays hidden until a skinned base body is assembled.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.viewport = ViewportWidget(self)
        self.animation_bar = AnimationBar(self)
        self.animation_bar.setVisible(False)

        layout.addWidget(self.viewport, 1)
        layout.addWidget(self.animation_bar, 0)


class CharacterBuilderPage(QWidget):
    """Three-panel character builder page with signal wiring."""

    # Fires whenever the assembled scene changes (piece equipped /
    # cleared / class-reset). Payload is ``True`` when the builder
    # currently has at least one renderable piece — which is also when
    # the export button should be enabled while the builder tab is
    # active. Main window listens to keep the status-bar export button
    # in sync without having to poll.
    assembly_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Game context — set lazily by ``set_game_context``. Until then
        # any piece-load attempt is a no-op (the SlotPanel is reachable
        # before the catalog is ready, but selecting a piece can't do
        # anything useful without a game directory).
        self._game_dir: Path | None = None
        self._d4data_path: Path | None = None
        self._mock_mode: bool = False

        # Per-piece parsed-mesh cache, keyed by SNO path. Re-equipping a
        # piece the user has previously selected this session reuses the
        # parsed MeshData rather than re-spawning a worker.
        self._mesh_cache: dict[str, MeshData] = {}
        # In-flight load workers, keyed by SNO path. Lets us tell whether
        # a piece is already being loaded (no need for a duplicate
        # worker) and lets ``shutdown`` cancel everything cleanly.
        self._active_workers: dict[str, LoadModelWorker] = {}

        # Per-piece decoded base-color textures, keyed by SNO path. Each
        # value is the per-piece ``{piece_submesh_idx: ndarray}`` dict
        # the TextureWorker emits — caching here means re-equipping a
        # piece skips both the parse AND the texture decode. The
        # per-piece submesh indices are remapped to merged-submesh
        # indices in ``_apply_assembly_textures``.
        self._texture_cache: dict[str, dict[int, np.ndarray]] = {}
        # In-flight texture workers, keyed by SNO path.
        self._active_texture_workers: dict[str, TextureWorker] = {}
        # The last successful merge result. Held so a TextureWorker
        # whose result lands after ``_rebuild_assembly`` can remap its
        # per-piece submesh indices into the merged submesh space —
        # without these we'd need to re-merge every time a texture
        # arrived just to know the offsets.
        self._last_merged_mesh: MeshData | None = None
        self._last_merged_pieces: list[MeshData] = []
        # Static overlay pieces from the last rebuild. Each entry maps
        # the viewport tag (e.g. "weapon_0") → the SNO path so textures
        # landing later can be routed to ``apply_overlay_textures``.
        self._overlay_tag_to_sno: dict[str, str] = {}

        # Animation state — discovery is keyed on the class+gender
        # prefix (e.g. "barM"). The animation index is keyed by
        # canonical skeleton appearance, and a single character's clips
        # are split across several of those appearances, so a prefix
        # union is the correct lookup. Equipping armor must not reset
        # playback — only a class/gender change does. The cache is
        # keyed by prefix; ``_current_character_prefix`` is the prefix
        # the bar is currently bound to.
        self._anim_discovery_cache: dict[str, list[AnimationInfo]] = {}
        self._anim_index_ready: bool = False
        self._active_anim_load: AnimLoadWorker | None = None
        self._current_animation: DecodedAnimation | None = None
        self._current_animation_info: AnimationInfo | None = None
        self._current_animation_permutation: int = 0
        self._current_animation_frame: int = 0
        self._current_animation_was_playing: bool = False
        self._current_character_prefix: str | None = None
        # Prefix captured when the in-flight AnimLoadWorker was started
        # — lets the load callback drop a decode whose character
        # changed under it.
        self._anim_load_prefix: str | None = None
        # True while playback was auto-paused because the user switched
        # away from the builder tab — resume on return.
        self._anim_paused_for_tab: bool = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setHandleWidth(8)
        splitter.setChildrenCollapsible(False)

        self.slot_panel = SlotPanel(splitter)
        self.slot_panel.setMinimumWidth(200)
        splitter.addWidget(self.slot_panel)

        self.viewport_container = _ViewportContainer(splitter)
        self.viewport_container.setMinimumWidth(400)
        splitter.addWidget(self.viewport_container)

        self.piece_browser = PieceBrowser(splitter)
        self.piece_browser.setMinimumWidth(180)
        splitter.addWidget(self.piece_browser)

        self.assembly_parts_panel = AssemblyPartsPanel(splitter)
        self.assembly_parts_panel.setMinimumWidth(200)
        splitter.addWidget(self.assembly_parts_panel)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 6)
        splitter.setStretchFactor(2, 1)
        splitter.setStretchFactor(3, 2)
        splitter.setSizes([250, 500, 100, 200])

        self._splitter = splitter
        layout.addWidget(splitter)

        self._connect_signals()

        # Equipment pieces are labelled by their in-game name wherever
        # the equipment name index has resolved one; the resolver falls
        # back to the SNO stem otherwise. Wired here so the PieceBrowser
        # shows names as soon as a slot is browsed.
        self.piece_browser.set_name_resolver(self._display_name_for)

        # Show the top-left notice reminding users that skin, eye
        # color, markings, and makeup are handled by the Blender addon.
        self.viewport.set_blender_notice_visible(True)

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self.slot_panel.class_changed.connect(self._on_class_changed)
        self.slot_panel.gender_changed.connect(self._on_gender_changed)
        self.slot_panel.slot_selected.connect(self._on_slot_selected)
        self.slot_panel.customization_changed.connect(
            self._on_customization_changed,
        )
        self.piece_browser.piece_selected.connect(self._on_piece_selected)

        # Parts panel → viewport. The panel translates per-row toggles
        # into either merged-stream global indices or overlay
        # (tag, local index) pairs before emitting, so the viewport
        # methods can be wired direct without further translation.
        self.assembly_parts_panel.merged_visibility_changed.connect(
            self.viewport.set_submesh_visible,
        )
        self.assembly_parts_panel.overlay_visibility_changed.connect(
            self.viewport.set_overlay_submesh_visible,
        )
        self.assembly_parts_panel.merged_bulk_visibility_changed.connect(
            self.viewport.set_all_submeshes_visible,
        )
        self.assembly_parts_panel.overlay_bulk_visibility_changed.connect(
            self.viewport.set_all_overlay_submeshes_visible,
        )

        # Animation bar → builder. Mirrors the browser wiring in
        # ``main_window._build_browser_tab``.
        anim_bar = self.viewport_container.animation_bar
        anim_bar.animation_selected.connect(self._on_animation_selected)
        anim_bar.permutation_changed.connect(
            self._on_animation_permutation_changed,
        )
        anim_bar.frame_changed.connect(self._on_animation_frame_changed)
        anim_bar.play_toggled.connect(self._on_animation_play_toggled)

    def _on_class_changed(self, class_name: str) -> None:
        # Class change in the slot panel resets gender + slot below
        # it, so the browser should clear its filters too. The next
        # gender_changed signal will install the new prefix.
        self.piece_browser.set_class_gender_display(class_name, None)
        self.piece_browser.set_class_gender_filter(None)
        self.piece_browser.set_slot_filter(None)
        # A new class means a new skeleton template — every cached mesh
        # is now incompatible. Cancel everything in flight, drop the
        # cache, and clear the viewport so the user gets a fresh start.
        self._cancel_all_workers()
        self._cancel_all_texture_workers()
        self._mesh_cache.clear()
        self._texture_cache.clear()
        self._last_merged_mesh = None
        self._last_merged_pieces = []
        self._overlay_tag_to_sno.clear()
        self.assembly_parts_panel.clear()
        # A new class is a new skeleton — drop any discovered animations
        # and hide the transport until a base body is rebuilt.
        self._reset_anim_state(hide_bar=True)
        self.viewport.show_hint()
        self.assembly_changed.emit(False)

    def _on_gender_changed(self, class_name: str, gender: str) -> None:
        new_prefix = self.slot_panel.get_class_gender_prefix()
        self.piece_browser.set_class_gender_display(class_name, gender)
        self.piece_browser.set_class_gender_filter(new_prefix)
        # Gender change preserves the slot selection in the slot panel,
        # but the previous prefix is gone — re-apply the current slot
        # so the filter pool refreshes against the new prefix.
        self.piece_browser.set_slot_filter(self.slot_panel.get_selected_slot())

        # Drop cached entries whose filenames don't match the new
        # class+gender prefix — those came from the previous gender's
        # pool and won't be re-equipped. Weapons (gender-neutral) and
        # any piece whose stem already matches the new prefix stay.
        if new_prefix is not None:
            self._evict_cache_for_prefix(new_prefix)
            self._evict_texture_cache_for_prefix(new_prefix)

        # Try to swap each equipped armor piece to the new gender's
        # equivalent. Weapons (mh/oh) are gender-neutral so they stay.
        self._swap_equipped_gender(class_name, gender)

        # Populate Face / Hair Style / Facial Hair dropdowns from the
        # catalog now that we know both class and gender.
        self._populate_customization_options(class_name, gender)

        # Equipment swap / customization repopulation may have changed
        # what's currently equipped — repaint the assembly to match.
        self._rebuild_assembly()

    def _on_slot_selected(self, slot_key: str) -> None:
        self.piece_browser.set_slot_filter(slot_key)

    def _on_piece_selected(self, sno_path: str) -> None:
        slot_key = self.slot_panel.get_selected_slot()
        if slot_key is None:
            # Defensive: the search field is disabled until a slot is
            # selected, so this branch shouldn't be reachable from the
            # UI. Logged rather than raised so a stray signal doesn't
            # crash the app.
            log.debug(
                "piece_selected fired with no active slot: %s", sno_path,
            )
            return
        self.slot_panel.set_equipped(
            slot_key, sno_path, self._display_name_for(sno_path),
        )
        self._ensure_piece_loaded(sno_path)

    def _on_customization_changed(
        self, field: str, display: str, sno_path: str,
    ) -> None:
        """Handle Face / Hair / Facial Hair selection.

        Material-only options (Clean, Stubble) carry an empty SNO path
        — there's no mesh to load, but we still rebuild the assembly so
        a previously-selected mesh from the same field is removed.
        """
        if not sno_path:
            # Material-only selection: nothing to load, but the prior
            # mesh in this customization slot has already been replaced
            # in the SlotPanel state, so the rebuild will drop it.
            log.info(
                "Customization '%s' = %s — material-only, no mesh "
                "(visible after Blender export).",
                field, display,
            )
            self._rebuild_assembly()
            return
        self._ensure_piece_loaded(sno_path)

    # ------------------------------------------------------------------
    # Customization population
    # ------------------------------------------------------------------

    def _populate_customization_options(
        self, class_name: str, gender: str,
    ) -> None:
        """Build Face/Hair/Beard/Jewelry dropdowns from definition tables.

        Each option is cross-referenced against the CASC catalog to
        resolve the actual SNO path (``{prefix}_{suffix}.app``).
        Material-only options (Clean, Stubble) have no mesh and get an
        empty SNO path — they'll still appear in the dropdown so the
        user can select them, and the viewport phase will handle them
        by hiding the corresponding mesh layer.

        Facial Hair is cleared for Female characters since the game
        ships no female facial-hair meshes and the SlotPanel already
        hides that row.

        Jewelry uses a reversed naming convention
        (``jwl##_{prefix}.app`` instead of ``{prefix}_##.app``) and is
        discovered dynamically from the catalog rather than from
        static definition tables.
        """
        prefix = self.slot_panel.get_class_gender_prefix()
        if prefix is None:
            return

        # Build a quick lookup: file_suffix (e.g. "P00", "H09", "B02")
        # → full CASC SNO path for the current class+gender.
        prefix_lower = prefix.lower() + "_"
        catalog_by_suffix: dict[str, str] = {}
        # Jewelry entries: jwl##_{prefix}.app — indexed separately.
        jewelry_entries: list[tuple[str, str]] = []  # (variant "00", sno_path)

        for entry in self.piece_browser.all_entries:
            filename = _basename(entry)
            fn_lower = filename.lower()
            # Jewelry: jwl##_{prefix}.app
            if fn_lower.startswith("jwl"):
                stem = filename
                if fn_lower.endswith(".app"):
                    stem = stem[:-4]
                # Check that this jewelry piece is for the current
                # class+gender (suffix after last "_").
                last_us = stem.rfind("_")
                if last_us >= 0:
                    jwl_suffix = stem[last_us + 1:]
                    if jwl_suffix.lower() == prefix.lower():
                        # Extract variant number from the jwl prefix.
                        variant = stem[3:last_us]  # e.g. "00", "31"
                        jewelry_entries.append((variant, entry))
                continue
            if not fn_lower.startswith(prefix_lower):
                continue
            # Extract suffix: everything between the last "_" and ".app"
            stem = filename
            if stem.lower().endswith(".app"):
                stem = stem[:-4]
            last_us = stem.rfind("_")
            if last_us >= 0:
                suffix = stem[last_us + 1:]  # "P00", "H09", "B02", etc.
                catalog_by_suffix[suffix.upper()] = entry

        for field, defs_table in _FIELD_DEFS.items():
            if field is CustomizationField.FACIAL_HAIR and gender != "Male":
                self.slot_panel.populate_options(field, [])
                continue

            class_defs = get_options_for_class(defs_table, class_name)
            options: list[CustomizationOption] = []

            for d in class_defs:
                if d.has_mesh:
                    sno_path = catalog_by_suffix.get(d.file_suffix, "")
                    if not sno_path:
                        # Mesh expected but not found in catalog — skip
                        # rather than show an unloadable option.
                        log.debug(
                            "Mesh not found for %s %s (%s)",
                            class_name, d.display_name, d.file_suffix,
                        )
                        continue
                else:
                    # Material-only option (e.g. Clean, Stubble).
                    sno_path = ""

                options.append(
                    CustomizationOption(
                        display_name=d.display_name,
                        sno_path=sno_path,
                        file_key=d.file_suffix,
                        mesh_required=d.has_mesh,
                    ),
                )

            self.slot_panel.populate_options(field, options)

        # Jewelry — dynamically discovered from catalog.
        jewelry_entries.sort(key=lambda t: t[0])
        jwl_options: list[CustomizationOption] = []
        for variant, sno_path in jewelry_entries:
            # Display as "Jewelry 01", "Jewelry 02", etc. (1-based for
            # the user). Variant "00" becomes "Jewelry 01".
            try:
                num = int(variant) + 1
            except ValueError:
                num = 0
            display = f"Jewelry {num:02d}"
            jwl_options.append(
                CustomizationOption(
                    display_name=display,
                    sno_path=sno_path,
                    file_key=f"jwl{variant}",
                    mesh_required=True,
                ),
            )
        self.slot_panel.populate_options(
            CustomizationField.JEWELRY, jwl_options,
        )

    # ------------------------------------------------------------------
    # Gender swap
    # ------------------------------------------------------------------

    # Weapon / off-hand slots are gender-neutral — don't touch them.
    _WEAPON_SLOTS = frozenset({"mh", "oh"})

    def _swap_equipped_gender(
        self, class_name: str, gender: str,
    ) -> None:
        """Swap each equipped armor piece to the new gender's equivalent.

        For every filled equipment slot (except weapons), rewrite the
        SNO path's class+gender prefix from the old gender to the new
        one. If the rewritten path exists in the catalog, re-equip it;
        otherwise clear the slot.
        """
        cls_prefix = CLASS_PREFIX.get(class_name)
        new_suffix = GENDER_SUFFIX.get(gender)
        if cls_prefix is None or new_suffix is None:
            return

        new_prefix = f"{cls_prefix}{new_suffix}"
        # Build a regex that matches any gender variant of this class's
        # prefix at the start of the filename component.
        # e.g. for Barbarian → matches "barM_" or "barF_"
        swap_rx = re.compile(
            rf"^({re.escape(cls_prefix)})[MF](?=_)",
            re.IGNORECASE,
        )

        build = self.slot_panel.get_build()
        for slot_key, sno_path in build.items():
            if sno_path is None or slot_key in self._WEAPON_SLOTS:
                continue

            # Split into directory + filename, swap the prefix in the
            # filename, then rejoin.
            if "/" in sno_path:
                directory, filename = sno_path.rsplit("/", 1)
            else:
                directory, filename = "", sno_path

            new_filename = swap_rx.sub(
                rf"\g<1>{new_suffix}", filename, count=1,
            )

            if new_filename == filename:
                # Prefix didn't match — unusual, but leave the slot as-is
                # rather than clearing something we don't understand.
                continue

            new_sno = f"{directory}/{new_filename}" if directory else new_filename

            if self.piece_browser.has_entry(new_sno):
                self.slot_panel.set_equipped(
                    slot_key, new_sno, self._display_name_for(new_sno),
                )
            else:
                self.slot_panel.clear_slot(slot_key)

    # ------------------------------------------------------------------
    # Piece loading and assembly
    # ------------------------------------------------------------------

    def _ensure_piece_loaded(self, sno_path: str) -> None:
        """Make sure ``sno_path`` is in the cache, then rebuild assembly.

        Cache hit: rebuild immediately.
        In-flight: nothing to do — the existing worker will trigger the
        rebuild when it finishes.
        Cache miss: spawn a ``LoadModelWorker`` and wait.
        """
        if not sno_path:
            return
        if sno_path in self._mesh_cache:
            self._rebuild_assembly()
            # Cache hit on geometry but textures may have errored out
            # or never been kicked (e.g. d4data path was set after the
            # piece was first loaded). The kick is a no-op when the
            # textures are already cached or in flight.
            self._kick_texture_worker(sno_path, self._mesh_cache[sno_path])
            return
        if sno_path in self._active_workers:
            # Worker already running — its finished handler will rebuild.
            return
        if not self._mock_mode and self._game_dir is None:
            # No game directory configured yet. Surface the issue rather
            # than spawning a worker that will just emit ``error``.
            self.viewport.show_error(
                "Configure the game directory before equipping pieces."
            )
            return
        self._spawn_worker(sno_path)

    def _spawn_worker(self, sno_path: str) -> None:
        worker = LoadModelWorker(
            self._game_dir, sno_path,
            mock=self._mock_mode,
            d4data_path=self._d4data_path,
            parent=self,
        )
        worker.finished.connect(
            lambda mesh, p=sno_path: self._on_worker_finished(p, mesh),
        )
        worker.error.connect(
            lambda msg, p=sno_path: self._on_worker_error(p, msg),
        )
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._active_workers[sno_path] = worker
        # Surface a loading state when the assembly is currently empty —
        # otherwise let the existing scene stay on screen so equipping a
        # second piece doesn't blank the viewport mid-load.
        if not self._has_renderable_pieces():
            self.viewport.show_loading(
                f"Loading {self._display_name_for(sno_path)}…"
            )
        # Show (or update) the floating model-loading indicator with the
        # current count of in-flight workers.
        self._update_model_loading_indicator()
        worker.start()

    def _on_worker_finished(self, sno_path: str, mesh_data) -> None:
        # Discard if the user changed class while the worker was running
        # — ``_cancel_all_workers`` removes the entry from the dict, so
        # a stale signal landing afterwards must not poison the cache.
        if sno_path not in self._active_workers:
            return
        self._active_workers.pop(sno_path, None)
        self._update_model_loading_indicator()
        if isinstance(mesh_data, MeshData):
            self._mesh_cache[sno_path] = mesh_data
        else:
            log.warning(
                "LoadModelWorker returned non-MeshData for %s: %r",
                sno_path, type(mesh_data).__name__,
            )
            return
        self._rebuild_assembly()
        # Kick the texture pipeline for this piece. Best-effort: skipped
        # silently in mock mode or when no d4data path is configured.
        self._kick_texture_worker(sno_path, mesh_data)

    def _on_worker_error(self, sno_path: str, message: str) -> None:
        log.warning("Failed to load %s: %s", sno_path, message)
        self._active_workers.pop(sno_path, None)
        self._update_model_loading_indicator()
        # Show the error briefly only when nothing else is on screen —
        # otherwise the existing assembly is more useful than the
        # message. The rebuild call below paints whichever state is
        # appropriate (existing pieces, hint, or this error).
        if not self._has_renderable_pieces():
            self.viewport.show_error(f"Failed to load piece:\n{message}")
            return
        self._rebuild_assembly()

    def _rebuild_assembly(self) -> None:
        """Merge every cached piece into one MeshData and render it.

        Skinned pieces (those with a skeleton) are merged via
        ``merge_pieces`` under a shared skeleton.  Static pieces (no
        skeleton — typically weapons) are rendered as overlay actors on
        top of the merged body so they appear in the viewport without
        being forced into the skinning pipeline.
        """
        equipped = self._collect_equipped_paths()
        ready = [
            self._mesh_cache[p] for p in equipped if p in self._mesh_cache
        ]
        if not ready:
            if self._active_workers:
                # First piece is still loading — leave the loading
                # overlay alone.
                return
            self._last_merged_mesh = None
            self._last_merged_pieces = []
            self.assembly_parts_panel.clear()
            self._update_animation_after_rebuild()
            self.viewport.show_hint()
            self.assembly_changed.emit(False)
            return

        # Partition into skinned (body/armor/face/hair) and static
        # (weapons, props).
        skinned = [p for p in ready if p.skeleton is not None]
        static = [p for p in ready if p.skeleton is None]
        if static:
            log.info(
                "Assembly: %d skinned + %d static (unskinned) pieces. "
                "Static: %s",
                len(skinned), len(static),
                ", ".join(p.name for p in static),
            )

        if skinned:
            merged, included = merge_pieces(skinned, name="character")
        else:
            merged, included = None, []

        if merged is None and not static:
            self._last_merged_mesh = None
            self._last_merged_pieces = []
            self.assembly_parts_panel.clear()
            self._update_animation_after_rebuild()
            self.viewport.show_hint()
            self.assembly_changed.emit(False)
            return

        if merged is not None:
            try:
                self.viewport.load_mesh(merged)
            except Exception as exc:
                log.exception("Builder viewport load_mesh failed")
                self.viewport.show_error(f"Failed to render: {exc}")
                return
        elif static:
            # Only static pieces (no skinned body yet) — load the first
            # static piece as the primary scene so the viewport clears
            # any prior state and resets the camera.
            first = static[0]
            try:
                self.viewport.load_mesh(first)
            except Exception as exc:
                log.exception("Builder viewport load_mesh failed (static)")
                self.viewport.show_error(f"Failed to render: {exc}")
                return
            static = static[1:]  # remainder handled as overlays

        self._last_merged_mesh = merged
        self._last_merged_pieces = included

        # Overlay static pieces (weapons) onto the scene.
        self.viewport.remove_overlay_actors("weapon")
        self._overlay_tag_to_sno.clear()
        for i, piece in enumerate(static):
            tag = f"weapon_{i}"
            sno_path = self._sno_for_piece(piece)
            try:
                self.viewport.add_overlay_mesh(piece, tag=tag)
            except Exception as exc:
                log.warning(
                    "Failed to overlay static piece %s: %s",
                    piece.name, exc,
                )
                continue
            if sno_path:
                self._overlay_tag_to_sno[tag] = sno_path

        # Re-apply any per-piece textures already in cache so a rebuild
        # triggered by an unrelated piece's load doesn't drop the
        # textures that landed earlier.
        self._apply_assembly_textures()
        self._apply_overlay_textures()

        # Repopulate the parts panel last so it sees the final overlay
        # set + tag mapping. The viewport just rebuilt every actor in
        # the "visible" state, and the panel rebuilds every row in the
        # "checked" state — so the two start in sync without an
        # explicit push. Subsequent texture arrivals only call
        # ``update_material_names`` on the existing rows; that path's
        # cloth-proxy auto-hide fires per-row toggles which propagate
        # through to the viewport via the wired signals.
        self._populate_parts_panel()

        # Reconcile the animation bar with the rebuilt assembly: hide
        # it for a weapons-only scene, re-bind a running animation when
        # only equipment changed, or re-discover when the FACE changed.
        # Runs after ``load_mesh`` (which wipes viewport animation state)
        # so any re-bind lands on the freshly-built actors.
        self._update_animation_after_rebuild()

        # Tell the main window that the export button can be enabled
        # while the builder tab is active. Anything renderable counts —
        # even a weapons-only scene exports as a single static mesh.
        self.assembly_changed.emit(True)

    def _collect_equipped_paths(self) -> list[str]:
        """Equipment + customization SNO paths in display-order.

        Empty / None entries are filtered out. Order is customization
        first then equipment so face/hair are merged before armor —
        matters only for diagnostic logs since the merge itself is
        order-independent.
        """
        out: list[str] = []
        for sno in self.slot_panel.get_customization_build().values():
            if sno:
                out.append(sno)
        for sno in self.slot_panel.get_build().values():
            if sno:
                out.append(sno)
        return out

    def _has_renderable_pieces(self) -> bool:
        for sno in self._collect_equipped_paths():
            if sno in self._mesh_cache:
                return True
        return False

    # ------------------------------------------------------------------
    # Worker lifecycle helpers
    # ------------------------------------------------------------------

    def _cancel_all_workers(self) -> None:
        """Cancel every in-flight load worker.

        Pops the dict first so any late-arriving ``finished`` /
        ``error`` signal from a worker we just told to stop is a no-op
        (``_on_worker_finished`` checks the dict for the path).
        """
        workers = list(self._active_workers.values())
        self._active_workers.clear()
        self.viewport.stop_model_loading()
        for worker in workers:
            try:
                if worker.isRunning():
                    worker.requestInterruption()
                    try:
                        worker.finished.disconnect()
                        worker.error.disconnect()
                    except (RuntimeError, TypeError):
                        pass
                    worker.quit()
                    worker.wait(2000)
            except RuntimeError:
                # C++ side already gone — nothing to cancel.
                continue

    # ------------------------------------------------------------------
    # Texture pipeline
    # ------------------------------------------------------------------

    def _kick_texture_worker(self, sno_path: str, mesh_data: MeshData) -> None:
        """Spawn a per-piece TextureWorker if pre-conditions are met.

        Mirrors the Model Browser's texture flow: skips silently in
        mock mode, when no game directory is set, or when no d4data
        path is configured. Each piece is textured against its own
        ``.app`` stem (the worker reads ``mesh_data.name``), so the
        per-piece submesh indices in the result are local — they're
        remapped to merged submesh indices in
        :meth:`_apply_assembly_textures`.
        """
        if self._mock_mode:
            return
        if self._game_dir is None or self._d4data_path is None:
            return
        if sno_path in self._texture_cache:
            return
        if sno_path in self._active_texture_workers:
            return

        worker = TextureWorker(
            mesh_data,
            game_dir=self._game_dir,
            d4data_path=self._d4data_path,
            mesh_token=mesh_data,
            sno_path=sno_path,
            parent=self,
        )
        worker.finished.connect(
            lambda mt, st, p=sno_path: self._on_textures_ready(p, mt, st),
        )
        worker.error.connect(
            lambda msg, p=sno_path: self._on_texture_error(p, msg),
        )
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._active_texture_workers[sno_path] = worker
        self._maybe_show_texture_indicator()
        worker.start()

    def _on_textures_ready(
        self,
        sno_path: str,
        mesh_token,
        submesh_textures: dict,
    ) -> None:
        # If the worker was cancelled (class change, shutdown), the dict
        # entry is already gone — drop the result so a stale decode
        # doesn't get applied to a different character's mesh.
        if sno_path not in self._active_texture_workers:
            return
        self._active_texture_workers.pop(sno_path, None)
        if submesh_textures:
            # Defensive copy: TextureWorker hands ownership over but the
            # cache key outlives any single rebuild.
            self._texture_cache[sno_path] = dict(submesh_textures)
        self._maybe_hide_texture_indicator()
        # The cached piece's geometry might already be in the merged
        # output; re-apply will pick it up via the offset table.
        self._apply_assembly_textures()
        # Static overlay pieces (weapons) have their own texture path.
        self._apply_overlay_textures()
        # Material resolution lands alongside the texture decode (the
        # worker resolves materials before the BC decode), so by the
        # time we get here ``mesh_token.materials`` is populated even
        # if no BASE_COLOR images came back. Push the names into the
        # parts panel so cloth-only rows auto-hide and labels switch
        # from the index fallback to the material name.
        materials = getattr(mesh_token, "materials", None)
        if materials is not None:
            self.assembly_parts_panel.update_material_names(
                sno_path, materials,
            )
    def _on_texture_error(self, sno_path: str, message: str) -> None:
        # Texturing is best-effort; log and move on. The piece keeps
        # its flat per-submesh colors.
        log.warning("Texture pass failed for %s: %s", sno_path, message)
        self._active_texture_workers.pop(sno_path, None)
        self._maybe_hide_texture_indicator()

    def _apply_assembly_textures(self) -> None:
        """Combine cached per-piece textures and push them to the viewport.

        Walks the pieces in the same order ``merge_pieces`` placed them
        in the merged output, accumulating a submesh offset so each
        piece's local submesh indices land at the correct merged index.
        Pieces whose textures haven't decoded yet are simply skipped —
        the viewport keeps their flat-color fallback until a later
        ``finished`` signal triggers another apply pass.
        """
        merged = self._last_merged_mesh
        if merged is None or not self._last_merged_pieces:
            return
        if not self._texture_cache:
            return

        combined: dict[int, np.ndarray] = {}
        submesh_base = 0
        for piece in self._last_merged_pieces:
            sno_path = self._sno_for_piece(piece)
            per_piece = (
                self._texture_cache.get(sno_path) if sno_path else None
            )
            if per_piece:
                for sm_idx, arr in per_piece.items():
                    combined[submesh_base + sm_idx] = arr
            submesh_base += len(piece.submeshes)

        if not combined:
            return
        try:
            self.viewport.apply_submesh_textures(merged, combined)
        except Exception:
            log.exception("apply_submesh_textures failed for assembly")

    def _apply_overlay_textures(self) -> None:
        """Push cached textures to static overlay (weapon) actors.

        Each overlay was added under a tag (``weapon_0``, ``weapon_1``,
        …) with a corresponding SNO path stored in
        ``_overlay_tag_to_sno``. The texture cache is keyed by SNO path
        so the lookup is straightforward — no submesh-offset remapping
        is needed because each overlay has its own actor namespace.
        """
        if not self._overlay_tag_to_sno:
            return
        if not self._texture_cache:
            return
        for tag, sno_path in self._overlay_tag_to_sno.items():
            per_piece = self._texture_cache.get(sno_path)
            if not per_piece:
                continue
            try:
                self.viewport.apply_overlay_textures(tag, per_piece)
            except Exception:
                log.exception(
                    "apply_overlay_textures failed for %s (%s)",
                    tag, sno_path,
                )

    def _populate_parts_panel(self) -> None:
        """Rebuild the assembly parts panel from the current scene.

        Walks ``_last_merged_pieces`` in order so the per-piece submesh
        offsets line up with the merged stream the viewport renders,
        then appends one entry per static overlay piece. Slot /
        customization labels are recovered by reverse-looking-up SNO
        paths against the slot panel's current build state.
        """
        # SNO → display label maps. Built from the slot panel's current
        # state because the merge order doesn't preserve slot identity
        # — pieces only know their own MeshData and SNO path, so we
        # need the slot side to supply the label prefix.
        slot_label: dict[str, str] = {}
        for slot_key, sno in self.slot_panel.get_build().items():
            if not sno:
                continue
            disp = next(
                (d for k, d in EQUIPMENT_SLOTS if k == slot_key),
                slot_key,
            )
            slot_label[sno] = disp

        custom_label: dict[str, str] = {}
        for field_value, sno in self.slot_panel.get_customization_build().items():
            if not sno:
                continue
            disp = next(
                (d for f, d in CUSTOMIZATION_DISPLAY if f.value == field_value),
                field_value,
            )
            custom_label[sno] = disp

        entries: list[PieceEntry] = []

        # Skinned merged pieces: accumulate the submesh offset as we
        # go so each entry's ``submesh_offset`` matches the merged
        # MeshData's global submesh ordering.
        submesh_offset = 0
        for piece in self._last_merged_pieces:
            sno_path = self._sno_for_piece(piece)
            n_subs = len(piece.submeshes)
            if sno_path is None:
                # Defensive: a piece in the merge that isn't in our
                # cache shouldn't happen, but if it does, advance the
                # offset so subsequent pieces still line up.
                submesh_offset += n_subs
                continue
            label_prefix = (
                slot_label.get(sno_path)
                or custom_label.get(sno_path)
                or "Piece"
            )
            entries.append(PieceEntry(
                display_name=f"{label_prefix} — {self._display_name_for(sno_path)}",
                sno_path=sno_path,
                submeshes=list(piece.submeshes),
                target_type="merged",
                submesh_offset=submesh_offset,
                overlay_tag=None,
            ))
            submesh_offset += n_subs

        # Static overlay pieces (weapons). Each has its own actor
        # namespace so the offset is always 0 — local indices map
        # directly to the actor name suffix.
        for tag, sno_path in self._overlay_tag_to_sno.items():
            piece = self._mesh_cache.get(sno_path)
            if piece is None:
                continue
            label_prefix = (
                slot_label.get(sno_path)
                or custom_label.get(sno_path)
                or "Weapon"
            )
            entries.append(PieceEntry(
                display_name=f"{label_prefix} — {self._display_name_for(sno_path)}",
                sno_path=sno_path,
                submeshes=list(piece.submeshes),
                target_type="overlay",
                submesh_offset=0,
                overlay_tag=tag,
            ))

        self.assembly_parts_panel.populate(entries)

        # Push any already-resolved materials into the new rows so
        # rebuilds triggered by a fresh equip don't blank out labels
        # that the texture worker resolved earlier in the session
        # (e.g. when re-equipping a cached piece, no fresh
        # ``_on_textures_ready`` fires).
        for entry in entries:
            piece = self._mesh_cache.get(entry.sno_path)
            if piece is None:
                continue
            materials = getattr(piece, "materials", None)
            if materials:
                self.assembly_parts_panel.update_material_names(
                    entry.sno_path, materials,
                )

    def _sno_for_piece(self, piece: MeshData) -> str | None:
        """Reverse-lookup an SNO path by ``MeshData`` identity.

        ``_mesh_cache`` is the source of truth; pieces in the merged
        output are the same Python objects we stashed there, so identity
        comparison is correct (and cheap — at most one entry per
        equipped slot).
        """
        for sno_path, cached in self._mesh_cache.items():
            if cached is piece:
                return sno_path
        return None

    def _cancel_all_texture_workers(self) -> None:
        workers = list(self._active_texture_workers.values())
        self._active_texture_workers.clear()
        for worker in workers:
            try:
                if worker.isRunning():
                    worker.requestInterruption()
                    try:
                        worker.finished.disconnect()
                        worker.error.disconnect()
                    except (RuntimeError, TypeError):
                        pass
                    worker.quit()
                    worker.wait(2000)
            except RuntimeError:
                continue
        self._maybe_hide_texture_indicator()

    def _evict_texture_cache_for_prefix(self, keep_prefix: str) -> None:
        """Mirror of ``_evict_cache_for_prefix`` for the texture cache."""
        if not self._texture_cache:
            return
        prefix_lower = keep_prefix.lower() + "_"
        stale: list[str] = []
        for sno_path in list(self._texture_cache):
            filename = _basename(sno_path).lower()
            if filename.startswith(prefix_lower):
                continue
            if (
                len(filename) >= 5
                and filename[3] in ("m", "f")
                and filename[4] == "_"
            ):
                stale.append(sno_path)
        for sno_path in stale:
            self._texture_cache.pop(sno_path, None)

    def _update_model_loading_indicator(self) -> None:
        """Start, update, or stop the model-loading indicator."""
        count = len(self._active_workers)
        if count > 0:
            self.viewport.start_model_loading(count)
        else:
            self.viewport.stop_model_loading()

    def _maybe_show_texture_indicator(self) -> None:
        """Show the floating spinner while any texture worker is busy."""
        if not self._active_texture_workers:
            return
        try:
            self.viewport.start_texture_loading("Resolving textures…")
        except Exception:
            log.debug("start_texture_loading failed", exc_info=True)

    def _maybe_hide_texture_indicator(self) -> None:
        """Hide the spinner once every texture worker has finished."""
        if self._active_texture_workers:
            return
        try:
            self.viewport.stop_texture_loading()
        except Exception:
            log.debug("stop_texture_loading failed", exc_info=True)

    # ------------------------------------------------------------------
    # Cache eviction
    # ------------------------------------------------------------------

    def _evict_cache_for_prefix(self, keep_prefix: str) -> None:
        """Drop cached pieces whose filename doesn't match ``keep_prefix``.

        Used on gender change to clear out the previous gender's
        face/hair/equipment without throwing away gender-neutral
        weapons (which already match no class+gender prefix and would
        be evicted unconditionally otherwise — but their stems start
        with weapon-class prefixes like ``wpn_``, not ``barM_``, so
        they stay because the prefix check is a prefix match against
        the class+gender prefix of the *current* selection).
        """
        if not self._mesh_cache:
            return
        prefix_lower = keep_prefix.lower() + "_"
        stale: list[str] = []
        for sno_path in list(self._mesh_cache):
            filename = _basename(sno_path).lower()
            # Keep weapons (their filenames don't start with a class
            # prefix at all) and pieces whose prefix already matches
            # the new gender. Anything starting with a different
            # class+gender prefix is now unreachable — drop it.
            if filename.startswith(prefix_lower):
                continue
            # Heuristic: a name beginning with three letters + 'M_' or
            # 'F_' is gendered class content. Anything else (weapons,
            # generic content) is left alone.
            if len(filename) >= 5 and filename[3] in ("m", "f") and filename[4] == "_":
                stale.append(sno_path)
        for sno_path in stale:
            self._mesh_cache.pop(sno_path, None)

    # ------------------------------------------------------------------
    # Animation — discovery, playback, persistence
    # ------------------------------------------------------------------

    def _character_prefix(self) -> str | None:
        """Class+gender prefix (e.g. 'barM') for animation discovery.

        Returns None if either selector is unset. Animations are
        indexed by canonical skeleton appearance — see
        ``anim_lookup.discover_by_prefix`` for why a prefix lookup is
        correct here rather than a per-piece appearance lookup.
        """
        cls = self.slot_panel.get_current_class()
        gen = self.slot_panel.get_current_gender()
        if not cls or not gen:
            return None
        cls_prefix = CLASS_PREFIX.get(cls)
        gen_suffix = GENDER_SUFFIX.get(gen)
        if not cls_prefix or not gen_suffix:
            return None
        return cls_prefix + gen_suffix

    def _character_prefix_pair(self) -> tuple[str, str] | None:
        """Class prefix combined with each gender, as (matched, other).

        Used to union both gender prefixes during animation discovery.
        D4's runtime fallback (snoFemaleOverrideAnim-not-present) means a
        character of any gender plays the other gender's animations
        whenever a gender-specific override doesn't exist — so we
        surface both in the builder, with collisions resolved in favor
        of the selected gender. See ``anim_lookup.union_genders``.

        Returns None if class or gender is unset.
        """
        cls = self.slot_panel.get_current_class()
        gen = self.slot_panel.get_current_gender()
        if not cls or not gen:
            return None
        cls_prefix = CLASS_PREFIX.get(cls)
        if not cls_prefix:
            return None
        matched_suffix = GENDER_SUFFIX.get(gen)
        if not matched_suffix:
            return None
        # The "other" gender is whichever isn't selected.
        other_gen = "Male" if gen == "Female" else "Female"
        other_suffix = GENDER_SUFFIX.get(other_gen)
        if not other_suffix:
            return None
        return (cls_prefix + matched_suffix, cls_prefix + other_suffix)

    def _has_base_body(self) -> bool:
        """True when a FACE customization is selected.

        A FACE selection is what puts a skinned base body into the
        assembly; without it there is nothing for an animation to
        drive even when class+gender (and thus a prefix) are set.
        """
        return bool(self.slot_panel.get_customization_build().get("face"))

    def _update_animation_after_rebuild(self) -> None:
        """Reconcile the animation bar with a freshly rebuilt assembly.

        Three outcomes, keyed on the class+gender prefix: no skinned
        base body hides the transport; the same prefix re-binds the
        running animation onto the new merged mesh; a changed prefix
        resets state and re-runs discovery.
        """
        merged = self._last_merged_mesh
        skel = (
            getattr(merged, "skeleton", None) if merged is not None else None
        )
        is_skinned = (
            skel is not None
            and bool(getattr(skel, "bones", None))
            and bool(getattr(merged, "joints", None))
            and bool(getattr(merged, "weights", None))
        )
        new_prefix = self._character_prefix()

        if not self._has_base_body() or new_prefix is None or not is_skinned:
            # No skinned base body to drive (no FACE, or a weapons-only
            # assembly) — hide and clean up.
            self._reset_anim_state(hide_bar=True)
            return

        if new_prefix == self._current_character_prefix:
            # Same character family — only equipment changed. Keep the
            # bar and the animation; re-bind it to the new merged mesh.
            self._reapply_current_animation_to_merged_mesh()
            return

        # Class or gender changed → a different animation set applies.
        self._reset_anim_state(hide_bar=False)
        self._current_character_prefix = new_prefix
        bar = self.viewport_container.animation_bar
        bar.setVisible(True)

        if self._mock_mode or self._d4data_path is None:
            bar.set_inactive("Set a D4Data path to load animations")
            return

        if self._anim_index_ready or self._anim_index_in_cache():
            self._anim_index_ready = True
            self._populate_animations_for_prefix(new_prefix)
        else:
            bar.set_inactive("Loading animations…")
            # set_anim_index_ready populates once the index is built.

    def _reset_anim_state(self, *, hide_bar: bool) -> None:
        """Cancel the in-flight load worker and clear all animation state.

        ``hide_bar`` hides the transport entirely (no base body to
        drive); otherwise the bar stays visible for a fresh discovery.
        """
        self._cancel_anim_load_worker()
        self.viewport_container.viewport.clear_animation()
        self._current_animation = None
        self._current_animation_info = None
        self._current_animation_permutation = 0
        self._current_animation_frame = 0
        self._current_animation_was_playing = False
        self._anim_paused_for_tab = False
        self._current_character_prefix = None
        self._anim_load_prefix = None
        bar = self.viewport_container.animation_bar
        bar.reset()
        if hide_bar:
            bar.setVisible(False)

    def _reapply_current_animation_to_merged_mesh(self) -> None:
        """Re-bind the running animation after an equipment-only rebuild.

        ``load_mesh`` wiped the viewport's skinning cache when the new
        merged mesh was rendered; rebuild it against the new mesh and
        restore the frame (and playback) the user was on.
        """
        if self._current_animation is None or self._last_merged_mesh is None:
            return
        viewport = self.viewport_container.viewport
        bar = self.viewport_container.animation_bar
        prev_frame = self._current_animation_frame
        prev_playing = self._current_animation_was_playing or bar.is_playing()

        ok = viewport.set_animation(
            self._current_animation, self._last_merged_mesh,
        )
        if not ok:
            # The merge produced a non-skinnable result — fall back
            # cleanly rather than leaving a half-bound animation.
            log.warning("Re-binding animation to rebuilt assembly failed")
            self._reset_anim_state(hide_bar=True)
            return
        viewport.show_frame(prev_frame)
        if prev_playing:
            bar.set_playing(True)

    def _populate_animations_for_prefix(self, prefix: str) -> None:
        """Look up animations for ``prefix`` and push them into the bar.

        Discovery unions both gender prefixes for the class — the
        assembled character can play either gender's animations because
        they share the same skeleton template. On name collisions the
        selected gender's version wins, matching D4 runtime behavior
        (a missing ``snoFemaleOverrideAnim`` falls back to the other
        gender's clip).

        The lookup is a fast in-memory scan of the cached animation
        index, so it runs on the GUI thread — no worker round-trip.
        Only the .ani extract+decode (``AnimLoadWorker``) stays
        off-thread. Results are cached per matched-gender prefix.

        IGC and other non-player-playable prefixes are dropped per
        gender via ``anim_lookup.is_player_anim`` before the union, so
        both the dropdown and the export getter see the filtered set.
        """
        bar = self.viewport_container.animation_bar
        if self._d4data_path is None:
            bar.set_inactive("Set a D4Data path to load animations")
            return

        cached = self._anim_discovery_cache.get(prefix)
        if cached is None:
            pair = self._character_prefix_pair()
            if pair is None:
                cached = []
            else:
                matched, other = pair
                # ``matched`` should equal the prefix we were called
                # with; the state machine guarantees it, but guard
                # against drift rather than caching a stale list.
                if matched != prefix:
                    return
                primary_infos = [
                    info
                    for info in anim_lookup.discover_by_prefix(
                        self._d4data_path, matched
                    )
                    if anim_lookup.is_player_anim(info.name)
                ]
                other_infos = [
                    info
                    for info in anim_lookup.discover_by_prefix(
                        self._d4data_path, other
                    )
                    if anim_lookup.is_player_anim(info.name)
                ]
                cached = anim_lookup.union_genders(
                    primary_infos, other_infos,
                    primary_prefix=matched, other_prefix=other,
                )
            self._anim_discovery_cache[prefix] = cached

        if not cached:
            bar.set_inactive("No animations found for this character.")
            return
        bar.set_animations(cached)
        # set_animations populates the dropdown but selects nothing —
        # the bar goes active only when the user picks an animation.

    def _on_animation_selected(self, name: str) -> None:
        """Handle an animation pick from the bar's dropdown.

        The rest-pose entry clears playback; any other entry spawns an
        AnimLoadWorker to extract + decode the .ani off-thread.
        """
        self._cancel_anim_load_worker()
        self.viewport_container.viewport.clear_animation()
        self._current_animation = None
        self._current_animation_info = None
        self._current_animation_frame = 0
        self._current_animation_was_playing = False

        if name == REST_POSE_LABEL:
            return

        bar = self.viewport_container.animation_bar
        info = bar.current_animation()
        if info is None or info.name != name:
            return
        if self._last_merged_mesh is None or self._d4data_path is None:
            return
        if self._game_dir is None:
            return
        self._current_animation_info = info
        self._current_animation_permutation = bar.current_permutation()
        self._start_anim_load(info, self._current_animation_permutation)

    def _start_anim_load(self, info: AnimationInfo, permutation: int) -> None:
        """Spawn an AnimLoadWorker for one animation permutation.

        Cancels any prior load worker first. The decoded animation is
        handed back to :meth:`_on_animation_loaded` on the main thread.
        """
        if self._game_dir is None or self._d4data_path is None:
            return
        self._cancel_anim_load_worker()
        # Stamp the load with the character it was started for, so the
        # callback can drop the decode if the user switched class/gender.
        self._anim_load_prefix = self._current_character_prefix
        rest_pose_map = self._rest_pose_map_for(self._last_merged_mesh)
        worker = AnimLoadWorker(
            self._game_dir, info, permutation,
            self._d4data_path, rest_pose_map, parent=self,
        )
        worker.finished.connect(self._on_animation_loaded)
        worker.error.connect(self._on_animation_load_error)
        worker.finished.connect(self._clear_anim_load_worker_ref)
        worker.error.connect(self._clear_anim_load_worker_ref)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._active_anim_load = worker
        worker.start()

    def _rest_pose_map_for(self, mesh_data: MeshData | None) -> dict:
        """Build a ``bone_hash → (q, wp, scale)`` rest-pose map.

        The decoder folds the rest pose into bones an animation doesn't
        touch; the merged assembly's canonical skeleton supplies it.
        """
        skel = getattr(mesh_data, "skeleton", None)
        if skel is None or not getattr(skel, "bones", None):
            return {}
        return {
            b.name_hash: (b.local_trs.q, b.local_trs.wp, b.local_trs.scale)
            for b in skel.bones
        }

    def _on_animation_loaded(self, decoded, perm_index: int) -> None:
        """Bind a freshly-decoded animation onto the merged assembly."""
        info = self._current_animation_info
        merged = self._last_merged_mesh
        if info is None or merged is None:
            return
        # Drop a stale decode whose character prefix changed under us
        # (class/gender switched between selection and decode).
        if self._anim_load_prefix != self._current_character_prefix:
            return
        # Drop a stale decode whose permutation no longer matches the
        # selector (the user may have spun it again mid-load).
        if perm_index != self._current_animation_permutation:
            return
        self._current_animation = decoded
        viewport = self.viewport_container.viewport
        bar = self.viewport_container.animation_bar
        ok = viewport.set_animation(decoded, merged)
        if not ok:
            bar.set_inactive("Could not bind animation to assembly")
            self._current_animation = None
            return
        bar.set_active(
            frame_count=decoded.frame_count,
            frame_rate=decoded.frame_rate,
            permutation_count=info.permutation_count,
        )
        # Restore the prior scrub position — matters on a permutation
        # change, where set_active reset the slider to frame 0.
        target = self._current_animation_frame
        if 0 < target < decoded.frame_count:
            bar.set_frame(target)
        else:
            self._current_animation_frame = 0

    def _on_animation_load_error(self, message: str) -> None:
        log.warning("Builder animation load failed: %s", message)
        self.viewport_container.animation_bar.set_inactive(
            f"Load failed: {message[:80]}"
        )

    def _clear_anim_load_worker_ref(self, *_args) -> None:
        self._active_anim_load = None

    def _on_animation_frame_changed(self, frame: int) -> None:
        self._current_animation_frame = int(frame)
        if self._current_animation is not None:
            self.viewport_container.viewport.show_frame(int(frame))

    def _on_animation_permutation_changed(self, idx: int) -> None:
        """Reload the current animation at a new permutation index."""
        info = self._current_animation_info
        if info is None:
            return
        self._current_animation_permutation = int(idx)
        # A permutation change pauses playback (the bar stops its timer
        # with signals blocked, so ``play_toggled`` never fires). Clear
        # the latch by hand so a later equipment-swap re-bind doesn't
        # mistake the paused animation for a playing one.
        self._current_animation_was_playing = False
        # _current_animation_frame is intentionally preserved — the load
        # handler restores it once the new permutation finishes decoding.
        self.viewport_container.viewport.clear_animation()
        self._current_animation = None
        self._start_anim_load(info, self._current_animation_permutation)

    def _on_animation_play_toggled(self, playing: bool) -> None:
        # Latch the latest transport state so a rebuild firing mid-toggle
        # still has a reliable read when it re-binds the animation.
        self._current_animation_was_playing = bool(playing)

    def _cancel_anim_load_worker(self) -> None:
        """Stop the in-flight AnimLoadWorker, if any."""
        prior = self._active_anim_load
        if prior is None:
            return
        try:
            if prior.isRunning():
                prior.requestInterruption()
                try:
                    prior.finished.disconnect()
                    prior.error.disconnect()
                except (RuntimeError, TypeError):
                    pass
                prior.quit()
                prior.wait(2000)
        except RuntimeError:
            pass
        self._active_anim_load = None

    def _anim_index_in_cache(self) -> bool:
        """Whether the d4data animation index is already built in memory.

        Covers the warm-start case where ``AnimIndexBuildWorker`` finds
        the index already cached and never fires ``index_ready`` — the
        builder would otherwise wait forever for a signal never sent.
        """
        if self._d4data_path is None or self._mock_mode:
            return False
        try:
            return anim_lookup.get_cached_index(self._d4data_path) is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Public API — animation
    # ------------------------------------------------------------------

    def set_anim_index_ready(self, ready: bool = True) -> None:
        """Mark the d4data animation index as built and flush any wait.

        Wired to ``AnimIndexBuildWorker.index_ready``. If a character
        was selected before the index finished building, discovery was
        deferred — populate the bar now.
        """
        self._anim_index_ready = bool(ready)
        if not self._anim_index_ready:
            return
        prefix = self._current_character_prefix
        if (
            prefix is not None
            and prefix not in self._anim_discovery_cache
            and self._d4data_path is not None
            and not self._mock_mode
        ):
            self._populate_animations_for_prefix(prefix)

    def set_item_index_ready(self, ready: bool = True) -> None:
        """Mark the equipment name index built and refresh visible labels.

        Wired to ``ItemIndexBuildWorker.index_ready``. The PieceBrowser
        rows and any already-equipped slot cards were labelled with SNO
        stems while the index was building — re-resolve them now that
        in-game names are available.
        """
        if not ready:
            return
        self.piece_browser.refresh_display_names()
        for slot_key, sno_path in self.slot_panel.get_build().items():
            if sno_path:
                self.slot_panel.set_equipped(
                    slot_key, sno_path, self._display_name_for(sno_path),
                )

    def _display_name_for(self, sno_path: str) -> str:
        """Human-readable label for an equipment piece.

        Returns the in-game item name when the equipment name index has
        resolved one, else the SNO filename stem (the pre-index
        behaviour). Also installed as the PieceBrowser's row-label
        resolver, so the same fallback rule governs both surfaces.
        """
        if self._d4data_path is not None and not self._mock_mode:
            try:
                name = item_lookup.get_display_name(
                    self._d4data_path, sno_path,
                )
            except Exception:  # noqa: BLE001 — labelling must never crash
                name = None
            if name:
                return name
        return _stem_for(sno_path)

    def set_tab_active(self, active: bool) -> None:
        """Pause/resume builder animation playback on a tab switch.

        The AnimationBar's QTimer keeps firing even when the builder
        tab is hidden, uselessly re-skinning an off-screen viewport.
        Pause it on the way out and resume on return if it was
        mid-playback.
        """
        bar = self.viewport_container.animation_bar
        if active:
            if (
                self._anim_paused_for_tab
                and self._current_animation is not None
            ):
                bar.set_playing(True)
            self._anim_paused_for_tab = False
        else:
            if bar.is_playing():
                self._anim_paused_for_tab = True
                bar.set_playing(False)
            else:
                self._anim_paused_for_tab = False

    # ------------------------------------------------------------------
    # Public API — export
    # ------------------------------------------------------------------

    def has_export_data(self) -> bool:
        """Whether anything is currently equipped that the export can use.

        Static-only (e.g. weapons-only) configurations still count: the
        single static piece is exported as-is. Used by the main window
        to gate the status-bar export button without recomputing the
        full merge.
        """
        if self._last_merged_pieces:
            return True
        if self._overlay_tag_to_sno:
            return True
        # Last-ditch: a piece that loaded but for some reason isn't in
        # ``_last_merged_pieces`` (e.g. a single static loaded as the
        # primary mesh) is still exportable from the cache.
        return any(
            sno in self._mesh_cache for sno in self._collect_equipped_paths()
        )

    def get_export_anim_infos(self) -> list[AnimationInfo]:
        """All discovered animations for the current character family.

        Returns the cached ``AnimationInfo`` list for the active
        class+gender prefix — the same list shown in the animation
        bar. Empty list if discovery hasn't run, the index isn't
        ready, or no class/gender is selected. The ``ExportWorker``
        treats an empty list the same as ``None`` and emits an
        animation-free glTF.
        """
        if not self._current_character_prefix:
            return []
        return list(
            self._anim_discovery_cache.get(self._current_character_prefix, ()),
        )

    def get_export_data(
        self,
    ) -> tuple[
        MeshData,
        set[int] | None,
        list[tuple[MeshData, set[int] | None]],
    ] | None:
        """Build an export-ready primary mesh + filter + extras list.

        Returns ``(primary_mesh, primary_filter, extras)`` or ``None``
        when nothing is equipped. The primary mesh is the skinned
        assembly merged under the shared canonical skeleton. ``extras``
        is a list of ``(static_mesh, per_mesh_submesh_filter)`` —
        weapons and other unskinned pieces are kept separate so they
        export as standalone mesh nodes (no skin attachment), letting
        Blender users re-parent them to the appropriate hand bone
        manually.

        Edge case: if no skinned pieces are equipped, the first static
        piece becomes the primary (still unskinned, no extras) and
        subsequent statics become extras. A purely-empty assembly
        returns ``None``.

        ``prune_bones`` MUST be ``False`` when exporting the returned
        primary mesh — joint indices from each piece are valid against
        the shared skeleton because they all share the same template
        id, but pruning would re-index bones without rewriting
        JOINTS_0 from the other pieces. The caller (main_window) is
        responsible for forcing this option off.
        """
        # Reuse the same equipped-paths collection that drives the
        # rebuild — keeps export and viewport in lockstep.
        equipped = self._collect_equipped_paths()
        ready = [
            self._mesh_cache[p] for p in equipped if p in self._mesh_cache
        ]
        if not ready:
            return None

        skinned = [p for p in ready if p.skeleton is not None]
        static = [p for p in ready if p.skeleton is None]

        visibility_map = self.assembly_parts_panel.get_visibility_map()

        # Build per-static extras: each static piece carries its own
        # submesh filter against its own local indices. The parts
        # panel already keys visibility by SNO path, so we just look
        # them up here.
        def _filter_for(piece: MeshData) -> set[int] | None:
            sno = self._sno_for_piece(piece)
            if sno is None or sno not in visibility_map:
                return None
            visible = visibility_map[sno]
            if visible is None:
                return None
            # Collapse "every submesh visible" to ``None`` so the
            # exporter's hot path skips the filter loop entirely.
            if len(visible) == len(piece.submeshes):
                return None
            return set(visible)

        # No skinned pieces at all → fall back to single-mesh export
        # (first static piece as primary, rest as extras). The merged
        # body is what gives us a skeleton; without it we can't skin.
        if not skinned:
            if not static:
                return None
            primary = static[0]
            extras: list[tuple[MeshData, set[int] | None]] = [
                (p, _filter_for(p)) for p in static[1:]
            ]
            return primary, _filter_for(primary), extras

        merged, included = merge_pieces(skinned, name="character")
        if merged is None:
            return None

        # Translate the parts panel's per-SNO local-index visibility
        # into a flat set of merged-stream global indices. Walk the
        # ``included`` order returned by ``merge_pieces`` — pieces
        # filtered out by template-id mismatch won't appear there and
        # therefore don't contribute to the submesh stream either.
        filter_set: set[int] = set()
        global_offset = 0
        total_submeshes = 0
        for piece in included:
            sno = self._sno_for_piece(piece)
            n_subs = len(piece.submeshes)
            piece_visible: set[int] | None
            # ``get`` returns None for two distinct reasons: the SNO
            # isn't in the map at all (treat as "all visible" — the
            # parts panel might not have a row for this piece), or the
            # row's value is explicitly None ("every checkbox checked",
            # also "all visible"). Both should map to the all-visible
            # branch.
            if sno is None or sno not in visibility_map:
                piece_visible = None
            else:
                piece_visible = visibility_map[sno]
            for local_idx in range(n_subs):
                if piece_visible is None or local_idx in piece_visible:
                    filter_set.add(global_offset + local_idx)
            global_offset += n_subs
            total_submeshes += n_subs

        if len(filter_set) == total_submeshes:
            submesh_filter: set[int] | None = None
        else:
            submesh_filter = filter_set

        extras = [(p, _filter_for(p)) for p in static]
        return merged, submesh_filter, extras

    def get_export_pieces(
        self,
    ) -> tuple[
        list[tuple[MeshData, set[int] | None]],
        Skeleton | None,
        list[tuple[MeshData, set[int] | None]],
    ] | None:
        """Per-piece export data: each skinned piece kept as its own MeshData.

        Returns ``(skinned_pieces, canonical_skeleton, static_extras)``,
        or ``None`` if no exportable assembly is present.

        - ``skinned_pieces``: list of ``(MeshData, submesh_filter)`` for
          each piece that participates in the assembly's skinned
          skeleton. Each piece keeps its own positions / normals / UVs /
          joints / weights / indices / submeshes / materials. JOINTS_0
          values are valid against the canonical skeleton without
          remapping (shared template_id invariant — see
          ``assembly.py:1-27``).
        - ``canonical_skeleton``: the skeleton chosen by the same logic
          ``merge_pieces`` uses (via :func:`select_assembly_group`).
          Used as the single source-of-truth Skin for all skinned
          pieces in glTF. ``None`` only when ``skinned_pieces`` is empty.
        - ``static_extras``: same shape and semantics as
          ``get_export_data()``'s ``extras`` — unskinned overlay meshes
          (weapons) parented at scene root, not bound to the skeleton.

        When nothing skinned is equipped (a weapons-only scene) the
        skinned list is empty and ``canonical_skeleton`` is ``None``;
        the caller falls back to the single-mesh export path. A
        purely-empty assembly returns ``None``.

        Unlike :meth:`get_export_data` this does NOT call
        ``merge_pieces`` — the pieces stay separate so the exporter can
        emit one glTF mesh node per piece under a shared skin. The
        skeleton is returned separately rather than read from
        ``skinned_pieces[0].skeleton`` because each piece carries its
        own skeleton object; the canonical pick is the authoritative
        one. ``prune_bones`` MUST stay ``False`` for the same reason
        documented on :meth:`get_export_data`.
        """
        # Same equipped-paths collection that drives the rebuild — keeps
        # export and viewport in lockstep.
        equipped = self._collect_equipped_paths()
        ready = [
            self._mesh_cache[p] for p in equipped if p in self._mesh_cache
        ]
        if not ready:
            return None

        skinned = [p for p in ready if p.skeleton is not None]
        static = [p for p in ready if p.skeleton is None]

        visibility_map = self.assembly_parts_panel.get_visibility_map()

        # Per-piece submesh filter against the piece's own local indices.
        # The parts panel keys visibility by SNO path; mirror the
        # ``_filter_for`` closure in ``get_export_data``.
        def _filter_for(piece: MeshData) -> set[int] | None:
            sno = self._sno_for_piece(piece)
            if sno is None or sno not in visibility_map:
                return None
            visible = visibility_map[sno]
            if visible is None:
                return None
            # Collapse "every submesh visible" to ``None`` so the
            # exporter's hot path skips the filter loop entirely.
            if len(visible) == len(piece.submeshes):
                return None
            return set(visible)

        static_extras: list[tuple[MeshData, set[int] | None]] = [
            (p, _filter_for(p)) for p in static
        ]

        # Weapons-only assembly — no skeleton, nothing to skin. Hand the
        # statics back with an empty skinned list; the caller routes
        # this to the single-mesh export path.
        if not skinned:
            return [], None, static_extras

        # Reuse the exact grouping / outlier-exclusion logic
        # ``merge_pieces`` uses, but keep the pieces unmerged.
        group, canonical_skel = select_assembly_group(skinned)
        if not group:
            return None

        skinned_pieces: list[tuple[MeshData, set[int] | None]] = [
            (piece, _filter_for(piece)) for piece in group
        ]
        return skinned_pieces, canonical_skel, static_extras

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_catalog(self, entries: list[str]) -> None:
        """Forward the CASC catalog to the piece browser."""
        self.piece_browser.load_entries(entries)

    def set_game_context(
        self,
        game_dir: Path | None,
        d4data_path: Path | None,
        mock: bool = False,
    ) -> None:
        """Wire the CASC game directory the builder's load workers use.

        Called by the main window after the user picks (or restores) a
        game directory. Cached meshes from a different install would
        still be valid (same SNO → same parsed bytes), but mock-mode
        flips invalidate the cache because the worker output is
        synthetic in mock mode and real otherwise.
        """
        self._game_dir = game_dir
        self._d4data_path = d4data_path
        if mock != self._mock_mode:
            self._cancel_all_workers()
            self._cancel_all_texture_workers()
            self._cancel_anim_load_worker()
            self._mesh_cache.clear()
            self._texture_cache.clear()
            self._anim_discovery_cache.clear()
        self._mock_mode = mock

    @property
    def viewport(self) -> ViewportWidget:
        return self.viewport_container.viewport

    def shutdown(self) -> None:
        """Tear down the viewport and stop the piece browser worker."""
        self._cancel_all_workers()
        self._cancel_all_texture_workers()
        self._cancel_anim_load_worker()
        try:
            self.viewport_container.viewport.shutdown()
        except Exception:
            pass
        try:
            self.piece_browser.shutdown()
        except Exception:
            pass


def _basename(sno_path: str) -> str:
    """Return the trailing filename component of a CASC SNO path."""
    return sno_path.rsplit("/", 1)[-1]


def _stem_for(sno_path: str) -> str:
    """Filename stem (no directory, no ``.app``).

    The fallback label for an equipment piece when the equipment name
    index has no in-game name for it — see
    ``CharacterBuilderPage._display_name_for``.
    """
    name = _basename(sno_path)
    if name.lower().endswith(".app"):
        name = name[:-4]
    return name

