"""Top-level QMainWindow for D4.Export."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from d4extract.gui.settings import AppSettings
from d4extract.gui.widgets.animation_bar import AnimationBar, REST_POSE_LABEL
from d4extract.gui.widgets.character_builder import CharacterBuilderPage
from d4extract.gui.widgets.d4data_card import D4DataCard
from d4extract.gui.widgets.export_button import ExportButton, FMT_GLB, FMT_GLTF
from d4extract.gui.widgets.model_list import ModelListWidget
from d4extract.gui.widgets.properties_bar import PropertiesBar
from d4extract.gui.widgets.setup_card import SetupCard
from d4extract.gui.widgets.submesh_list import SubmeshListWidget
from d4extract.gui.widgets.viewport import ViewportWidget
from d4extract.gui.workers.actor_worker import ActorIndexBuildWorker
from d4extract.gui.workers.anim_worker import (
    AnimDiscoveryWorker, AnimIndexBuildWorker, AnimLoadWorker,
)
from d4extract.gui.workers.catalog_worker import CatalogWorker
from d4extract.gui.workers.export_worker import ExportWorker
from d4extract.gui.workers.item_worker import ItemIndexBuildWorker
from d4extract.gui.workers.load_worker import LoadModelWorker
from d4extract.gui.workers.texture_worker import TextureWorker

log = logging.getLogger(__name__)

# QStackedWidget page indices.
PAGE_SETUP = 0
PAGE_D4DATA = 1
PAGE_MAIN = 2

# Tab indices on the main page.
TAB_BROWSER = 0
TAB_CHARACTER_BUILDER = 1


def _model_stem(sno_path: str) -> str:
    """Filename of a SNO path with any ``.app`` extension stripped.

    Last-resort fallback for the Model Browser's row labels when
    neither the equipment-name index nor the actor-name index has a
    hit (or when both are still building).
    """
    name = sno_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if name.lower().endswith(".app"):
        name = name[:-4]
    return name


class _ViewportContainer(QWidget):
    """Viewport + animation bar + properties bar stacked vertically.

    The animation bar lives between the viewport and the properties
    strip; it stays hidden until a skinned model has loaded *and*
    animation discovery has returned at least one match.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.viewport = ViewportWidget(self)
        self.animation_bar = AnimationBar(self)
        self.animation_bar.setVisible(False)
        self.properties = PropertiesBar(self)

        layout.addWidget(self.viewport, 1)
        layout.addWidget(self.animation_bar, 0)
        layout.addWidget(self.properties, 0)


