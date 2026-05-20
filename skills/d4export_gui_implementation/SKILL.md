# D4.Export GUI — Four-Phase Implementation Architecture

## Overview

Build **D4.Export**, a PySide6 desktop application that lets users browse, preview, and export 3D models from their Diablo IV installation. The GUI is a thin presentation layer over the existing production Python pipeline — it calls `app_parser`, `material_parser`, `texture_parser`, and `GltfExporter` directly, with no reimplementation.

**UX reference**: wow.export (screenshots provided). We adapt its proven browse → preview → export flow to D4's CASC archive and .app model format.

**Tech stack**: PySide6 (Qt 6) for the app shell, PyVistaQt + VTK for the 3D viewport, existing Python parsers for all data work.

**Design language**: Dark theme (#1e1e2e background, #2a2a3e panels, #cdd6f4 text), minimal chrome, wow.export-inspired layout with a scrollable model list on the left and a 3D viewport on the right.

---

## Existing codebase (do not reimplement — import directly)

```
d4extract/src/d4extract/
├── casc/
│   ├── rustydemon.py          → RustyDemonCLI (CASC extraction via subprocess)
│   └── archive.py             → CASC archive utilities
├── formats/
│   ├── app_parser.py          → MeshData dataclass + parse_app(meta, payload)
│   ├── material_parser.py     → Material resolution from d4data JSON
│   └── texture_parser.py      → Texture decode (DDS → PIL → PNG)
├── export/
│   └── gltf_export.py         → GltfExporter class (configurable glTF 2.0 export)
└── cli.py                     → Click CLI (reference for wiring parsers together)
```

Key imports the GUI will use:
```python
from d4extract.casc.rustydemon import RustyDemonCLI
from d4extract.formats.app_parser import parse_app, MeshData
from d4extract.formats.material_parser import resolve_materials
from d4extract.export.gltf_export import GltfExporter
```

---

## Phase 1 — Game Directory Setup

### What the user sees

A centered welcome screen on a dark background. One prominent card:

```
┌─────────────────────────────────────────────────┐
│                                                 │
│     ⬡  Open Diablo IV Installation              │
│     Browse to your local D4 install folder      │
│     Last opened: C:\Program Files\Diablo IV     │  ← if previously saved
│                                                 │
└─────────────────────────────────────────────────┘
```

A subtle D4.Export logo/wordmark is watermarked in the background (low-opacity SVG or painted with QPainter). This screen only appears on first launch or if the saved path becomes invalid.

### Behavior

1. User clicks the card → `QFileDialog.getExistingDirectory()` opens
2. Validate the selected path:
   - Check for `Data/` subdirectory OR `.build.info` file at root
   - If invalid: show inline error on the card ("No Diablo IV data found at this location"), let user retry
3. On valid selection: save to `QSettings("d4extract", "D4Export")` under key `"game_dir"`
4. Transition to Phase 2 (model list)

### Returning users

On launch, check `QSettings` for a saved `game_dir`. If it exists and validates, skip Phase 1 entirely — go straight to Phase 2. Show the saved path as "Last opened:" on the card if the user returns to this screen via File → Change Game Directory.

### Auto-detection (nice-to-have)

Before showing the dialog, attempt auto-detect:
- **Steam**: `C:/Program Files (x86)/Steam/steamapps/common/Diablo IV`
- **Battle.net**: `C:/Program Files (x86)/Diablo IV`
- **Registry** (Windows): `HKLM\SOFTWARE\Blizzard Entertainment\Diablo IV`

If found, pre-fill the card with the detected path and a "Use this installation" button instead of making the user browse.

### Implementation notes

- Use a `QStackedWidget` as the main window's central widget. Phase 1 is page 0, Phase 2+ is page 1. `setCurrentIndex()` to transition.
- The card is a `QFrame` with rounded corners, styled via QSS (Qt Style Sheets).
- Store the validated `game_dir` as an instance variable on the main window for Phases 2-4.

### Phase 1 prompt

The following is a self-contained prompt for a new terminal session. Copy it verbatim.

---

> **Project context**: d4extract is a Diablo IV 3D model extraction pipeline. It extracts models from the game's CASC archive, parses binary `.app` files into mesh data, and exports to glTF 2.0. The CLI pipeline is production-ready. We're now building **D4.Export**, a PySide6 desktop GUI on top of it.
>
> **Your task**: Build Phase 1 of the D4.Export GUI — the application shell and game directory setup screen. This is the first code in the `gui/` module; nothing exists yet.
>
> **Before writing any code**, read these files to understand the project structure and existing code:
> - `d4extract/src/d4extract/cli.py` — see how the CLI wires up parsers and exporters. Your GUI will follow the same patterns.
> - `d4extract/src/d4extract/casc/rustydemon.py` — the `RustyDemonCLI` class you'll use for CASC access. Note its constructor takes a game directory path.
>
> Also read the `d4-python-gui` skill (it covers the full GUI architecture, widget layout, QSettings patterns, and threading model).
>
> **What to build**:
>
> Create the following files under `d4extract/src/d4extract/gui/`:
>
> ```
> gui/
> ├── __init__.py              # empty
> ├── main.py                  # QApplication entry point
> ├── main_window.py           # D4ExportWindow (QMainWindow)
> ├── widgets/
> │   ├── __init__.py
> │   └── setup_card.py        # SetupCard widget
> ├── theme.py                 # QSS stylesheet + color constants
> └── settings.py              # QSettings wrapper
> ```
>
> **main.py**: Entry point. Creates `QApplication`, sets `os.environ["QT_API"] = "pyside6"`, applies the dark theme stylesheet, creates and shows `D4ExportWindow`, runs the event loop. Window title: "D4.Export". Minimum size: 900×600.
>
> **main_window.py**: `D4ExportWindow(QMainWindow)` with:
> - A `QStackedWidget` as the central widget with two pages:
>   - Page 0: `SetupCard` (the welcome screen)
>   - Page 1: Placeholder `QWidget` (Phase 2 will replace this)
> - A menu bar with File → "Change Game Directory" (returns to page 0) and File → "Exit"
> - On init: check `AppSettings.game_dir()`. If valid path exists, skip to page 1. Otherwise show page 0.
> - A `set_game_dir(path)` method that saves to settings, stores as instance var, and transitions to page 1.
>
> **settings.py**: `AppSettings` class wrapping `QSettings("d4extract", "D4Export")` with typed accessors:
> - `game_dir() -> Path | None` — returns saved game directory (validated on read; returns None if path no longer exists)
> - `set_game_dir(path: Path)` — saves game directory
> - `last_export_dir() -> Path | None`
> - `set_last_export_dir(path: Path)`
>
> **setup_card.py**: `SetupCard(QWidget)` — the centered welcome screen:
> - Dark background (#1e1e2e) filling the entire widget
> - One centered card (`QFrame`, max-width 600px, rounded corners, background #2a2a3e):
>   - Title: "Open Diablo IV Installation" (large, #cdd6f4)
>   - Subtitle: "Browse to your local D4 install folder" (smaller, #a6adc8)
>   - If a previous path exists in settings: show "Last opened: <path>" in muted text (#6c7086)
>   - The entire card is clickable (override `mousePressEvent` or use a transparent `QPushButton` overlay)
> - On click: open `QFileDialog.getExistingDirectory()`
> - Validate the selected path:
>   - Valid if `(path / "Data").is_dir()` OR `(path / ".build.info").is_file()`
>   - If invalid: show error label on the card in red (#f38ba8): "No Diablo IV data found at this location"
>   - If valid: emit `directory_selected(Path)` signal → main window calls `set_game_dir()`
> - Auto-detection: before showing the file dialog, check these common paths:
>   - `C:/Program Files (x86)/Steam/steamapps/common/Diablo IV`
>   - `C:/Program Files/Steam/steamapps/common/Diablo IV`
>   - `C:/Program Files (x86)/Diablo IV`
>   - `C:/Program Files/Diablo IV`
>   - If any auto-detected path is valid, show it on the card with a "Use this installation" button
>
> **theme.py**: Export a `DARK_THEME_QSS` string constant and a `Colors` namespace class:
> ```python
> class Colors:
>     BG = "#1e1e2e"
>     SURFACE = "#2a2a3e"
>     TEXT = "#cdd6f4"
>     SUBTEXT = "#a6adc8"
>     OVERLAY = "#6c7086"
>     ACCENT = "#89b4fa"
>     ERROR = "#f38ba8"
>     BORDER = "#45475a"
>     LIST_BG = "#181825"
>     HOVER = "#262637"
>     SELECTED = "#313244"
> ```
> The QSS should style QMainWindow, QWidget, QPushButton, QLineEdit, QLabel, QFrame, QMenuBar, QMenu, and QStatusBar in the dark theme. Use monospace font (Consolas / Cascadia Code fallback) for the model list; system font for everything else.
>
> **Exit criteria**: Run `python -m d4extract.gui.main` and verify:
> 1. App launches with dark theme, centered setup card visible
> 2. Clicking the card opens a directory picker
> 3. Selecting a directory WITHOUT `Data/` or `.build.info` shows the red error message
> 4. Selecting a valid directory (or any dir with a `Data/` subfolder for testing) transitions to the placeholder page 1
> 5. Restarting the app skips the setup card and goes directly to page 1
> 6. File → Change Game Directory returns to the setup card
>
> **Do not** install PyVistaQt or pyvista yet — they're Phase 3. Phase 1 only needs `PySide6`.

---

## Phase 2 — Model List Browser

### What the user sees

A full-window scrollable list of all D4 model paths from the CASC archive. Layout matches wow.export's model browser:

```
┌──────────────────────────────────────────────────────────────────┐
│  D4.Export                                          [─] [□] [×]  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  base/meta/Appearance/monster_beast_werewolf.app                │
│  base/meta/Appearance/monster_beast_spider.app                  │
│  base/meta/Appearance/monster_demon_succubus.app                │
│  base/meta/Appearance/monster_undead_skeleton_king.app          │
│  base/meta/Appearance/player_barbarian_armor_001.app            │
│  base/meta/Appearance/player_necromancer_helm_003.app           │
│  ...                                                            │
│  (scrollable list, ~13K entries)                                │
│                                                                  │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│  13,247 models found.  Quick filter: ALL │ MON │ PLR │ NPC │ ITM│
│  ┌──────────────────────────────────────────────────────────────┐│
│  │ Filter models...                                            ││
│  └──────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────┘
```

### Key elements

1. **Model list** (`QListView` + `QStringListModel` + `QSortFilterProxyModel`): Full-width, dark-themed rows. Selected item highlighted in accent blue (#89b4fa). Each row shows the SNO path.

2. **Status bar** (bottom): Shows total model count + count after filtering. Quick filter toggle buttons for SNO categories:
   - ALL — no filter
   - MON — monster_ prefix
   - PLR — player_ prefix  
   - NPC — npc_ prefix
   - ITM — item_ prefix
   - ENV — environment_ / world_ prefix
   - WPN — weapon_ prefix

3. **Filter bar** (very bottom, like wow.export): `QLineEdit` with placeholder "Filter models..." — live-filters the list as you type via `proxy.setFilterFixedString()`.

### Loading the catalog

On transition from Phase 1:

1. Show a loading spinner / progress label ("Scanning CASC archive...")
2. On a `QThread` worker: call `RustyDemonCLI(game_dir).list_files("base/meta/Appearance/*")`
3. Cache the result to disk (`~/.cache/d4extract/catalog.json` or `%LOCALAPPDATA%/d4extract/cache/catalog.json`)
   - Cache key: `game_dir` + `.build.info` mtime
   - On subsequent launches, load from cache if valid (instant startup)
4. Populate the `QStringListModel` with SNO paths
5. Hide spinner, show the list

### Performance

Qt's model/view architecture handles 13K string entries efficiently without virtual scrolling. `QSortFilterProxyModel` provides instant case-insensitive filtering. No custom optimization needed.

### Quick filter implementation

The quick filter buttons are a `QButtonGroup` of `QPushButton` widgets styled as toggle pills. Each sets a regex prefix filter on the proxy model:

```python
# MON button clicked
proxy.setFilterRegularExpression(QRegularExpression("^.*monster_", QRegularExpression.CaseInsensitiveOption))
```

The text filter and quick filter compose: quick filter narrows by category, text filter narrows within that category.

### Single click → Phase 3

When the user clicks a model in the list, emit the `model_selected` signal with the SNO path. This triggers the transition to the two-panel layout (Phase 3).

### Phase 2 prompt

The following is a self-contained prompt for a new terminal session. Copy it verbatim.

---

> **Project context**: d4extract is a Diablo IV 3D model extraction pipeline. The CLI pipeline extracts models from D4's CASC archive via `rustydemon` (a Rust CLI tool), parses `.app` binary files, and exports to glTF. We're building **D4.Export**, a PySide6 desktop GUI on top of it.
>
> **Phase 1 is complete.** The GUI shell exists at `d4extract/src/d4extract/gui/` with: `main.py` (entry point), `main_window.py` (QMainWindow with QStackedWidget — page 0 is setup card, page 1 is placeholder), `widgets/setup_card.py` (game directory picker), `theme.py` (dark theme QSS + Colors class), `settings.py` (QSettings wrapper with `game_dir` accessor).
>
> **Your task**: Build Phase 2 — the model list browser. When the user has a valid game directory (either from Phase 1 setup or persisted from a previous session), replace the placeholder page 1 with a scrollable, filterable list of all ~13K model entries from the CASC archive.
>
> **Before writing any code**, read these files:
> - All existing GUI code: `d4extract/src/d4extract/gui/` — read every `.py` file to understand the architecture, signal patterns, theme colors, and settings API.
> - `d4extract/src/d4extract/casc/rustydemon.py` — the `RustyDemonCLI` class. Read it to understand the constructor signature (takes game directory), and the `list_files()` method signature and return type. This is the class you'll call from a worker thread to scan the CASC archive.
> - `d4extract/src/d4extract/cli.py` — see how the CLI uses `RustyDemonCLI` for reference.
>
> Also read the `d4-casc-pipeline` skill (it covers RustyDemonCLI usage, CASC catalog scanning, file caching strategy, and SNO type filtering).
>
> **What to build**:
>
> Create/modify these files:
>
> ```
> gui/
> ├── widgets/
> │   └── model_list.py         # NEW — ModelListWidget
> ├── workers/
> │   ├── __init__.py            # NEW
> │   └── catalog_worker.py      # NEW — CatalogWorker (QThread)
> └── main_window.py             # MODIFY — wire up page 1
> ```
>
> **model_list.py**: `ModelListWidget(QWidget)` — the full model browser panel:
>
> Layout (top to bottom):
> 1. `QListView` filling most of the space — displays SNO paths from the CASC catalog
> 2. Status line: `QLabel` showing "{N} models found." (updates as filters change to show "{M} of {N} models.")
> 3. Quick filter row: `QHBoxLayout` with toggle `QPushButton` pills:
>    - ALL (default, active) — clears category filter
>    - MON — filters to paths containing `monster_`
>    - PLR — filters to paths containing `player_`
>    - NPC — filters to paths containing `npc_`
>    - ITM — filters to paths containing `item_`
>    - ENV — filters to paths containing `environment_` or `world_`
>    - WPN — filters to paths containing `weapon_`
>    - Style: small rounded buttons, inactive = #313244 bg, active = #89b4fa bg with #1e1e2e text
>    - Mutually exclusive (use QButtonGroup with exclusive=True)
> 4. Filter text input: `QLineEdit` with placeholder "Filter models..." at the very bottom
>
> Data model stack:
> - `QStringListModel` holds the full SNO path list
> - `QSortFilterProxyModel` sits between the model and the view
> - `setFilterCaseSensitivity(Qt.CaseInsensitive)` on the proxy
> - The text filter input connects `textChanged` → `proxy.setFilterFixedString()`
> - The quick filter buttons set a regex on the proxy that composes with the text filter. To compose both filters, subclass `QSortFilterProxyModel` and override `filterAcceptsRow()` to check both the category regex AND the text substring.
>
> Signals:
> - `model_selected = Signal(str)` — emitted when a list item is clicked (single click), carrying the SNO path string. This will be connected in Phase 3 to trigger model loading.
>
> Loading state:
> - Before the catalog is loaded, show a centered `QLabel` with "Scanning CASC archive..." over the list area
> - After `CatalogWorker` finishes, hide the label and show the populated list
>
> **catalog_worker.py**: `CatalogWorker(QThread)`:
> - Constructor takes `game_dir: Path`
> - `run()` method:
>   1. Check for a cached catalog file: `Path(QStandardPaths.writableLocation(QStandardPaths.CacheLocation)) / "d4extract" / "catalog.json"`
>   2. Cache key: `str(game_dir)` + mtime of `game_dir / ".build.info"` (if it exists)
>   3. If cache hit: load the list from JSON, emit `finished`
>   4. If cache miss: call `RustyDemonCLI(str(game_dir)).list_files(...)` to get all Appearance entries. The exact method name and args may vary — read the source to confirm. Save the result to the cache file. Emit `finished`.
> - Signals: `finished = Signal(list)` (list of SNO path strings), `error = Signal(str)`
>
> **main_window.py modifications**:
> - Replace the placeholder page 1 with `ModelListWidget`
> - On valid game directory (either from Phase 1 setup card or from persisted settings on launch):
>   1. Switch to page 1
>   2. Spawn `CatalogWorker` with the game directory
>   3. Connect `CatalogWorker.finished` → `ModelListWidget.load_entries(entries: list[str])`
>   4. Connect `CatalogWorker.error` → show error in status bar
> - Connect `ModelListWidget.model_selected` → store as `self.current_sno_path` (Phase 3 will use this)
> - Add keyboard shortcut: Ctrl+F → focus the filter text input (`model_list.focus_filter()`)
>
> **Testing without a real D4 install**: If you can't test with a real CASC archive, add a `--mock-catalog` flag or env var to `main.py` that populates the list with fake SNO paths (e.g., generate 13,000 paths like `base/meta/Appearance/monster_beast_{i:04d}.app`). This lets you test the UI, scrolling performance, and filtering without needing rustydemon or a D4 installation.
>
> **Exit criteria**:
> 1. App launches → if game_dir is set, shows the model list (or mock data if no real D4 install)
> 2. The list is scrollable and handles ~13K entries smoothly
> 3. Typing in the filter bar instantly narrows the list
> 4. Quick filter buttons work and compose with the text filter (e.g., clicking MON then typing "beast" shows only monster entries containing "beast")
> 5. Status line updates to show filtered count: "3,247 of 13,247 models."
> 6. Clicking a model in the list prints the SNO path to console (or stores it) — visual confirmation the signal works
> 7. On subsequent launches, the catalog loads from cache (near-instant)
> 8. Ctrl+F focuses the filter bar

---

## Phase 3 — 3D Viewport Preview

### What the user sees

The window splits into two panels: model list on the left (narrowed), 3D viewport on the right.

```
┌─────────────────┬────────────────────────────────────────────────┐
│ (model list)    │                                                │
│                 │          3D Viewport (PyVistaQt)               │
│ ▸ monster_...   │                                                │
│ ▸ monster_...   │        ┌─────────────────────┐                │
│ ▸ monster_...   │        │                     │                │
│ █ monster_w...  │        │    [3D Model]       │                │
│ ▸ player_...    │        │                     │                │
│ ▸ player_...    │        │                     │                │
│                 │        └─────────────────────┘                │
│                 │                                                │
│                 │  ┌──────────────────────────────────────────┐  │
│                 │  │ Verts: 12,480  Tris: 8,320  Bones: 142  │  │
│                 │  │ Submeshes: 4   Format: Skinned           │  │
│                 │  └──────────────────────────────────────────┘  │
│                 │                                                │
│                 │  ┌──┐┌──┐┌──┐┌──┐┌──┐                        │
│                 │  │T1││T2││T3││T4││T5│  (texture thumbnails)   │
│                 │  └──┘└──┘└──┘└──┘└──┘                        │
├─────────────────┴────────────────────────────────────────────────┤
│  13,247 models. (1 selected)         Filter: ________   [GLB ▾] │
└──────────────────────────────────────────────────────────────────┘
```

### Layout transition

Use a `QSplitter` (horizontal) with two children:
- Left: the existing model list widget (compressed to ~30% width)
- Right: a new `ViewportContainer` widget

The transition is animated via `QPropertyAnimation` on the splitter sizes, or simply set the sizes directly for v1.

### 3D Viewport (PyVistaQt)

The viewport is a `BackgroundPlotter` from `pyvistaqt`, embedded as a Qt widget:

```python
from pyvistaqt import BackgroundPlotter

plotter = BackgroundPlotter(show=False, toolbar=False, menu_bar=False)
```

**Background**: Dark gradient matching the app theme (#1a1a2e → #16161e).

**Camera**: Orbit camera (PyVistaQt default interactor). Auto-frame the model on load via `plotter.reset_camera()`.

**Lighting**: Key light from upper-right, subtle fill light from lower-left, ambient at 0.15.

### Loading a model

When the user clicks a model in the list:

1. Show a loading overlay on the viewport ("Extracting..." → "Parsing..." → "Rendering...")
2. On a **QThread worker**:
   a. Extract .app meta + payload from CASC via `RustyDemonCLI`
   b. Parse via `parse_app(meta_path, payload_path)` → `MeshData`
   c. Apply coordinate conversion: `mesh_data` positions use D4's left-handed Z-up. Convert to Y-up for VTK: `(x, y, z) → (x, z, -y)`
3. **On the main thread** (via signal): build PyVista mesh and add to plotter

```python
# Worker emits MeshData → main thread renders
def on_model_loaded(self, mesh_data: MeshData):
    self.plotter.clear()
    
    positions = np.array(mesh_data.positions, dtype=np.float32)
    indices = np.array(mesh_data.indices, dtype=np.int64)
    
    # VTK face format: [3, v0, v1, v2, 3, v3, v4, v5, ...]
    n_tris = len(indices) // 3
    faces = np.empty(n_tris * 4, dtype=np.int64)
    faces[0::4] = 3
    faces[1::4] = indices[0::3]
    faces[2::4] = indices[1::3]
    faces[3::4] = indices[2::3]
    
    mesh = pv.PolyData(positions, faces)
    
    if mesh_data.normals:
        mesh.point_data["Normals"] = np.array(mesh_data.normals)
    
    # Per-submesh coloring for visual distinction
    scalars = np.zeros(len(positions), dtype=np.int32)
    for sm in mesh_data.submeshes:
        scalars[sm.vertex_offset:sm.vertex_offset + sm.vertex_count] = sm.material_index
    mesh.point_data["material"] = scalars
    
    self.plotter.add_mesh(mesh, scalars="material", cmap="Set2", show_edges=False, smooth_shading=True)
    self.plotter.reset_camera()
```

**Critical**: All `plotter` calls must happen on the main thread. The worker does extraction + parsing only.

### Properties overlay

A semi-transparent info bar at the bottom of the viewport (not a separate panel) showing:
- Vertex count, triangle count, bone count
- Submesh count, format (Skinned / Static)
- Model name (from SNO path)

This is a `QLabel` with a dark translucent background, positioned via layout at the bottom of the viewport container. It updates whenever a new model loads.

### Texture thumbnail strip

Below the properties overlay, a horizontal strip of texture thumbnails (like wow.export's bottom strip). Each thumbnail shows a decoded texture from the model's material chain:

- Only shown if `d4data_path` is configured (File → Set D4Data Path)
- Thumbnails are 64×64 previews decoded via `texture_parser`
- Clicking a thumbnail does nothing for now (future: opens full-size viewer)
- If no textures are available, the strip is hidden

This is a `QScrollArea` with horizontal layout containing `QLabel` widgets with `QPixmap` icons.

### Wireframe toggle

A small toolbar or keyboard shortcut (W key) toggles wireframe rendering. Implementation via VTK actor properties — see the d4-python-gui skill.

### Phase 3 prompt

The following is a self-contained prompt for a new terminal session. Copy it verbatim.

---

> **Project context**: d4extract is a Diablo IV 3D model extraction pipeline. It extracts models from D4's CASC archive (via `rustydemon`, a Rust CLI), parses binary `.app` files into a `MeshData` dataclass (positions, normals, UVs, tangents, indices, submeshes, skeleton/bones, joints, weights), and exports to glTF 2.0. The CLI pipeline is production-ready. We're building **D4.Export**, a PySide6 + PyVistaQt desktop GUI on top of it.
>
> **Phases 1-2 are complete.** The GUI exists at `d4extract/src/d4extract/gui/`:
> - `main.py` — entry point, QApplication, dark theme
> - `main_window.py` — QMainWindow with QStackedWidget (page 0 = setup card, page 1 = model list browser). Stores `self.game_dir` and `self.current_sno_path`. Has a menu bar.
> - `widgets/setup_card.py` — Phase 1 game directory picker
> - `widgets/model_list.py` — Phase 2 scrollable model list with ~13K entries, QSortFilterProxyModel, filter bar, quick filter category buttons. Emits `model_selected(str)` signal on click.
> - `workers/catalog_worker.py` — QThread worker that scans CASC and populates the model list
> - `theme.py` — Colors class (BG, SURFACE, TEXT, ACCENT, etc.) + DARK_THEME_QSS string
> - `settings.py` — AppSettings wrapping QSettings
>
> **Your task**: Build Phase 3 — the 3D viewport. When a user clicks a model in the list, extract it from CASC, parse the binary data, and render it in a PyVistaQt 3D viewport with orbit camera, per-submesh coloring, and a properties overlay.
>
> **Before writing any code**, read these files in order:
> 1. All existing GUI code in `d4extract/src/d4extract/gui/` — every `.py` file, to understand the widget tree, signals, theme, and settings.
> 2. `d4extract/src/d4extract/formats/app_parser.py` — find the `MeshData` dataclass and `parse_app()` function. Read the MeshData definition carefully: you need to know what fields are available (positions, normals, uvs, tangents, indices, colors, joints, weights, submeshes, skeleton) and their types (lists of tuples, flat list of ints, etc.). Also note the `Submesh` dataclass (vertex_offset, vertex_count, index_offset, index_count, material_index).
> 3. `d4extract/src/d4extract/casc/rustydemon.py` — the `RustyDemonCLI` class. You'll need its `extract()` or `extract_app()` method to get the .app meta + payload file paths from a SNO path.
> 4. `d4extract/src/d4extract/cli.py` — see the `export` command for reference on how to wire up extraction → parsing → coordinate conversion. Look for how it calls `parse_app()` and what arguments it passes.
>
> Also read these skills:
> - `d4-python-gui` — covers PyVistaQt BackgroundPlotter setup, VTK mesh construction from MeshData, QThread worker patterns, coordinate system notes
> - `d4-coordinate-systems` — covers the axis conversion from D4 (left-handed Z-up) to VTK/glTF (right-handed Y-up). **The critical formula**: positions `(x, y, z) → (x, z, -y)`, normals same transform, quaternions `(qx, qy, qz, qw) → (qx, qz, -qy, qw)`.
>
> **What to build**:
>
> Create/modify these files:
>
> ```
> gui/
> ├── widgets/
> │   ├── viewport.py            # NEW — ViewportWidget (PyVistaQt wrapper)
> │   └── properties_bar.py      # NEW — PropertiesBar (info overlay)
> ├── workers/
> │   └── load_worker.py         # NEW — LoadModelWorker (QThread)
> └── main_window.py             # MODIFY — add viewport panel, wire signals
> ```
>
> **main_window.py modifications**:
>
> The current page 1 is a flat `ModelListWidget` filling the whole window. Change it to a `QSplitter` (horizontal) with two children:
> - Left (30% width): the existing `ModelListWidget`
> - Right (70% width): a new `ViewportContainer` (QWidget that wraps `ViewportWidget` + `PropertiesBar`)
>
> The viewport starts empty (dark background, maybe a subtle "Select a model" hint). When `model_selected` fires:
> 1. Cancel any in-progress `LoadModelWorker`
> 2. Spawn a new `LoadModelWorker(game_dir, sno_path)`
> 3. Show a loading state on the viewport
> 4. On `worker.finished` → call `viewport.load_mesh(mesh_data)` and `properties_bar.update(mesh_data)` — **both on main thread**
> 5. On `worker.error` → show error overlay on viewport
>
> Wire keyboard shortcuts: W = toggle wireframe, F = reset camera to frame model.
>
> **viewport.py**: `ViewportWidget(QWidget)`:
>
> - Embeds a `pyvistaqt.BackgroundPlotter` (show=False, toolbar=False, menu_bar=False)
> - Access the plotter's interactor widget and add it to a QVBoxLayout with zero margins
> - Set background color: `plotter.set_background("#1a1a2e")`
> - Add lighting: key light from (5, 5, 10) at intensity 0.8, fill light from (-3, -2, 5) at intensity 0.3
> - `load_mesh(mesh_data: MeshData)` method:
>   1. `plotter.clear()`
>   2. Convert positions to numpy array (float32). **Apply coordinate conversion**: for each position (x, y, z), output (x, z, -y). Do this with numpy: `positions[:, [0, 2, 1]]` then negate column 2 (the new Y). Same for normals if present.
>   3. Convert indices to numpy int64 array
>   4. Build VTK face array: `[3, v0, v1, v2, 3, v3, v4, v5, ...]` format. Allocate array of length `n_tris * 4`, set `faces[0::4] = 3`, `faces[1::4] = indices[0::3]`, etc.
>   5. Create `pv.PolyData(positions, faces)`
>   6. If normals: `mesh.point_data["Normals"] = normals_array`
>   7. For per-submesh coloring: create an int32 scalars array of length n_vertices, iterate submeshes and assign `scalars[vo:vo+vc] = sm.material_index`
>   8. `plotter.add_mesh(mesh, scalars="material", cmap="Set2", show_edges=False, smooth_shading=True)`
>   9. `plotter.reset_camera()`
> - `toggle_wireframe()` method: iterate `plotter.renderer.actors`, toggle between wireframe and surface representation
> - `reset_view()` method: `plotter.reset_camera()`
>
> **CRITICAL THREADING RULE**: Every `self.plotter.*` call MUST happen on the main thread. The `LoadModelWorker` does extraction and parsing on a background thread, then emits the parsed `MeshData` back to the main thread via signal. The main thread's slot calls `viewport.load_mesh()`. Never call plotter methods from inside the worker.
>
> **load_worker.py**: `LoadModelWorker(QThread)`:
> - Constructor: `(game_dir: Path, sno_path: str)`
> - Signals: `finished = Signal(object)` (emits MeshData), `progress = Signal(str)`, `error = Signal(str)`
> - `run()` method:
>   1. `self.progress.emit("Extracting from CASC...")`
>   2. Use `RustyDemonCLI` to extract the .app file pair (meta + payload) for the given SNO path. Read the rustydemon.py source to understand the exact method call and what it returns (likely two Path objects for meta and payload files).
>   3. `self.progress.emit("Parsing model...")`
>   4. Call `parse_app(meta_path, payload_path)` → `MeshData`
>   5. `self.finished.emit(mesh_data)`
>   6. Wrap everything in try/except, emit `self.error.emit(str(e))` on failure
>
> **properties_bar.py**: `PropertiesBar(QFrame)`:
> - Positioned at the bottom of the viewport container via layout
> - Semi-transparent dark background (`rgba(30, 30, 46, 200)`)
> - Single-line or two-line layout showing: model name, vertex count, triangle count, submesh count, bone count (or "Static" if no skeleton), format (Skinned / Static)
> - `update(mesh_data: MeshData)` method populates all fields
> - Use formatted numbers: `f"{count:,}"` for thousands separators
>
> **Dependencies**: This phase requires `pyvistaqt`, `pyvista`, `vtk`, and `numpy`. Install with:
> ```
> pip install pyvistaqt pyvista numpy
> ```
> Ensure PySide6 backend: set `os.environ["QT_API"] = "pyside6"` in `main.py` BEFORE importing pyvistaqt.
>
> **Testing without a real D4 install**: If you previously added a mock catalog mode, extend it: when `model_selected` fires with a mock path, generate synthetic MeshData (a simple cube or sphere with known positions/indices/normals) so you can test the viewport rendering pipeline end-to-end without CASC extraction.
>
> **Exit criteria**:
> 1. App launches with the two-panel split layout (model list left, viewport right)
> 2. Clicking a model in the list triggers extraction + parsing on a background thread
> 3. Progress status shows "Extracting..." then "Parsing..."
> 4. The parsed model appears in the 3D viewport with per-submesh coloring
> 5. The model is oriented correctly (standing upright, not sideways — the coordinate conversion is working)
> 6. Orbit camera works (click-drag to rotate, scroll to zoom)
> 7. Properties bar at the bottom shows accurate vertex/tri/bone counts
> 8. Pressing W toggles wireframe
> 9. Pressing F resets the camera to frame the model
> 10. Clicking a different model in the list replaces the current one
> 11. If extraction or parsing fails, the viewport shows an error message instead of crashing

---

## Phase 4 — GLB Export

### What the user sees

An "Export GLB" button with a dropdown arrow at the bottom-right of the status bar (like wow.export). Clicking the main button exports with current settings. Clicking the dropdown arrow reveals export options.

```
┌─────────────────────────────────────────────────────────┐
│                                        [Export GLB ▾]   │
│                                        ┌──────────────┐ │
│                                        │ Export GLB   │ │
│                                        │ Export GLTF  │ │
│                                        │ ──────────── │ │
│                                        │ ☑ Textures   │ │
│                                        │ ☑ Skeleton   │ │
│                                        │ ☑ Normals    │ │
│                                        │ ☑ Tangents   │ │
│                                        │ ☐ Prune bones│ │
│                                        │ ☐ Cloth mesh │ │
│                                        │ ☐ Mat. JSON  │ │
│                                        │ ──────────── │ │
│                                        │ Coord: Y-up ▾│ │
│                                        └──────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### Export flow

1. User clicks "Export GLB" (or hits Ctrl+E)
2. `QFileDialog.getSaveFileName()` opens, defaulting to `<model_name>.glb` in the last-used export directory
3. A progress overlay appears on the viewport:
   - "Resolving materials..." (if textures enabled and d4data configured)
   - "Embedding textures..." (if textures enabled)
   - "Writing GLB..."
4. On a **QThread worker**: call `GltfExporter.export(mesh_data, output_path)` with current options
5. On completion: status bar shows "Exported model_name.glb (12,480 verts, 8,320 tris, 3.2 MB)"

### Export options

The dropdown menu is a `QMenu` with checkable `QAction` items. Settings persist across sessions via `QSettings`.

| Option | QSettings key | Default | GltfExporter param |
|--------|--------------|---------|-------------------|
| Embed textures | `export/textures` | True | `embed_textures` |
| Include skeleton | `export/skeleton` | True | `export_skin` |
| Include normals | `export/normals` | True | `export_normals` |
| Include tangents | `export/tangents` | True | `export_tangents` |
| Prune unused bones | `export/prune_bones` | False | `prune_bones` |
| Include cloth meshes | `export/cloth` | False | `include_cloth` |
| Materials sidecar | `export/sidecar` | True | `write_materials_sidecar` |
| Coordinate transform | `export/coords` | `z_up_to_y_up` | `coordinate_transform` |

### Export button implementation

Use `QPushButton` with a `QMenu`:

```python
export_btn = QPushButton("Export GLB")
export_menu = QMenu()
export_menu.addAction("Export GLB", self.export_glb)
export_menu.addAction("Export glTF", self.export_gltf)
export_menu.addSeparator()
# ... checkable options ...
export_btn.setMenu(export_menu)
```

The main button click (`clicked` signal) triggers export with current format. The dropdown arrow opens the menu.

### D4Data path requirement

Texture embedding requires `d4data_path` pointing to extracted D4 JSON data. If the user clicks "Export GLB" with textures enabled but no d4data configured:
- Show a dialog: "Texture embedding requires D4Data. Would you like to set the path now, or export without textures?"
- Options: "Set Path" (opens directory picker) | "Export Without Textures" | "Cancel"
- Save the d4data path to `QSettings` for future sessions

### Phase 4 prompt

The following is a self-contained prompt for a new terminal session. Copy it verbatim.

---

> **Project context**: d4extract is a Diablo IV 3D model extraction pipeline. It extracts models from D4's CASC archive, parses binary `.app` files into `MeshData` (positions, normals, UVs, tangents, indices, submeshes, skeleton, joints, weights, materials), and exports to glTF 2.0 via the `GltfExporter` class. We've built **D4.Export**, a PySide6 + PyVistaQt desktop GUI on top of it.
>
> **Phases 1-3 are complete.** The GUI at `d4extract/src/d4extract/gui/` can:
> - Phase 1: Pick and validate a D4 game directory, persist via QSettings
> - Phase 2: Scan CASC and show ~13K models in a filterable list (QSortFilterProxyModel + quick filter category buttons)
> - Phase 3: Click a model → extract from CASC on QThread → parse to MeshData → render in PyVistaQt with per-submesh coloring, orbit camera, wireframe toggle, properties overlay
>
> The main window stores `self.game_dir` (Path), `self.current_sno_path` (str), and `self.current_mesh_data` (MeshData) when a model is loaded.
>
> **Your task**: Build Phase 4 — the GLB export system. Add an "Export GLB" button with a dropdown menu of export options, wire it to the existing `GltfExporter` class, and run the export on a background thread.
>
> **Before writing any code**, read these files in order:
> 1. All existing GUI code in `d4extract/src/d4extract/gui/` — every `.py` file. Pay special attention to the main_window's signal wiring, the current status bar setup, and where `self.current_mesh_data` is stored.
> 2. `d4extract/src/d4extract/export/gltf_export.py` — this is the most important file. Read the `GltfExporter` class:
>    - Constructor parameters: `coordinate_transform`, `export_normals`, `export_tangents`, `export_uvs`, `export_colors`, `export_skin`, `prune_bones`, `embed_textures`, `include_cloth`, `write_materials_sidecar`, `texture_dir`, `d4data_path`
>    - The `export(mesh_data, output_path)` method signature and what it does
>    - Note any class attributes that report results after export (vertex count, triangle count, etc.)
> 3. `d4extract/src/d4extract/cli.py` — see the `export` CLI command for reference on how it constructs a `GltfExporter` with options and calls `export()`. This is your reference for the correct wiring.
> 4. `d4extract/src/d4extract/formats/material_parser.py` — understand how materials are resolved, since the exporter may need `d4data_path` for material/texture embedding.
>
> Also read the `d4-gltf-export` skill (covers GltfExporter options, PBR materials, texture embedding, alpha modes, coordinate transforms) and the `d4-materials-textures` skill (covers texture decode, d4data path requirements).
>
> **What to build**:
>
> Create/modify these files:
>
> ```
> gui/
> ├── widgets/
> │   └── export_button.py       # NEW — ExportButton widget + options menu
> ├── workers/
> │   └── export_worker.py       # NEW — ExportWorker (QThread)
> ├── main_window.py             # MODIFY — add export button, wire signals, menu items
> └── settings.py                # MODIFY — add export option accessors + d4data_path
> ```
>
> **export_button.py**: `ExportButton(QWidget)`:
>
> A composite widget containing a styled `QPushButton` ("Export GLB") with an attached `QMenu` dropdown. Position it at the bottom-right of the window, in the status bar area or in a toolbar at the bottom.
>
> The button itself:
> - Style: accent color (#89b4fa) background, dark (#1e1e2e) text, bold, rounded corners, 8px 16px padding
> - Disabled state (no model loaded): grayed out, not clickable
> - Enabled when `self.current_mesh_data is not None`
> - Main click action: trigger export with current settings
>
> The dropdown menu (`QMenu`):
> - "Export as GLB" action → export in .glb (binary) format
> - "Export as glTF" action → export in .gltf + .bin format
> - Separator
> - Checkable options (each persisted via QSettings):
>   - ☑ Embed Textures (default: True) — maps to `embed_textures`
>   - ☑ Include Skeleton (default: True) — maps to `export_skin`
>   - ☑ Include Normals (default: True) — maps to `export_normals`
>   - ☑ Include Tangents (default: True) — maps to `export_tangents`
>   - ☐ Prune Unused Bones (default: False) — maps to `prune_bones`
>   - ☐ Include Cloth Meshes (default: False) — maps to `include_cloth`
>   - ☑ Write Materials Sidecar (default: True) — maps to `write_materials_sidecar`
> - Separator
> - "Coordinate System" submenu with radio-style options:
>   - Y-Up (glTF standard) → `coordinate_transform="z_up_to_y_up"` [default]
>   - Z-Up (raw D4) → `coordinate_transform="none"`
> - Separator
> - "Set D4Data Path..." action → opens `QFileDialog.getExistingDirectory()`, saves to QSettings
>
> Signals:
> - `export_requested = Signal(dict)` — emits a dict of all export options when the user triggers an export. The dict maps directly to `GltfExporter` constructor kwargs.
>
> Methods:
> - `set_enabled(enabled: bool)` — enable/disable based on whether a model is loaded
> - `get_export_options() -> dict` — read all checkable actions and return as kwargs dict
>
> **export_worker.py**: `ExportWorker(QThread)`:
> - Constructor: `(mesh_data: MeshData, output_path: Path, options: dict)`
> - Signals: `finished = Signal(str)` (success message), `progress = Signal(str)`, `error = Signal(str)`
> - `run()` method:
>   1. `self.progress.emit("Preparing export...")`
>   2. Construct `GltfExporter(**self.options)` — the options dict contains all the constructor kwargs
>   3. `self.progress.emit("Writing GLB...")`
>   4. Call `exporter.export(self.mesh_data, self.output_path)`
>   5. Get the output file size: `self.output_path.stat().st_size`
>   6. Format a success message: `"Exported {name} ({verts:,} verts, {tris:,} tris, {size_mb:.1f} MB)"`
>   7. `self.finished.emit(success_msg)`
>   8. Wrap in try/except → `self.error.emit(str(e))`
>
> **main_window.py modifications**:
>
> - Add `ExportButton` to the bottom bar (status bar area, right-aligned)
> - Connect `export_button.export_requested` signal:
>   1. Open `QFileDialog.getSaveFileName()` with filter "GLB files (*.glb)" or "glTF files (*.gltf)" depending on the selected format. Default filename: derive from the current SNO path (e.g., `monster_beast_werewolf.glb`). Default directory: last-used export dir from QSettings.
>   2. If the user selects a path: save the directory to `AppSettings.set_last_export_dir()`, then spawn `ExportWorker(self.current_mesh_data, output_path, options)`
>   3. Connect `ExportWorker.progress` → show in status bar
>   4. Connect `ExportWorker.finished` → show success in status bar, re-enable export button
>   5. Connect `ExportWorker.error` → show `QMessageBox.critical()` with error details
> - Disable the export button while an export is in progress
> - Add keyboard shortcut: Ctrl+E → trigger export (same as clicking the export button)
> - Add menu bar: File → "Set D4Data Path..." (same action as the dropdown menu option)
>
> **D4Data path handling**:
>
> When the user triggers an export with "Embed Textures" checked but no `d4data_path` is configured in QSettings:
> - Show a `QMessageBox.question()`: "Texture embedding requires a D4Data path (extracted game JSON data). What would you like to do?"
> - Three buttons: "Set Path..." | "Export Without Textures" | "Cancel"
> - "Set Path..." → open directory picker → save to QSettings → proceed with export
> - "Export Without Textures" → set `embed_textures=False` in the options dict → proceed
> - "Cancel" → abort
>
> **settings.py additions**:
> - `d4data_path() -> Path | None`
> - `set_d4data_path(path: Path)`
> - `last_export_dir() -> Path | None`
> - `export_option(key: str, default: bool) -> bool` — generic getter for export checkboxes
> - `set_export_option(key: str, value: bool)` — generic setter
>
> **Exit criteria**:
> 1. Export button appears at the bottom-right, disabled when no model is loaded, enabled when a model is displayed
> 2. Clicking "Export GLB" opens a save dialog defaulting to `<model_name>.glb`
> 3. After saving, the export runs on a background thread with progress shown in status bar
> 4. On completion, the status bar shows "Exported X (N verts, M tris, S MB)"
> 5. The dropdown arrow opens the options menu with all checkable settings
> 6. Toggling options persists across app restarts (QSettings)
> 7. "Export as glTF" produces .gltf + .bin format instead of .glb
> 8. If "Embed Textures" is checked with no d4data path set, the dialog prompts the user
> 9. Ctrl+E keyboard shortcut triggers export
> 10. Exported .glb files are valid — test by opening in Blender or running `gltf_validator` if available

---

## Cross-cutting concerns

### Threading model

All blocking work runs on `QThread` workers. Three worker types:

| Worker | Triggered by | Emits |
|--------|-------------|-------|
| `CatalogWorker` | Phase 1 → 2 transition | `finished(list[str])` — SNO paths |
| `LoadModelWorker` | Model list click | `finished(MeshData)` — parsed model |
| `ExportWorker` | Export button click | `finished(Path)` — output file path |

Each worker also emits `progress(str)` for status updates and `error(str)` for failures.

**Rule**: PyVistaQt plotter calls (add_mesh, clear, reset_camera) happen ONLY on the main thread, never in a worker.

### Error handling

- CASC extraction failure → status bar error message + model list item marked red
- Parse failure → status bar error + viewport shows "Failed to parse model" overlay
- Export failure → dialog with error details + "Copy to Clipboard" button
- Invalid game directory → redirect to Phase 1 setup screen

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| Ctrl+O | Change game directory (back to Phase 1) |
| Ctrl+E | Export current model |
| Ctrl+F | Focus the filter bar |
| W | Toggle wireframe |
| F | Frame selected (reset camera to fit model) |
| Esc | Clear selection / close dropdown |

### Dark theme (QSS)

```css
QMainWindow, QWidget {
    background-color: #1e1e2e;
    color: #cdd6f4;
}

QListView {
    background-color: #181825;
    border: none;
    font-family: "Consolas", "Cascadia Code", monospace;
    font-size: 13px;
}

QListView::item:selected {
    background-color: #313244;
    color: #89b4fa;
}

QListView::item:hover {
    background-color: #262637;
}

QPushButton#exportButton {
    background-color: #89b4fa;
    color: #1e1e2e;
    font-weight: bold;
    border-radius: 4px;
    padding: 8px 16px;
}

QLineEdit {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 4px;
    padding: 6px;
    color: #cdd6f4;
}
```

### Application structure

```
d4extract/src/d4extract/gui/
├── __init__.py
├── main.py                  # Entry point, QApplication setup
├── main_window.py           # D4ExportWindow (QMainWindow)
├── widgets/
│   ├── setup_card.py        # Phase 1 — game directory picker
│   ├── model_list.py        # Phase 2 — scrollable model list + filter
│   ├── viewport.py          # Phase 3 — PyVistaQt wrapper
│   ├── properties_bar.py    # Phase 3 — info overlay
│   ├── texture_strip.py     # Phase 3 — thumbnail strip (future)
│   └── export_button.py     # Phase 4 — export button + options menu
├── workers/
│   ├── catalog_worker.py    # QThread for CASC catalog scan
│   ├── load_worker.py       # QThread for model extraction + parsing
│   └── export_worker.py     # QThread for GLB export
├── theme.py                 # QSS stylesheet + color constants
└── settings.py              # QSettings wrapper for persistent config
```

### Dependencies

```
PySide6>=6.6
pyvistaqt>=0.11
pyvista>=0.43
vtk>=9.3
numpy
```

Install: `pip install PySide6 pyvistaqt pyvista numpy`

Ensure `pyvistaqt` uses PySide6 backend (not PyQt5). Set `os.environ["QT_API"] = "pyside6"` before importing pyvistaqt if needed.

---

## Implementation order

Build and test each phase before starting the next. Each phase produces a runnable application:

1. **Phase 1**: Launches, shows setup card, validates game directory, saves to QSettings. Exit criterion: can select a valid D4 install and persist it.

2. **Phase 2**: On valid game dir, scans CASC, shows model list with filter. Exit criterion: can see all ~13K models, filter by text and category, cached for fast restarts.

3. **Phase 3**: Clicking a model extracts, parses, and renders in PyVistaQt viewport. Exit criterion: can orbit, zoom, and inspect any model with per-submesh coloring and properties overlay.

4. **Phase 4**: Export button writes .glb via GltfExporter with configurable options. Exit criterion: exported .glb passes Khronos glTF validator and opens correctly in Blender 5.1.

---

## Skills reference

These skills contain detailed technical specifications for each subsystem:

| Skill | Use for |
|-------|---------|
| d4-python-gui | PyVistaQt setup, VTK mesh construction, QThread patterns, widget layout |
| d4-casc-pipeline | RustyDemonCLI usage, CASC catalog, caching strategy, error handling |
| d4-binary-formats | .app file parsing, vertex buffer layouts |
| d4-coordinate-systems | D4 Z-up → glTF/VTK Y-up conversion for positions, normals, quaternions |
| d4-gltf-export | GltfExporter options, PBR materials, texture embedding, alpha modes |
| d4-materials-textures | Texture decode pipeline, shader slot mapping, cloth material filtering |
| d4-debugging | Diagnostic recipes when models look wrong |