class D4ExportWindow(QMainWindow):
    """Main application window. Two-page stack: setup → main browser."""

    def __init__(self, mock_entries: list[str] | None = None) -> None:
        super().__init__()
        self.setWindowTitle("D4.Export")
        self.setMinimumSize(900, 600)

        self._settings = AppSettings()
        # One-shot migration for a stale ``d4data_path`` saved by an
        # earlier downloader build that persisted the top-level repo
        # instead of the ``json/`` subpath the rest of the code
        # consumes. If the saved path has ``json/base/`` inside it
        # (the top-level shape), rewrite QSettings to the ``json/``
        # subdir; if it's already correct (``base/`` directly inside),
        # this is a no-op. A genuinely broken path falls through to
        # the existing d4data card flow.
        self._maybe_migrate_d4data_path()
        self.game_dir: Path | None = None
        self.current_sno_path: str | None = None
        self.current_mesh_data: object | None = None
        self._mock_entries = mock_entries
        self._mock_mode = mock_entries is not None
        self._catalog_worker: CatalogWorker | None = None
        self._load_worker: LoadModelWorker | None = None
        self._export_worker: ExportWorker | None = None
        self._texture_worker: TextureWorker | None = None
        self._anim_index_build_worker: AnimIndexBuildWorker | None = None
        self._item_index_build_worker: ItemIndexBuildWorker | None = None
        self._actor_index_build_worker: ActorIndexBuildWorker | None = None
        self._anim_discovery_worker: AnimDiscoveryWorker | None = None
        self._anim_load_worker: AnimLoadWorker | None = None
        # Cache animation discovery results per appearance so re-loading
        # the same skeleton skips the d4data scan.
        self._anim_discovery_cache: dict[str, list] = {}
        # Cache the appearance name discovery is currently running for —
        # late results land asynchronously and we drop them when the
        # user has moved on to a different model.
        self._pending_discovery_appearance: str | None = None
        # True once the current anim bar is populated from the full index;
        # False means it holds prefix-glob (fast, possibly incomplete) results.
        self._current_discovery_is_complete: bool = True
        # Hold the currently-loaded ``DecodedAnimation`` so the frame
        # handler can re-skin without re-extracting from CASC.
        self._current_animation = None
        self._current_animation_info = None

        # Session-scoped in-memory caches: re-clicking a model the user
        # already loaded skips both the .app re-parse and the texture
        # re-decode. Keyed on the SNO path string. Cleared on window
        # close — no on-disk persistence beyond the existing CASC
        # extraction / texture payload disk caches.
        self._mesh_cache: dict = {}
        self._texture_cache: dict = {}

        self._stack = QStackedWidget(self)
        self.setCentralWidget(self._stack)

        self._setup_card = SetupCard(self._settings, self._stack)
        self._setup_card.directory_selected.connect(self.set_game_dir)
        self._stack.addWidget(self._setup_card)

        # d4data card: shown when ``settings.d4data_path()`` is unset.
        # Order matches PAGE_* constants — index in the stack is what
        # gets compared against the PAGE_D4DATA marker.
        self._d4data_card = D4DataCard(self._settings, self._stack)
        self._d4data_card.d4data_ready.connect(self._on_d4data_ready)
        self._stack.addWidget(self._d4data_card)

        self._main_page = self._build_main_page()
        self._stack.addWidget(self._main_page)

        # Status bar — message on the left, export button permanently on
        # the right. Permanent widgets survive transient showMessage calls.
        self.statusBar().setSizeGripEnabled(False)
        # TACT key status indicator: tells the user at a glance whether
        # encrypted content will be reachable on the next extract.
        # Added before the export button so it sits to its left.
        self._tact_status = QLabel("", self.statusBar())
        self._tact_status.setStyleSheet("QLabel { padding: 0 8px; color: #666; }")
        self.statusBar().addPermanentWidget(self._tact_status)
        self._export_button = ExportButton(self._settings, self.statusBar())
        self._export_button.export_requested.connect(self._on_export_requested)
        self.statusBar().addPermanentWidget(self._export_button)
        self.statusBar().showMessage("Ready")
        self._refresh_tact_status()

        self._build_menu_bar()
        self._build_shortcuts()

        # Mock mode never has a real game directory; surface that to the
        # builder page so its load workers run the synthetic path.
        self._builder_page.set_game_context(
            None, self._settings.d4data_path(), mock=self._mock_mode,
        )

        # Decide initial page from saved settings.
        # Order: SETUP (missing game dir) → D4DATA (missing d4data) → MAIN.
        # In mock mode the catalog comes from synthetic entries, so we
        # skip both setup cards.
        saved = self._settings.game_dir()
        if self._mock_mode:
            self._stack.setCurrentIndex(PAGE_MAIN)
            self._begin_catalog_load(saved or Path("."))
        elif saved is None:
            self._stack.setCurrentIndex(PAGE_SETUP)
        elif self._settings.d4data_path() is None:
            self.game_dir = saved
            self._stack.setCurrentIndex(PAGE_D4DATA)
        else:
            self.game_dir = saved
            self._builder_page.set_game_context(
                saved, self._settings.d4data_path(), mock=self._mock_mode,
            )
            self._stack.setCurrentIndex(PAGE_MAIN)
            self._begin_catalog_load(saved)

        # Kick off the animation + equipment + actor name index builds
        # immediately so they're ready (or loading from disk) before the
        # user picks their first model / browses an equipment slot. Each
        # touches a different StringList prefix so they don't contend.
        d4data = self._settings.d4data_path()
        if d4data is not None and not self._mock_mode:
            self._begin_anim_index_build(d4data)
            self._begin_item_index_build(d4data)
            self._begin_actor_index_build(d4data)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_main_page(self) -> QWidget:
        tabs = QTabWidget(self)
        tabs.setDocumentMode(True)
        tabs.addTab(self._build_browser_tab(), "Model Browser")
        tabs.addTab(self._build_character_builder_tab(), "Character Builder")
        tabs.setCurrentIndex(TAB_BROWSER)
        self._tabs = tabs
        # Switching tabs flips which side the export button is gated
        # against — Model Browser uses ``current_mesh_data``, builder
        # uses ``has_export_data``. The signal also fires when the
        # builder rebuilds, so the button stays in sync without polling.
        tabs.currentChanged.connect(self._on_tab_changed)
        self._builder_page.assembly_changed.connect(
            self._on_builder_assembly_changed,
        )
        return tabs

    def _build_browser_tab(self) -> QWidget:
        # The browser tab is the splitter plus optional first-run
        # banners stacked above it. Each banner is created hidden and
        # only revealed when its respective resource is missing AND
        # the user hasn't dismissed it. d4data banner sits above the
        # TACT banner because d4data is the more disruptive missing
        # dependency (without it many UI features silently degrade).
        container = QWidget(self)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._d4data_banner = self._build_d4data_banner(container)
        self._d4data_banner.setVisible(False)
        outer.addWidget(self._d4data_banner, 0)

        self._tact_banner = self._build_tact_banner(container)
        self._tact_banner.setVisible(False)
        outer.addWidget(self._tact_banner, 0)

        # Horizontal splitter: 30% list, 60% viewport, 10% submesh list.
        splitter = QSplitter(Qt.Horizontal, container)
        splitter.setHandleWidth(8)
        splitter.setChildrenCollapsible(False)

        self._model_list = ModelListWidget(self._settings, splitter)
        self._model_list.model_selected.connect(self._on_model_selected)
        # Label every row with its in-game / actor display name; falls
        # back to the SNO stem when neither index has a hit (or when
        # both are still building). The resolver tolerates an empty
        # game context, so installing it before the indices land is
        # safe — rows just stay on stems until refresh_display_names
        # fires.
        self._model_list.set_name_resolver(self._model_display_name_for)
        self._model_list.setMinimumWidth(200)
        splitter.addWidget(self._model_list)

        self._viewport_container = _ViewportContainer(splitter)
        self._viewport_container.setMinimumWidth(400)
        splitter.addWidget(self._viewport_container)

        anim_bar = self._viewport_container.animation_bar
        anim_bar.animation_selected.connect(self._on_animation_selected)
        anim_bar.permutation_changed.connect(self._on_animation_permutation_changed)
        anim_bar.frame_changed.connect(self._on_animation_frame_changed)

        self._submesh_list = SubmeshListWidget(splitter)
        self._submesh_list.visibility_changed.connect(
            self._on_submesh_visibility_changed,
        )
        self._submesh_list.bulk_visibility_changed.connect(
            self._on_submesh_bulk_visibility_changed,
        )
        self._submesh_list.setMinimumWidth(180)
        splitter.addWidget(self._submesh_list)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 6)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([300, 600, 200])

        self._splitter = splitter
        outer.addWidget(splitter, 1)
        return container

    def _build_tact_banner(self, parent: QWidget) -> QFrame:
        """First-run banner that nags about missing TACT keys.

        Dismissal is persisted in QSettings so the banner doesn't
        return after the user has explicitly closed it; loading a key
        file via the menu also flips the dismissed flag (no point
        reminding them about something they just did).
        """
        frame = QFrame(parent)
        frame.setObjectName("tactBanner")
        frame.setFrameShape(QFrame.StyledPanel)
        # Styling lives in gui/theme.py under the QFrame#tactBanner
        # selector so the banner picks up the same accent blue as the
        # rest of the chrome (selected tabs, primary buttons, focused
        # inputs) instead of a one-off inline palette.
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        label = QLabel(
            "TACT keys not loaded — some encrypted content (unreleased "
            "cosmetics/seasonal items) will be skipped. Use "
            "<b>File → Load TACT Keys…</b> to add them.",
            frame,
        )
        label.setWordWrap(True)
        label.setTextFormat(Qt.RichText)
        layout.addWidget(label, 1)

        load_btn = QPushButton("Load now", frame)
        load_btn.setObjectName("tactBannerPrimary")
        load_btn.clicked.connect(self._on_menu_load_tact_keys)
        layout.addWidget(load_btn, 0)

        dismiss_btn = QPushButton("Dismiss", frame)
        dismiss_btn.setObjectName("tactBannerGhost")
        dismiss_btn.clicked.connect(self._on_dismiss_tact_banner)
        layout.addWidget(dismiss_btn, 0)

        return frame

    def _refresh_tact_banner(self) -> None:
        """Show or hide the banner based on current keys + dismiss state."""
        if not hasattr(self, "_tact_banner"):
            return
        loaded = self._settings.tact_keys_path() is not None
        dismissed = self._settings.tact_banner_dismissed()
        self._tact_banner.setVisible(not loaded and not dismissed)

    def _build_d4data_banner(self, parent: QWidget) -> QFrame:
        """First-run banner that nags about missing d4data.

        Same pattern as :meth:`_build_tact_banner` — a thin coloured
        strip with a primary "set up" action and a dismiss button.
        Dismissal is persistent; opening the d4data card via the
        button auto-clears it.
        """
        frame = QFrame(parent)
        frame.setObjectName("d4dataBanner")
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        label = QLabel(
            "d4data not configured — model and material references "
            "will not resolve. Use <b>File → Set d4data Folder…</b> "
            "or <b>Re-download d4data</b>.",
            frame,
        )
        label.setWordWrap(True)
        label.setTextFormat(Qt.RichText)
        layout.addWidget(label, 1)

        setup_btn = QPushButton("Set up d4data", frame)
        setup_btn.setObjectName("d4dataBannerPrimary")
        setup_btn.clicked.connect(self._on_open_d4data_card)
        layout.addWidget(setup_btn, 0)

        dismiss_btn = QPushButton("Dismiss", frame)
        dismiss_btn.setObjectName("d4dataBannerGhost")
        dismiss_btn.clicked.connect(self._on_dismiss_d4data_banner)
        layout.addWidget(dismiss_btn, 0)

        return frame

    def _refresh_d4data_banner(self) -> None:
        """Show or hide the d4data banner based on current state."""
        if not hasattr(self, "_d4data_banner"):
            return
        configured = self._settings.d4data_path() is not None
        dismissed = self._settings.d4data_banner_dismissed()
        self._d4data_banner.setVisible(not configured and not dismissed)

    def _on_dismiss_d4data_banner(self) -> None:
        self._settings.set_d4data_banner_dismissed(True)
        self._refresh_d4data_banner()

    def _on_open_d4data_card(self) -> None:
        """Switch to the d4data card from the banner's primary button."""
        self._stack.setCurrentIndex(PAGE_D4DATA)

    def _maybe_migrate_d4data_path(self) -> None:
        """Rewrite a stale top-level d4data path to its ``json/`` subdir.

        Background: an earlier downloader build saved the top-level
        repo path (``…/d4data/``) rather than its ``json/`` subdir.
        Every downstream consumer (payload resolver, material parser,
        anim index builder) expects ``base/`` as a direct child, so
        the stale value reproduces the "missing one of: ('base',)"
        symptom on every launch until the user reconfigures.

        We migrate in-place: if the saved path looks like the
        top-level repo, swap in the ``json/`` subpath. If the saved
        path is already correct, this is a no-op. If the path is
        genuinely broken (neither shape matches), leave it alone so
        the existing D4DataCard flow can re-prompt the user.
        """
        saved = self._settings.d4data_path()
        if saved is None:
            return
        # Already in the consumed form (``base/`` directly inside).
        if (saved / "base").is_dir():
            return
        # Top-level form: ``json/base/`` exists one level down.
        from d4extract.setup import is_d4data_dir, resolve_d4data_json_path

        if is_d4data_dir(saved):
            migrated = resolve_d4data_json_path(saved)
            self._settings.set_d4data_path(migrated)
            log.info(
                "migrated stale d4data_path %s -> %s", saved, migrated,
            )

    def _refresh_tact_status(self) -> None:
        """Update the status-bar TACT key indicator.

        Distinguishes three states so users can diagnose "encrypted
        content skipped" failures without guessing: no path set, path
        set but file gone (most often a deleted user data dir), and the
        normal loaded-N-keys case.
        """
        if not hasattr(self, "_tact_status"):
            return
        cached = self._settings.tact_keys_path()
        if cached is None:
            # _read_path returns None both when QSettings has no entry
            # and when the entry's file no longer exists; disambiguate
            # by checking QSettings directly.
            raw = self._settings._qs.value(self._settings.KEY_TACT_KEYS)
            if raw:
                self._tact_status.setText("TACT keys: file missing")
            else:
                self._tact_status.setText("TACT keys: not loaded")
            return
        from d4extract.config import count_valid_keys

        n = count_valid_keys(cached)
        self._tact_status.setText(f"TACT keys: {n} loaded")

    def _on_dismiss_tact_banner(self) -> None:
        self._settings.set_tact_banner_dismissed(True)
        self._refresh_tact_banner()

    def _build_character_builder_tab(self) -> QWidget:
        self._builder_page = CharacterBuilderPage(self)
        return self._builder_page

    def _build_menu_bar(self) -> None:
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")

        change_action = QAction("Change Game Directory", self)
        change_action.triggered.connect(self._on_change_game_dir)
        file_menu.addAction(change_action)

        d4data_action = QAction("Set D4Data Path…", self)
        d4data_action.triggered.connect(self._on_menu_set_d4data)
        file_menu.addAction(d4data_action)

        d4data_folder_action = QAction("Set d4data Folder…", self)
        d4data_folder_action.triggered.connect(
            self._on_menu_set_d4data_folder,
        )
        file_menu.addAction(d4data_folder_action)

        redownload_action = QAction("Re-download d4data", self)
        redownload_action.triggered.connect(
            self._on_menu_redownload_d4data,
        )
        file_menu.addAction(redownload_action)

        tact_load_action = QAction("Load TACT Keys…", self)
        tact_load_action.triggered.connect(self._on_menu_load_tact_keys)
        file_menu.addAction(tact_load_action)

        tact_clear_action = QAction("Clear TACT Keys", self)
        tact_clear_action.triggered.connect(self._on_menu_clear_tact_keys)
        file_menu.addAction(tact_clear_action)
        self._tact_clear_action = tact_clear_action

        file_menu.addSeparator()

        export_action = QAction("Export Selected…", self)
        export_action.setShortcut(QKeySequence("Ctrl+E"))
        export_action.triggered.connect(self._on_export_shortcut)
        file_menu.addAction(export_action)
        self._export_action = export_action

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.setShortcut(QKeySequence.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        view_menu = menu_bar.addMenu("&View")

        self._wireframe_action = QAction("Toggle Wireframe", self)
        self._wireframe_action.setShortcut(QKeySequence("W"))
        self._wireframe_action.triggered.connect(self._on_toggle_wireframe)
        view_menu.addAction(self._wireframe_action)

        self._reset_view_action = QAction("Reset View", self)
        self._reset_view_action.setShortcut(QKeySequence("F"))
        self._reset_view_action.triggered.connect(self._on_reset_view)
        view_menu.addAction(self._reset_view_action)

    def _build_shortcuts(self) -> None:
        # Ctrl+F focuses the model list filter input from anywhere.
        find_shortcut = QShortcut(QKeySequence.Find, self)
        find_shortcut.setContext(Qt.ApplicationShortcut)
        find_shortcut.activated.connect(self._on_focus_filter)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_game_dir(self, path: Path) -> None:
        """Persist the chosen game directory and advance the setup flow.

        Routing depends on whether d4data is already configured: if
        not, the user gets the d4data setup card before the main
        browser. The catalog scan and the per-resource index builds
        are gated on d4data being present, so deferring them past the
        d4data card is the cleaner UX.
        """
        path = Path(path)
        self._settings.set_game_dir(path)
        self.game_dir = path
        self._builder_page.set_game_context(
            path, self._settings.d4data_path(), mock=self._mock_mode,
        )
        if (
            not self._mock_mode
            and self._settings.d4data_path() is None
        ):
            self._stack.setCurrentIndex(PAGE_D4DATA)
            return
        self._stack.setCurrentIndex(PAGE_MAIN)
        self._begin_catalog_load(path)

    def _on_d4data_ready(self, d4data_path: Path) -> None:
        """Settle the post-d4data state and proceed to the main page.

        Called from :class:`D4DataCard.d4data_ready`. The card has
        already persisted the path via ``settings.set_d4data_path``;
        our job is to wire the path into the builder, kick the index
        builds, and switch to the main page. If we reached this card
        without ever choosing a game dir (e.g. mock mode being toggled
        off mid-flight), fall back to the setup card instead of
        loading nothing.
        """
        if self.game_dir is None and not self._mock_mode:
            self._stack.setCurrentIndex(PAGE_SETUP)
            return
        self._builder_page.set_game_context(
            self.game_dir, d4data_path, mock=self._mock_mode,
        )
        # Index builds were skipped at startup because d4data was
        # missing — start them now.
        if not self._mock_mode:
            self._begin_anim_index_build(d4data_path)
            self._begin_item_index_build(d4data_path)
            self._begin_actor_index_build(d4data_path)
        self._stack.setCurrentIndex(PAGE_MAIN)
        # Configuring d4data implicitly answers the first-run banner.
        self._settings.set_d4data_banner_dismissed(True)
        if hasattr(self, "_refresh_d4data_banner"):
            self._refresh_d4data_banner()
        # ``set_game_dir`` is the normal entry to this branch and
        # already kicks the catalog load, but when we land here from
        # the d4data card the catalog scan hasn't been triggered yet.
        if self.game_dir is not None:
            self._begin_catalog_load(self.game_dir)

    # ------------------------------------------------------------------
    # Catalog loading
    # ------------------------------------------------------------------

    def _begin_catalog_load(self, game_dir: Path) -> None:
        if self._mock_entries is not None:
            self.statusBar().showMessage(
                f"Loaded {len(self._mock_entries):,} mock entries"
            )
            self._model_list.load_entries(self._mock_entries)
            self._builder_page.load_catalog(self._mock_entries)
            return

        prior = self._catalog_worker
        if prior is not None:
            try:
                if prior.isRunning():
                    prior.requestInterruption()
                    prior.quit()
                    prior.wait(2000)
            except RuntimeError:
                # C++ object already deleted — nothing to cancel.
                pass
            self._catalog_worker = None

        self._model_list.show_loading("Scanning CASC archive…")
        self.statusBar().showMessage(f"Scanning {game_dir}…")

        worker = CatalogWorker(game_dir, parent=self)
        worker.finished.connect(self._on_catalog_loaded)
        worker.error.connect(self._on_catalog_error)
        # Clear our reference once the worker completes — without this
        # the next access (e.g. from closeEvent) probes a zombie object
        # whose C++ side has already been deleteLater'd.
        worker.finished.connect(self._clear_catalog_worker_ref)
        worker.error.connect(self._clear_catalog_worker_ref)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._catalog_worker = worker
        worker.start()

    def _clear_catalog_worker_ref(self, *_args) -> None:
        self._catalog_worker = None

    def _on_catalog_loaded(self, entries: list) -> None:
        paths: list[str] = [str(e) for e in entries]
        self._model_list.load_entries(paths)
        self._builder_page.load_catalog(paths)
        self.statusBar().showMessage(f"Loaded {len(paths):,} models")
        # Banners can only be evaluated after the catalog has landed —
        # showing them earlier would race the loading placeholder.
        self._refresh_tact_banner()
        self._refresh_d4data_banner()

    def _on_catalog_error(self, message: str) -> None:
        log.error("Catalog load failed: %s", message)
        self._model_list.show_error(f"Failed to load catalog:\n{message}")
        self.statusBar().showMessage(f"Catalog load failed: {message}")

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def _begin_model_load(self, sno_path: str) -> None:
        if not self._mock_mode and self.game_dir is None:
            self._viewport_container.viewport.show_error(
                "No game directory configured."
            )
            return

        # Cancel any in-flight texture decode for the previous model —
        # its result, if it lands, would target a viewport that has
        # since moved on. ``_kick_texture_worker`` will also do this on
        # success but starting the cancellation here means we don't
        # waste cycles decoding textures the user no longer wants.
        self._cancel_texture_worker()

        # Session cache hit: the user already loaded this SNO at least
        # once this run. Render synchronously and skip both the .app
        # re-parse and (when available) the texture re-decode.
        cached_mesh = self._mesh_cache.get(sno_path)
        if cached_mesh is not None:
            self._cancel_load_worker()
            self._apply_cached_mesh(sno_path, cached_mesh)
            return

        self._cancel_load_worker()

        self.current_mesh_data = None
        self._export_button.set_enabled(False)

        self._viewport_container.viewport.show_loading("Extracting from CASC…")
        self._viewport_container.viewport.start_model_loading(1)
        self._viewport_container.properties.clear()
        self.statusBar().showMessage(f"Loading {sno_path}…")

        worker = LoadModelWorker(
            self.game_dir, sno_path,
            mock=self._mock_mode,
            d4data_path=self._settings.d4data_path(),
            parent=self,
        )
        worker.progress.connect(self._on_load_progress)
        worker.finished.connect(self._on_model_loaded)
        worker.error.connect(self._on_load_error)
        # Clear our reference as soon as the worker completes (either
        # signal). This prevents a follow-up click from probing a
        # zombie LoadModelWorker whose C++ side has been deleteLater'd.
        worker.finished.connect(self._clear_load_worker_ref)
        worker.error.connect(self._clear_load_worker_ref)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._load_worker = worker
        worker.start()

    def _clear_load_worker_ref(self, *_args) -> None:
        self._load_worker = None

    def _cancel_load_worker(self) -> None:
        """Stop the in-flight LoadModelWorker, if any.

        Factored out of ``_begin_model_load`` so both the cache-hit and
        cache-miss branches can share the same cancellation semantics
        — a stale worker's ``finished`` signal would otherwise overwrite
        the cached mesh we just rendered synchronously.
        """
        prior = self._load_worker
        if prior is None:
            return
        try:
            if prior.isRunning():
                prior.requestInterruption()
                try:
                    prior.finished.disconnect()
                    prior.progress.disconnect()
                    prior.error.disconnect()
                except (RuntimeError, TypeError):
                    pass
                prior.quit()
                prior.wait(2000)
        except RuntimeError:
            # C++ object already deleted — nothing to cancel.
            pass
        self._load_worker = None

    def _apply_cached_mesh(self, sno_path: str, mesh_data) -> None:
        """Render a previously-loaded MeshData synchronously.

        Mirrors the success branch of ``_on_model_loaded`` but without
        spawning ``LoadModelWorker``. If a texture pass also completed
        successfully on a prior visit, the cached texture dict is
        re-applied here too — re-clicking is fully synchronous.
        """
        try:
            self._viewport_container.viewport.load_mesh(mesh_data)
            self._viewport_container.properties.update_from(mesh_data)
            self._submesh_list.populate(list(mesh_data.submeshes))
        except Exception as exc:
            log.exception("Viewport mesh load failed (cached)")
            self._viewport_container.viewport.show_error(
                f"Failed to render: {exc}"
            )
            self._submesh_list.clear()
            # The cached entry just failed to render — drop it so the
            # next click attempts a fresh load instead of repeating the
            # failure.
            self._mesh_cache.pop(sno_path, None)
            self._texture_cache.pop(sno_path, None)
            self.current_mesh_data = None
            self._export_button.set_enabled(False)
            return

        # Cached MeshData may already carry resolved materials from a
        # prior texture/export run — promote those names into the row
        # labels right away, before the texture worker re-runs.
        cached_materials = getattr(mesh_data, "materials", None)
        if cached_materials:
            self._submesh_list.update_material_names(cached_materials)

        self.current_mesh_data = mesh_data
        if self._export_worker is None:
            self._export_button.set_enabled(True)

        n_verts = len(mesh_data.positions)
        n_tris = len(mesh_data.indices) // 3
        self.statusBar().showMessage(
            f"Loaded {mesh_data.name}: {n_verts:,} verts, {n_tris:,} tris"
        )

        cached_textures = self._texture_cache.get(sno_path)
        if cached_textures is not None:
            try:
                self._viewport_container.viewport.apply_submesh_textures(
                    mesh_data, cached_textures,
                )
            except Exception:
                log.exception("apply_submesh_textures failed (cached)")
                # Drop the cached textures and fall back to the worker
                # path so the user still gets textured rendering.
                self._texture_cache.pop(sno_path, None)
                self._kick_texture_worker(mesh_data, sno_path)
            return

        # Mesh was cached but textures weren't — kick the worker so the
        # viewport ends up textured. The texture worker's disk cache
        # makes this fast on the second visit.
        self._kick_texture_worker(mesh_data, sno_path)
        self._refresh_animation_for_model(mesh_data, sno_path)

    def _on_load_progress(self, message: str) -> None:
        self._viewport_container.viewport.show_loading(message)
        self.statusBar().showMessage(message)

    def _on_model_loaded(self, mesh_data) -> None:
        self._viewport_container.viewport.stop_model_loading()
        # Capture the sno_path the worker was started for *before* we
        # touch the viewport — using ``current_sno_path`` here would
        # mis-attribute the cache entry if the user has already clicked
        # forward to another model whose worker is still in flight.
        sno_path: str | None = None
        if self._load_worker is not None:
            sno_path = getattr(self._load_worker, "sno_path", None)

        try:
            self._viewport_container.viewport.load_mesh(mesh_data)
            self._viewport_container.properties.update_from(mesh_data)
            self._submesh_list.populate(list(mesh_data.submeshes))
        except Exception as exc:
            log.exception("Viewport mesh load failed")
            self._viewport_container.viewport.show_error(
                f"Failed to render: {exc}"
            )
            self._submesh_list.clear()
            self.current_mesh_data = None
            self._export_button.set_enabled(False)
            return

        self.current_mesh_data = mesh_data
        # Export is gated on having a model loaded. The button stays
        # disabled while an export is already in progress.
        if self._export_worker is None:
            self._export_button.set_enabled(True)

        n_verts = len(mesh_data.positions)
        n_tris = len(mesh_data.indices) // 3
        self.statusBar().showMessage(
            f"Loaded {mesh_data.name}: {n_verts:,} verts, {n_tris:,} tris"
        )

        # Populate the session mesh cache so a re-click renders
        # synchronously. Don't cache when the sno_path is unknown — that
        # only happens if the worker reference was already cleared, in
        # which case we'd have no safe key to use.
        if sno_path is not None:
            self._mesh_cache[sno_path] = mesh_data

        # Kick off the viewport texture pipeline once geometry is on
        # screen. It is best-effort — a missing d4data path or game
        # directory just leaves the model rendering with the existing
        # flat per-submesh colours.
        self._kick_texture_worker(mesh_data, sno_path)
        self._refresh_animation_for_model(mesh_data, sno_path)

    def _on_load_error(self, message: str) -> None:
        log.error("Model load failed: %s", message)
        self._viewport_container.viewport.stop_model_loading()
        self._viewport_container.viewport.show_error(message)
        self._viewport_container.properties.clear()
        self._submesh_list.clear()
        self.current_mesh_data = None
        self._export_button.set_enabled(False)
        self._reset_animation_bar()
        self.statusBar().showMessage(f"Load failed: {message}")

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export_shortcut(self) -> None:
        """Trigger the export button's primary action (Ctrl+E or File menu)."""
        if self._export_worker is not None:
            return
        # Active tab decides which side has exportable content; the
        # builder doesn't track ``current_mesh_data`` so checking it
        # here would gate the shortcut off whenever the builder tab is
        # foregrounded.
        if self._tabs.currentIndex() == TAB_CHARACTER_BUILDER:
            if not self._builder_page.has_export_data():
                return
        else:
            if self.current_mesh_data is None:
                return
        # Reuse the same code path as clicking the button so options /
        # busy state stay in sync.
        opts = self._export_button.get_export_options(format=FMT_GLB)
        self._on_export_requested(opts)

    def _on_export_requested(self, options: dict) -> None:
        # Route by active tab — the Character Builder has its own merge
        # + filter pipeline, and ``current_mesh_data`` is exclusively a
        # Model Browser concern.
        if self._tabs.currentIndex() == TAB_CHARACTER_BUILDER:
            self._export_builder(options)
            return
        if self.current_mesh_data is None:
            return
        if self._export_worker is not None:
            self.statusBar().showMessage("Export already in progress")
            return

        # Always respect the submesh checkbox state. ``None`` means
        # every row is checked (or there are no rows) — the worker
        # treats that as "no filter," so it's the same as exporting
        # the whole model.
        visible = self._submesh_list.get_visible_indices()
        if visible is not None and not visible:
            self.statusBar().showMessage(
                "Export Selected: no submeshes are selected to export",
                5_000,
            )
            return
        submesh_filter = visible

        options = dict(options)
        fmt = options.pop("format", FMT_GLB)

        # If the user asked for textures or a sidecar but never set a
        # d4data path, surface the choice. ``_resolve_d4data_dependent``
        # may strip those flags or abort the export entirely.
        options = self._resolve_d4data_dependent(options)
        if options is None:
            return  # user cancelled

        out_path = self._prompt_export_path(fmt)
        if out_path is None:
            return

        # Persist the directory the user just exported into.
        # ``out_path`` is now ``<parent>/<stem>/<stem>.<fmt>`` — persist
        # the *parent* the user picked, not the per-model subfolder, so
        # the next dialog opens back at the same chosen root.
        self._settings.set_last_export_dir(out_path.parent.parent)

        # Pull every discovered animation for this appearance out of
        # the GUI's discovery cache and let the worker bulk-decode them
        # on its thread. Decoding all on the main thread would freeze
        # the UI; passing AnimationInfo objects keeps that work off
        # the event loop. If discovery hasn't completed yet (or the
        # appearance has no animations), we just ship a static .glb.
        anim_infos: list = []
        rest_pose_map: dict = {}
        appearance = self._appearance_name_for(
            self.current_mesh_data, self.current_sno_path,
        )
        if appearance:
            anim_infos = list(
                self._anim_discovery_cache.get(appearance, []),
            )
        if anim_infos:
            rest_pose_map = self._rest_pose_map_for(self.current_mesh_data)

        # Honor the "Include Animations" export toggle.
        anim_infos, rest_pose_map = self._gate_animations(
            options, anim_infos, rest_pose_map,
        )

        worker = ExportWorker(
            mesh_data=self.current_mesh_data,
            output_path=out_path,
            options=options,
            game_dir=self.game_dir,
            d4data_path=self._settings.d4data_path(),
            submesh_filter=submesh_filter,
            anim_infos=anim_infos or None,
            rest_pose_map=rest_pose_map or None,
            parent=self,
        )
        worker.progress.connect(self._on_export_progress)
        worker.warning.connect(self._on_export_warning)
        worker.finished.connect(self._on_export_finished)
        worker.error.connect(self._on_export_error)
        worker.finished.connect(self._clear_export_worker_ref)
        worker.error.connect(self._clear_export_worker_ref)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._export_worker = worker
        self._export_button.set_busy(True)
        self.statusBar().showMessage("Preparing export…")
        worker.start()

    def _export_builder(self, options: dict) -> None:
        """Route a Character Builder export to the right pipeline.

        With skinned pieces equipped, each piece is exported as its own
        glTF mesh node sharing one skin (``_export_builder_pieces``) so
        Blender shows one Armature with N independently-toggleable mesh
        objects. A weapons-only assembly (no skeleton) falls back to the
        legacy single-mesh path (``_export_builder_merged``).
        """
        if self._export_worker is not None:
            self.statusBar().showMessage("Export already in progress")
            return

        pieces_data = self._builder_page.get_export_pieces()
        if pieces_data is None:
            self.statusBar().showMessage(
                "Nothing to export — equip some pieces first", 5_000,
            )
            return

        skinned_pieces, canonical_skeleton, static_extras = pieces_data
        if skinned_pieces:
            self._export_builder_pieces(
                options, skinned_pieces, canonical_skeleton, static_extras,
            )
        else:
            # No skinned pieces — a weapons-only scene. The legacy
            # single-mesh path already handles a static-only export.
            self._export_builder_merged(options)

    def _export_builder_pieces(
        self,
        options: dict,
        skinned_pieces: list,
        canonical_skeleton,
        static_extras: list,
    ) -> None:
        """Spawn an ExportWorker for the per-piece Character Builder path.

        Each ``(MeshData, submesh_filter)`` in ``skinned_pieces`` becomes
        its own glTF mesh node, all sharing one skin built from
        ``canonical_skeleton``. ``static_extras`` (weapons) ride along
        as separate unskinned mesh nodes.

        ``prune_bones`` is forced ``False``: every piece's JOINTS_0
        addresses the unpruned canonical skeleton (shared template id),
        so pruning would re-index bones without rewriting the other
        pieces' joint streams.
        """
        # Nothing survives if every piece's submesh filter is an empty
        # set ("Export Selected" with no rows checked). A ``None`` filter
        # means "all visible", so it always counts as content.
        def _has_content(f) -> bool:
            return f is None or bool(f)

        if not any(
            _has_content(f)
            for _m, f in (*skinned_pieces, *static_extras)
        ):
            self.statusBar().showMessage(
                "Export Selected: no submeshes are selected to export",
                5_000,
            )
            return

        options = dict(options)
        # CRITICAL: see method docstring. Forced regardless of the
        # user's menu setting.
        options["prune_bones"] = False
        fmt = options.pop("format", FMT_GLB)

        options = self._resolve_d4data_dependent(options)
        if options is None:
            return  # user cancelled

        out_path = self._prompt_export_path(fmt, default_stem="character")
        if out_path is None:
            return

        # ``out_path`` is now ``<parent>/<stem>/<stem>.<fmt>`` — persist
        # the *parent* the user picked, not the per-model subfolder, so
        # the next dialog opens back at the same chosen root.
        self._settings.set_last_export_dir(out_path.parent.parent)

        # Bundle every discovered animation into the export, mirroring
        # the Model Browser path. Decoding stays on the worker thread —
        # we hand over AnimationInfo objects, not decoded clips.
        anim_infos = self._builder_page.get_export_anim_infos()
        # Animations are skeleton-bound; gate on a real canonical
        # skeleton. ``rest_pose_map`` is bone-hash-keyed, so the one map
        # built from the canonical skeleton applies to every piece.
        skinned = (
            canonical_skeleton is not None
            and bool(getattr(canonical_skeleton, "bones", None))
        )
        rest_pose_map = (
            self._rest_pose_map_for_skeleton(canonical_skeleton)
            if skinned else None
        )
        if not skinned:
            anim_infos = []

        # Honor the "Include Animations" export toggle.
        anim_infos, rest_pose_map = self._gate_animations(
            options, anim_infos, rest_pose_map,
        )

        worker = ExportWorker(
            output_path=out_path,
            options=options,
            game_dir=self.game_dir,
            d4data_path=self._settings.d4data_path(),
            skinned_pieces=skinned_pieces,
            canonical_skeleton=canonical_skeleton,
            extra_meshes=static_extras or None,
            anim_infos=anim_infos or None,
            rest_pose_map=rest_pose_map or None,
            parent=self,
        )
        worker.progress.connect(self._on_export_progress)
        worker.warning.connect(self._on_export_warning)
        worker.finished.connect(self._on_export_finished)
        worker.error.connect(self._on_export_error)
        worker.finished.connect(self._clear_export_worker_ref)
        worker.error.connect(self._clear_export_worker_ref)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._export_worker = worker
        self._export_button.set_busy(True)
        self.statusBar().showMessage("Preparing assembled export…")
        worker.start()

    def _export_builder_merged(self, options: dict) -> None:
        """Legacy single-mesh Character Builder export.

        Used only for a weapons-only assembly (no skinned body, hence no
        skeleton). Sources its mesh + submesh filter + static extras
        from ``CharacterBuilderPage.get_export_data`` and forces
        ``prune_bones=False`` for the same reason documented on
        ``_export_builder_pieces``.
        """
        export_data = self._builder_page.get_export_data()
        if export_data is None:
            self.statusBar().showMessage(
                "Nothing to export — equip some pieces first", 5_000,
            )
            return

        mesh_data, submesh_filter, extra_meshes = export_data
        if submesh_filter is not None and not submesh_filter:
            self.statusBar().showMessage(
                "Export Selected: no submeshes are selected to export",
                5_000,
            )
            return

        options = dict(options)
        # CRITICAL: see method docstring. Forced regardless of the
        # user's menu setting.
        options["prune_bones"] = False
        fmt = options.pop("format", FMT_GLB)

        options = self._resolve_d4data_dependent(options)
        if options is None:
            return  # user cancelled

        out_path = self._prompt_export_path(
            fmt, default_stem=getattr(mesh_data, "name", None) or "character",
        )
        if out_path is None:
            return

        # ``out_path`` is now ``<parent>/<stem>/<stem>.<fmt>`` — persist
        # the *parent* the user picked, not the per-model subfolder, so
        # the next dialog opens back at the same chosen root.
        self._settings.set_last_export_dir(out_path.parent.parent)

        # Bundle every discovered animation into the export, mirroring
        # the Model Browser path. Decoding stays on the worker thread —
        # we hand over AnimationInfo objects, not decoded clips.
        anim_infos = self._builder_page.get_export_anim_infos()
        # MeshData has no ``.bones`` — the skeleton owns them. Animations
        # are meaningless without a skeleton (a weapons-only export root
        # has none), so gate on the skeleton rather than on whether a
        # prefix was discovered: class+gender can be set while the FACE
        # slot — the skinned base body — isn't.
        skel = getattr(mesh_data, "skeleton", None)
        skinned = skel is not None and bool(getattr(skel, "bones", None))
        rest_pose_map = self._rest_pose_map_for(mesh_data) if skinned else None
        if not skinned:
            anim_infos = []

        # Honor the "Include Animations" export toggle.
        anim_infos, rest_pose_map = self._gate_animations(
            options, anim_infos, rest_pose_map,
        )

        worker = ExportWorker(
            mesh_data=mesh_data,
            output_path=out_path,
            options=options,
            game_dir=self.game_dir,
            d4data_path=self._settings.d4data_path(),
            submesh_filter=submesh_filter,
            extra_meshes=extra_meshes or None,
            anim_infos=anim_infos or None,
            rest_pose_map=rest_pose_map or None,
            parent=self,
        )
        worker.progress.connect(self._on_export_progress)
        worker.warning.connect(self._on_export_warning)
        worker.finished.connect(self._on_export_finished)
        worker.error.connect(self._on_export_error)
        worker.finished.connect(self._clear_export_worker_ref)
        worker.error.connect(self._clear_export_worker_ref)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._export_worker = worker
        self._export_button.set_busy(True)
        self.statusBar().showMessage("Preparing assembled export…")
        worker.start()

    @staticmethod
    def _gate_animations(
        options: dict,
        anim_infos: list | None,
        rest_pose_map: dict | None,
    ) -> tuple[list | None, dict | None]:
        """Apply the "Include Animations" export toggle.

        Returns ``(anim_infos, rest_pose_map)`` unchanged when the toggle
        is on (the default), or ``(None, None)`` when off. Passing
        ``None`` for both to ``ExportWorker`` skips .ani decoding and the
        synthetic ``rest_pose`` entirely, so the glb is written with zero
        glTF animations. Shared by all three export paths (Model Browser,
        per-piece builder, merged builder).
        """
        if not options.get("include_animations", True):
            return None, None
        return anim_infos, rest_pose_map

    def _resolve_d4data_dependent(self, options: dict) -> dict | None:
        """Prompt the user when texture/sidecar requested without d4data.

        Returns the (possibly modified) options dict, or ``None`` if the
        user cancelled the export.
        """
        wants_textures = bool(options.get("embed_textures"))
        wants_sidecar = bool(options.get("write_materials_sidecar"))
        if not (wants_textures or wants_sidecar):
            return options
        if self._settings.d4data_path() is not None:
            return options

        which = (
            "Texture embedding" if wants_textures else "Materials sidecar"
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("D4Data path required")
        box.setText(
            f"{which} requires a D4Data path "
            f"(extracted game JSON data). What would you like to do?"
        )
        set_btn = box.addButton("Set Path…", QMessageBox.AcceptRole)
        skip_btn = box.addButton(
            "Export Without Textures", QMessageBox.DestructiveRole,
        )
        cancel_btn = box.addButton(QMessageBox.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is set_btn:
            picked = self._export_button.open_set_d4data_dialog()
            if picked is None:
                return None
            return options
        if clicked is skip_btn:
            options["embed_textures"] = False
            options["write_materials_sidecar"] = False
            return options
        # Cancel (or window-close) aborts.
        return None

    def _prompt_export_path(
        self, fmt: str, *, default_stem: str | None = None,
    ) -> Path | None:
        """Pick a parent directory; build ``<root>/<stem>/<stem>.<fmt>``.

        Each export now lives in its own model-named folder so loose
        textures, the sidecar, and (for split-file glTF) the buffer all
        co-locate next to the model file. The user picks the *parent*
        directory once and every per-model layout decision is owned by
        :func:`resolve_export_paths` — both this dialog and the CLI go
        through it so the on-disk shape is identical from either path.
        ``last_export_dir`` therefore stores the chosen parent, not the
        model folder, so the next dialog opens at the same parent.
        """
        from d4extract.export.gltf_export import resolve_export_paths

        suffix = ".glb" if fmt == FMT_GLB else ".gltf"

        if default_stem:
            # Caller supplied an explicit stem (e.g. the Character
            # Builder's assembled-mesh name). Wins over current_mesh_data
            # which reflects Model Browser state and isn't meaningful in
            # the builder flow.
            stem = default_stem
        elif self.current_mesh_data is not None:
            stem = getattr(self.current_mesh_data, "name", None) or "model"
        elif self.current_sno_path:
            stem = self.current_sno_path.rsplit("/", 1)[-1]
            if stem.endswith(".app"):
                stem = stem[:-4]
        else:
            stem = "model"

        last_dir = self._settings.last_export_dir()
        start_dir = str(last_dir) if last_dir is not None else ""

        chosen = QFileDialog.getExistingDirectory(
            self,
            f"Export {stem}{suffix} — pick parent folder",
            start_dir,
        )
        if not chosen:
            return None
        _model_dir, model_file, _textures_dir = resolve_export_paths(
            Path(chosen), stem, suffix,
        )
        return model_file

    def _on_export_progress(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def _on_export_warning(self, message: str) -> None:
        # Buffer the warning so we can show it in a single dialog after
        # the export reports finished — surfacing it now would be
        # overwritten by the success message in the status bar.
        log.warning("Export warning: %s", message)
        self._pending_export_warnings = getattr(
            self, "_pending_export_warnings", [],
        )
        self._pending_export_warnings.append(message)

    def _on_export_finished(self, message: str) -> None:
        self.statusBar().showMessage(message, 10_000)
        self._export_button.set_busy(False)
        # Re-enable against whichever tab the user is currently on, not
        # whichever side spawned the export (they may have switched
        # tabs while the worker was running).
        self._refresh_export_button_enabled()
        warnings = getattr(self, "_pending_export_warnings", None)
        if warnings:
            self._pending_export_warnings = []
            QMessageBox.warning(
                self,
                "Export completed with warnings",
                message + "\n\n" + "\n\n".join(warnings),
            )

    def _on_export_error(self, message: str) -> None:
        log.error("Export failed: %s", message)
        self._export_button.set_busy(False)
        self._refresh_export_button_enabled()
        self.statusBar().showMessage(f"Export failed: {message}")
        QMessageBox.critical(self, "Export failed", message)

    def _refresh_export_button_enabled(self) -> None:
        """Set the export button enabled state from the active tab."""
        if self._tabs.currentIndex() == TAB_CHARACTER_BUILDER:
            self._export_button.set_enabled(
                self._builder_page.has_export_data(),
            )
        else:
            self._export_button.set_enabled(self.current_mesh_data is not None)

    def _clear_export_worker_ref(self, *_args) -> None:
        self._export_worker = None

    # ------------------------------------------------------------------
    # Viewport textures
    # ------------------------------------------------------------------

    def _kick_texture_worker(
        self, mesh_data, sno_path: str | None,
    ) -> None:
        """Spawn the viewport texture pipeline if the prereqs are met.

        Skips silently when running in mock mode, when the game dir is
        unset, or when no d4data path is configured — texturing is a
        best-effort enhancement on top of the flat-coloured viewport,
        not a hard requirement.
        """
        # Always cancel any prior worker first; even if we end up not
        # spawning a new one we don't want a stale result to land.
        self._cancel_texture_worker()

        if self._mock_mode:
            log.info("Texture worker skipped: mock mode")
            return
        if self.game_dir is None:
            log.info("Texture worker skipped: no game_dir")
            return
        d4data = self._settings.d4data_path()
        if d4data is None:
            log.info("Texture worker skipped: no d4data path configured")
            return
        # Worker can't write back into the texture cache without a key.
        # Caller-of-last-resort guard — under normal flows ``sno_path``
        # is supplied by the load handler or the cache-hit path.
        if sno_path is None:
            log.info("Texture worker skipped: no sno_path")
            return

        worker = TextureWorker(
            mesh_data,
            game_dir=self.game_dir,
            d4data_path=d4data,
            mesh_token=mesh_data,
            sno_path=sno_path,
            parent=self,
        )
        worker.progress.connect(self._on_texture_progress)
        worker.finished.connect(self._on_textures_ready)
        worker.error.connect(self._on_texture_error)
        worker.finished.connect(self._clear_texture_worker_ref)
        worker.error.connect(self._clear_texture_worker_ref)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._texture_worker = worker
        worker.start()
        # Show the floating spinner immediately. The first ``progress``
        # signal will update the caption (e.g. "Resolving materials…",
        # "Extracting N textures…", "Decoding textures…"); the worker's
        # finished/error handlers below tear it down.
        try:
            self._viewport_container.viewport.start_texture_loading()
        except Exception:
            log.debug("start_texture_loading failed", exc_info=True)
        log.info(
            "TextureWorker spawned for %r",
            getattr(mesh_data, "name", None),
        )

    def _cancel_texture_worker(self) -> None:
        prior = self._texture_worker
        if prior is None:
            return
        try:
            if prior.isRunning():
                prior.requestInterruption()
                try:
                    prior.finished.disconnect()
                    prior.progress.disconnect()
                    prior.error.disconnect()
                except (RuntimeError, TypeError):
                    pass
                prior.quit()
                prior.wait(2000)
        except RuntimeError:
            # C++ object already deleted — nothing to cancel.
            pass
        self._texture_worker = None
        # Cancellation also has to drop the spinner — otherwise the
        # next selection sees a leftover indicator from the previous
        # in-flight worker.
        self._stop_texture_indicator()

    def _clear_texture_worker_ref(self, *_args) -> None:
        self._texture_worker = None
        # Always tear down the spinner when the worker reports back —
        # finished AND error connect to this slot. Previously the
        # ``_on_textures_ready`` early-return on an empty dict left the
        # status bar pinned to "Resolving materials…" indefinitely
        # (e.g. for environment statics whose materials have no
        # BASE_COLOR slot).
        self._stop_texture_indicator()

    def _on_texture_progress(self, message: str) -> None:
        # Mirror progress to both the status bar (existing behavior)
        # and the floating viewport spinner (new). Users can read
        # whichever is more visible to them.
        self.statusBar().showMessage(message)
        try:
            self._viewport_container.viewport.update_texture_loading_message(
                message,
            )
        except Exception:
            log.debug("update_texture_loading_message failed", exc_info=True)

    def _on_textures_ready(self, mesh_token, submesh_textures) -> None:
        log.info(
            "Textures ready: %d entries (token match=%s)",
            len(submesh_textures or {}),
            mesh_token is self.current_mesh_data,
        )
        # Capture the worker's sno_path before the success/failure
        # branches diverge. ``_clear_texture_worker_ref`` is connected
        # after this slot, so ``self._texture_worker`` is still set.
        sno_path: str | None = None
        if self._texture_worker is not None:
            sno_path = getattr(self._texture_worker, "sno_path", None)

        # The token guards against late results from a prior selection.
        # The viewport applies the same identity check, but bailing here
        # avoids the function-call overhead and the status-bar update.
        if mesh_token is not self.current_mesh_data:
            return

        # Material resolution happens before texture decode inside the
        # worker, so even an empty texture dict means materials are now
        # attached to the MeshData. Promote those names into the row
        # labels regardless of whether any BASE_COLOR images landed.
        materials = getattr(mesh_token, "materials", None)
        if materials:
            self._submesh_list.update_material_names(materials)

        if not submesh_textures:
            # No textures landed (e.g. environment statics whose
            # materials have no BASE_COLOR slot). Restore the status
            # bar to the "Loaded …" line that the load worker set so
            # the user doesn't think we're still working.
            self._restore_loaded_status(mesh_token)
            return
        try:
            self._viewport_container.viewport.apply_submesh_textures(
                mesh_token, submesh_textures,
            )
        except Exception:
            log.exception("apply_submesh_textures failed")
            self._restore_loaded_status(mesh_token)
            return
        n = len(submesh_textures)
        self.statusBar().showMessage(
            f"Applied {n} viewport texture(s)", 5_000,
        )

        # Cache the decoded textures so the next click on this SNO
        # re-applies them synchronously, skipping the texture worker
        # entirely. We only cache after a successful apply — failed
        # applies can't be safely replayed.
        if sno_path is not None:
            self._texture_cache[sno_path] = submesh_textures

    def _on_texture_error(self, message: str) -> None:
        # Texturing is best-effort; surface the failure to the log so
        # it's debuggable but keep the existing flat-color render.
        log.warning("Texture worker error: %s", message)
        # The spinner and the stale status bar both come down via
        # ``_clear_texture_worker_ref``, but make sure the user sees a
        # meaningful final state.
        if self.current_mesh_data is not None:
            self._restore_loaded_status(self.current_mesh_data)

    def _restore_loaded_status(self, mesh_data) -> None:
        """Re-emit the post-load status line after a texture pass ends."""
        if mesh_data is None:
            return
        try:
            n_verts = len(mesh_data.positions)
            n_tris = len(mesh_data.indices) // 3
            name = getattr(mesh_data, "name", None) or "model"
        except Exception:
            return
        self.statusBar().showMessage(
            f"Loaded {name}: {n_verts:,} verts, {n_tris:,} tris"
        )

    def _stop_texture_indicator(self) -> None:
        try:
            self._viewport_container.viewport.stop_texture_loading()
        except Exception:
            log.debug("stop_texture_loading failed", exc_info=True)

    # ------------------------------------------------------------------
    # Menu / shortcut handlers
    # ------------------------------------------------------------------

    def _on_change_game_dir(self) -> None:
        self._setup_card.refresh()
        self._stack.setCurrentIndex(PAGE_SETUP)

    def _on_menu_set_d4data(self) -> None:
        self._export_button.open_set_d4data_dialog()

    def _on_menu_set_d4data_folder(self) -> None:
        """Pick an existing d4data checkout and persist it.

        Unlike the older ``Set D4Data Path…`` action (which simply
        accepts whatever directory the user picks), this one validates
        via :func:`d4extract.setup.is_d4data_dir` so the user can't
        save a parent or sibling directory by accident — the kind of
        misconfiguration that leads to silent material-resolution
        failures elsewhere in the app.
        """
        from d4extract.setup import is_d4data_dir

        existing = self._settings.d4data_path()
        start_dir = str(existing) if existing is not None else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Select d4data folder", start_dir,
        )
        if not chosen:
            return
        path = Path(chosen)
        if not is_d4data_dir(path):
            QMessageBox.warning(
                self, "d4data",
                f"{path}\n\nThis doesn't look like a d4data checkout — "
                "it should contain a 'base/' subdirectory. Pick the "
                "repo root, not its parent.",
            )
            return
        self._settings.set_d4data_path(path)
        self.statusBar().showMessage(f"d4data: set to {path}")
        # Refresh dependent state (banner + builder + indices) the same
        # way the card's signal would.
        self._on_d4data_ready(path)
        if hasattr(self, "_refresh_d4data_banner"):
            self._refresh_d4data_banner()

    def _on_menu_redownload_d4data(self) -> None:
        """Confirm, then re-run the download worker against the default dir.

        Useful when Blizzard ships a patch and the community updates
        d4data: the user clicks once instead of finding the cached
        zipball location and clearing it manually. The current install
        (if any) is left in place by the worker and atomically
        replaced on success.
        """
        from d4extract.setup import default_d4data_dir

        target = default_d4data_dir()
        if QMessageBox.question(
            self, "Re-download d4data",
            f"Fetch the latest d4data snapshot and install to:\n\n{target}\n\n"
            "Your existing copy will be replaced atomically once the "
            "download completes successfully.",
        ) != QMessageBox.Yes:
            return
        # Route through the existing card — it owns the QProgressBar,
        # the worker lifecycle, and the d4data_ready emission.
        self._stack.setCurrentIndex(PAGE_D4DATA)
        self._d4data_card.start_download()

    def _on_menu_load_tact_keys(self) -> None:
        """Pick a wowdev-format TACT key file and cache it locally.

        Validation, copy, and QSettings persistence all go through
        :func:`d4extract.config.set_tact_keys_path` so the GUI and CLI
        paths share one implementation.
        """
        cached = self._settings.tact_keys_path()
        start_dir = str(cached.parent if cached is not None else Path.home())
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Select TACT key file",
            start_dir,
            "TACT key files (*.txt *.csv *.tactkey *.tact *.keys);;All files (*)",
        )
        if not path_str:
            return
        src = Path(path_str)
        try:
            from d4extract.config import count_valid_keys, set_tact_keys_path

            cached_path = set_tact_keys_path(src)
            n = count_valid_keys(cached_path)
        except ValueError as exc:
            QMessageBox.warning(
                self,
                "TACT keys",
                f"Could not load keys from {src.name}: {exc}\n\n"
                "Expected wowdev format — one key per line, KEYID and KEY "
                "as space- or semicolon-separated hex (16 + 32 chars).",
            )
            return
        # Loading keys implicitly answers the first-run banner.
        self._settings.set_tact_banner_dismissed(True)
        self._refresh_tact_banner()
        self._refresh_tact_status()
        self.statusBar().showMessage(
            f"TACT keys: loaded {n} from {cached_path.name}"
        )

    def _on_menu_clear_tact_keys(self) -> None:
        """Forget the cached TACT keys after explicit confirmation."""
        confirmed = QMessageBox.question(
            self,
            "Clear TACT keys",
            "Forget the loaded TACT key file? Encrypted content will be "
            "skipped on the next extraction.",
        )
        if confirmed != QMessageBox.Yes:
            return
        from d4extract.config import clear_tact_keys

        clear_tact_keys()
        # ``config.clear_tact_keys`` already clears QSettings, but call
        # the AppSettings helper too — it's a no-op on the second pass
        # and keeps the abstraction symmetrical with the load path.
        self._settings.clear_tact_keys_path()
        self._refresh_tact_banner()
        self._refresh_tact_status()
        self.statusBar().showMessage("TACT keys: cleared")

    def _is_browser_tab_active(self) -> bool:
        """True only when the user is on the Model Browser tab.

        Used to gate the Model-Browser-scoped shortcuts (Ctrl+F focuses
        the filter; W toggles wireframe; F resets view) so they don't
        fire on widgets the user can't see.
        """
        return (
            self._stack.currentIndex() == PAGE_MAIN
            and self._tabs.currentIndex() == TAB_BROWSER
        )

    def _on_tab_changed(self, index: int) -> None:
        """Re-evaluate export-button state and pause/resume builder anim."""
        # Pause builder animation playback when leaving its tab, resume
        # on return. Done before the mid-export early-return so the
        # off-screen QTimer is parked even while an export runs.
        self._builder_page.set_tab_active(index == TAB_CHARACTER_BUILDER)
        if self._export_worker is not None:
            # Mid-export: the busy state owns the button; let the
            # export's finished/error handlers re-enable it.
            return
        if index == TAB_CHARACTER_BUILDER:
            self._export_button.set_enabled(
                self._builder_page.has_export_data(),
            )
        else:
            self._export_button.set_enabled(self.current_mesh_data is not None)

    def _on_builder_assembly_changed(self, has_content: bool) -> None:
        """Mirror builder content changes onto the export button.

        Only takes effect while the builder tab is foregrounded — the
        Model Browser side keeps using its own ``current_mesh_data``
        gate.
        """
        if self._export_worker is not None:
            return
        if self._tabs.currentIndex() == TAB_CHARACTER_BUILDER:
            self._export_button.set_enabled(has_content)

    def _on_focus_filter(self) -> None:
        if self._is_browser_tab_active():
            self._model_list.focus_filter()

    def _on_toggle_wireframe(self) -> None:
        if self._is_browser_tab_active():
            self._viewport_container.viewport.toggle_wireframe()

    def _on_reset_view(self) -> None:
        if self._is_browser_tab_active():
            self._viewport_container.viewport.reset_view()

    def _on_submesh_visibility_changed(
        self, sm_idx: int, visible: bool,
    ) -> None:
        self._viewport_container.viewport.set_submesh_visible(sm_idx, visible)

    def _on_submesh_bulk_visibility_changed(self, visible: bool) -> None:
        self._viewport_container.viewport.set_all_submeshes_visible(visible)

    def _on_model_selected(self, sno_path: str) -> None:
        self.current_sno_path = sno_path
        self._begin_model_load(sno_path)

    # ------------------------------------------------------------------
    # Animation pipeline
    # ------------------------------------------------------------------

    def _refresh_animation_for_model(
        self, mesh_data, sno_path: str | None,
    ) -> None:
        """Reset the animation bar and (re)spawn discovery for a new model.

        Called from both the fresh-load and cache-hit paths so the bar's
        state always tracks the currently-rendered mesh.  The bar is
        hidden for static / non-skinned models and for skinned models
        when no d4data path is configured.
        """
        # First — pause and reset whatever was on the bar before.  This
        # also stops any in-flight playback timer.
        self._cancel_anim_load_worker()
        self._cancel_anim_discovery_worker()
        self._current_animation = None
        self._current_animation_info = None
        self._viewport_container.viewport.clear_animation()
        bar = self._viewport_container.animation_bar

        skel = getattr(mesh_data, "skeleton", None)
        is_skinned = (
            skel is not None
            and bool(getattr(skel, "bones", None))
            and bool(getattr(mesh_data, "joints", None))
            and bool(getattr(mesh_data, "weights", None))
        )
        if not is_skinned:
            bar.reset()
            bar.setVisible(False)
            return

        d4data = self._settings.d4data_path()
        if d4data is None:
            bar.reset()
            bar.set_inactive(
                "Set a d4data path (File → Set D4Data Path…) to load animations"
            )
            bar.setVisible(True)
            return

        appearance = self._appearance_name_for(mesh_data, sno_path)
        if not appearance:
            bar.reset()
            bar.set_inactive("Could not determine appearance name")
            bar.setVisible(True)
            return

        # Cache hit: skip the worker and populate the dropdown right
        # away.  Cache misses kick the discovery worker on a thread.
        cached = self._anim_discovery_cache.get(appearance)
        if cached is not None:
            bar.set_animations(cached)
            bar.setVisible(True)
            return

        bar.set_animations([])
        bar.set_inactive("Indexing animations…")
        bar.setVisible(True)
        self._pending_discovery_appearance = appearance

        worker = AnimDiscoveryWorker(appearance, d4data, parent=self)
        worker.finished.connect(self._on_animations_discovered)
        worker.error.connect(self._on_animation_discovery_error)
        worker.progress.connect(self._on_animation_discovery_progress)
        worker.finished.connect(self._clear_anim_discovery_worker_ref)
        worker.error.connect(self._clear_anim_discovery_worker_ref)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._anim_discovery_worker = worker
        worker.start()

    def _appearance_name_for(self, mesh_data, sno_path: str | None) -> str:
        """Best-effort recovery of the appearance stem for d4data lookup."""
        name = getattr(mesh_data, "name", None)
        if isinstance(name, str) and name:
            return name
        if not sno_path:
            return ""
        last = sno_path.rsplit("/", 1)[-1]
        if last.endswith(".app"):
            last = last[:-4]
        return last

    def _on_animations_discovered(
        self, appearance: str, infos: list, is_complete: bool,
    ) -> None:
        # Drop late results that target a model the user has moved off.
        if appearance != self._pending_discovery_appearance:
            return
        self._current_discovery_is_complete = is_complete
        bar = self._viewport_container.animation_bar
        if is_complete:
            self._anim_discovery_cache[appearance] = list(infos)
            bar.set_animations(infos)
            if not infos:
                bar.set_inactive("No animations found for this appearance")
        else:
            # Prefix-glob (fast) result — show it but indicate more may come.
            bar.set_animations(infos)
            if infos:
                bar.set_loading_message("Showing fast matches — full index building…")
            else:
                bar.set_loading_message("Scanning — full index building…")

    def _on_animation_discovery_error(self, message: str) -> None:
        log.warning("Animation discovery failed: %s", message)
        bar = self._viewport_container.animation_bar
        bar.set_inactive(f"Discovery failed: {message[:80]}")

    def _on_animation_discovery_progress(self, message: str) -> None:
        bar = self._viewport_container.animation_bar
        bar.set_loading_message(message)

    def _begin_anim_index_build(self, d4data_path: Path) -> None:
        """Start the background animation index build worker (idempotent)."""
        from d4extract.gui import anim_lookup
        key = Path(d4data_path).resolve()
        with anim_lookup._INDEX_LOCK:
            if key in anim_lookup._INDEX_CACHE:
                # Index already built (warm process) — the worker would
                # never spawn and never fire ``index_ready``, so notify
                # the builder directly.
                self._builder_page.set_anim_index_ready(True)
                return
        prior = self._anim_index_build_worker
        if prior is not None:
            try:
                if prior.isRunning():
                    return
            except RuntimeError:
                pass
        worker = AnimIndexBuildWorker(d4data_path, parent=self)
        worker.index_ready.connect(self._on_anim_index_ready)
        # The Character Builder waits on the same index for its own
        # animation discovery — let it know when the build completes.
        worker.index_ready.connect(self._builder_page.set_anim_index_ready)
        worker.progress.connect(self._on_anim_index_build_progress)
        worker.finished.connect(self._clear_anim_index_build_worker_ref)
        worker.finished.connect(worker.deleteLater)
        self._anim_index_build_worker = worker
        worker.start()

    def _on_anim_index_ready(self) -> None:
        """Called when the full index is in cache — refresh if bar was a fast result."""
        if self._current_discovery_is_complete:
            return
        appearance = self._pending_discovery_appearance
        if appearance is None:
            return
        d4data = self._settings.d4data_path()
        if d4data is None:
            return
        from d4extract.gui import anim_lookup
        full_index = anim_lookup.get_cached_index(d4data)
        if full_index is None:
            return
        infos = list(full_index.get(appearance, ()))
        self._anim_discovery_cache[appearance] = infos
        self._current_discovery_is_complete = True
        bar = self._viewport_container.animation_bar
        bar.set_animations(infos)
        if not infos:
            bar.set_inactive("No animations found for this appearance")

    def _on_anim_index_build_progress(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def _clear_anim_index_build_worker_ref(self, *_args) -> None:
        self._anim_index_build_worker = None

    def _begin_item_index_build(self, d4data_path: Path) -> None:
        """Start the background equipment-name index build (idempotent)."""
        from d4extract.gui import item_lookup
        key = Path(d4data_path).resolve()
        with item_lookup._INDEX_LOCK:
            if key in item_lookup._INDEX_CACHE:
                # Already built (warm process) — the worker would never
                # spawn and never fire ``index_ready``, so notify the
                # builder directly.
                self._builder_page.set_item_index_ready(True)
                # The model list's resolver consults the same index;
                # refresh so any rows that fell back to stems now pick
                # up display names.
                self._model_list.refresh_display_names()
                return
        prior = self._item_index_build_worker
        if prior is not None:
            try:
                if prior.isRunning():
                    return
            except RuntimeError:
                pass
        worker = ItemIndexBuildWorker(d4data_path, parent=self)
        # The Character Builder labels equipment pieces from this index;
        # let it refresh once the build completes.
        worker.index_ready.connect(self._builder_page.set_item_index_ready)
        # The Model Browser's row labels share this index too — refresh
        # so cold-startup stems get replaced with in-game names.
        worker.index_ready.connect(self._model_list.refresh_display_names)
        worker.progress.connect(self._on_anim_index_build_progress)
        worker.finished.connect(self._clear_item_index_build_worker_ref)
        worker.finished.connect(worker.deleteLater)
        self._item_index_build_worker = worker
        worker.start()

    def _clear_item_index_build_worker_ref(self, *_args) -> None:
        self._item_index_build_worker = None

    def _begin_actor_index_build(self, d4data_path: Path) -> None:
        """Start the background actor-name index build (idempotent).

        Mirrors ``_begin_item_index_build`` — same disk-cache /
        cold-build discipline, same refresh-on-ready signal flow. Only
        the Model Browser consumes this index today; the Character
        Builder labels its equipment pieces from the item index.
        """
        from d4extract.gui import actor_lookup
        key = Path(d4data_path).resolve()
        with actor_lookup._INDEX_LOCK:
            if key in actor_lookup._INDEX_CACHE:
                self._model_list.refresh_display_names()
                return
        prior = self._actor_index_build_worker
        if prior is not None:
            try:
                if prior.isRunning():
                    return
            except RuntimeError:
                pass
        worker = ActorIndexBuildWorker(d4data_path, parent=self)
        worker.index_ready.connect(self._model_list.refresh_display_names)
        worker.progress.connect(self._on_anim_index_build_progress)
        worker.finished.connect(self._clear_actor_index_build_worker_ref)
        worker.finished.connect(worker.deleteLater)
        self._actor_index_build_worker = worker
        worker.start()

    def _clear_actor_index_build_worker_ref(self, *_args) -> None:
        self._actor_index_build_worker = None

    def _model_display_name_for(self, sno_path: str) -> str:
        """Resolve a SNO path to its Model Browser row label.

        Priority: equipment in-game name → actor display name → SNO
        stem. Each lookup is wrapped in a broad ``try/except`` because
        labelling must never crash the list; an exception here would
        empty out every row. Returns the stem in mock mode (no d4data
        path) so the synthetic test harness still renders sensibly.
        """
        d4data = self._settings.d4data_path()
        if d4data is None or self._mock_mode:
            return _model_stem(sno_path)

        try:
            from d4extract.gui import item_lookup
            name = item_lookup.get_display_name(d4data, sno_path)
            if name:
                return name
        except Exception:  # noqa: BLE001 — labelling must never crash
            log.debug("item_lookup raised in resolver", exc_info=True)

        try:
            from d4extract.gui import actor_lookup
            name = actor_lookup.get_display_name(d4data, sno_path)
            if name:
                return name
        except Exception:  # noqa: BLE001 — labelling must never crash
            log.debug("actor_lookup raised in resolver", exc_info=True)

        return _model_stem(sno_path)

    def _on_animation_selected(self, name: str) -> None:
        # Always pause the viewport's last animation when the user
        # picks something new — including the rest-pose entry.
        self._cancel_anim_load_worker()
        self._current_animation = None
        self._current_animation_info = None
        self._viewport_container.viewport.clear_animation()

        if name == REST_POSE_LABEL:
            return

        if self.current_mesh_data is None or self.game_dir is None:
            return
        d4data = self._settings.d4data_path()
        if d4data is None:
            return

        bar = self._viewport_container.animation_bar
        info = bar.current_animation()
        if info is None or info.name != name:
            return
        self._current_animation_info = info

        rest_pose_map = self._rest_pose_map_for(self.current_mesh_data)
        worker = AnimLoadWorker(
            self.game_dir, info, bar.current_permutation(),
            d4data, rest_pose_map, parent=self,
        )
        worker.progress.connect(self._on_animation_load_progress)
        worker.finished.connect(self._on_animation_loaded)
        worker.error.connect(self._on_animation_load_error)
        worker.finished.connect(self._clear_anim_load_worker_ref)
        worker.error.connect(self._clear_anim_load_worker_ref)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._anim_load_worker = worker
        worker.start()

    def _on_animation_permutation_changed(self, perm_index: int) -> None:
        info = self._current_animation_info
        if info is None:
            return
        # Re-trigger the same animation with the new permutation.
        self._cancel_anim_load_worker()
        self._viewport_container.viewport.clear_animation()
        self._current_animation = None
        if (
            self.current_mesh_data is None
            or self.game_dir is None
            or self._settings.d4data_path() is None
        ):
            return
        rest_pose_map = self._rest_pose_map_for(self.current_mesh_data)
        worker = AnimLoadWorker(
            self.game_dir, info, perm_index,
            self._settings.d4data_path(), rest_pose_map, parent=self,
        )
        worker.progress.connect(self._on_animation_load_progress)
        worker.finished.connect(self._on_animation_loaded)
        worker.error.connect(self._on_animation_load_error)
        worker.finished.connect(self._clear_anim_load_worker_ref)
        worker.error.connect(self._clear_anim_load_worker_ref)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        self._anim_load_worker = worker
        worker.start()

    def _rest_pose_map_for(self, mesh_data) -> dict:
        return self._rest_pose_map_for_skeleton(
            getattr(mesh_data, "skeleton", None),
        )

    def _rest_pose_map_for_skeleton(self, skel) -> dict:
        """Build a ``bone_hash → (q, wp, scale)`` rest-pose map.

        Accepts a ``Skeleton`` directly — used by the per-piece builder
        export, whose canonical skeleton isn't owned by any one mesh.
        """
        if skel is None or not getattr(skel, "bones", None):
            return {}
        return {
            b.name_hash: (b.local_trs.q, b.local_trs.wp, b.local_trs.scale)
            for b in skel.bones
        }

    def _on_animation_load_progress(self, message: str) -> None:
        self._viewport_container.animation_bar.set_loading_message(message)
        self.statusBar().showMessage(message)

    def _on_animation_loaded(self, decoded, perm_index: int) -> None:
        info = self._current_animation_info
        if info is None or self.current_mesh_data is None:
            return
        self._current_animation = decoded

        ok = self._viewport_container.viewport.set_animation(
            decoded, self.current_mesh_data,
        )
        bar = self._viewport_container.animation_bar
        if not ok:
            bar.set_inactive("Could not bind animation to model")
            return
        bar.set_active(
            frame_count=decoded.frame_count,
            frame_rate=decoded.frame_rate,
            permutation_count=info.permutation_count,
        )
        self.statusBar().showMessage(
            f"Loaded {info.name} (perm {perm_index}): "
            f"{decoded.frame_count} frames @ {decoded.frame_rate:g}fps",
            5_000,
        )

    def _on_animation_load_error(self, message: str) -> None:
        log.warning("Animation load failed: %s", message)
        bar = self._viewport_container.animation_bar
        bar.set_inactive(f"Load failed: {message[:80]}")
        self.statusBar().showMessage(f"Animation load failed: {message}")

    def _on_animation_frame_changed(self, frame: int) -> None:
        if self._current_animation is None:
            return
        self._viewport_container.viewport.show_frame(int(frame))

    def _reset_animation_bar(self) -> None:
        self._cancel_anim_load_worker()
        self._cancel_anim_discovery_worker()
        self._current_animation = None
        self._current_animation_info = None
        self._viewport_container.viewport.clear_animation()
        bar = self._viewport_container.animation_bar
        bar.reset()
        bar.setVisible(False)

    def _cancel_anim_discovery_worker(self) -> None:
        prior = self._anim_discovery_worker
        if prior is None:
            return
        try:
            if prior.isRunning():
                prior.requestInterruption()
                try:
                    prior.finished.disconnect()
                    prior.error.disconnect()
                    prior.progress.disconnect()
                except (RuntimeError, TypeError):
                    pass
                prior.quit()
                prior.wait(2000)
        except RuntimeError:
            pass
        self._anim_discovery_worker = None

    def _cancel_anim_load_worker(self) -> None:
        prior = self._anim_load_worker
        if prior is None:
            return
        try:
            if prior.isRunning():
                prior.requestInterruption()
                try:
                    prior.finished.disconnect()
                    prior.error.disconnect()
                    prior.progress.disconnect()
                except (RuntimeError, TypeError):
                    pass
                prior.quit()
                prior.wait(2000)
        except RuntimeError:
            pass
        self._anim_load_worker = None

    def _clear_anim_discovery_worker_ref(self, *_args) -> None:
        self._anim_discovery_worker = None

    def _clear_anim_load_worker_ref(self, *_args) -> None:
        self._anim_load_worker = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt API
        # Tear down workers and the plotter cleanly before the app exits.
        # Each worker is wrapped because its C++ side may have already
        # been deleteLater'd between the finished signal firing and us
        # reaching this point — touching it then raises RuntimeError.
        for worker in (
            self._catalog_worker, self._load_worker, self._export_worker,
            self._texture_worker,
            self._anim_index_build_worker,
            self._item_index_build_worker,
            self._actor_index_build_worker,
            self._anim_discovery_worker, self._anim_load_worker,
        ):
            if worker is None:
                continue
            try:
                if worker.isRunning():
                    worker.requestInterruption()
                    worker.quit()
                    worker.wait(2000)
            except RuntimeError:
                continue
        self._catalog_worker = None
        self._load_worker = None
        self._export_worker = None
        self._texture_worker = None
        self._anim_index_build_worker = None
        self._item_index_build_worker = None
        self._actor_index_build_worker = None
        self._anim_discovery_worker = None
        self._anim_load_worker = None
        # The model list owns its own filter worker — cancel it here
        # too so the app doesn't sit on a long fuzzy scan during exit.
        try:
            self._model_list.shutdown()
        except Exception:
            log.debug("model_list.shutdown failed", exc_info=True)
        # Caches die with the window — the user explicitly chose
        # session-scoped behaviour, no on-disk persistence.
        self._mesh_cache.clear()
        self._texture_cache.clear()
        try:
            self._viewport_container.viewport.shutdown()
        except Exception:
            pass
        try:
            self._builder_page.shutdown()
        except Exception:
            pass
        super().closeEvent(event)
