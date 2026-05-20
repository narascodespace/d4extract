# Packaging & Distribution Plan

How d4extract gets from "Python source on the maintainer's machine" to
"double-clickable .exe that a Diablo IV player can use without
installing anything else."

## Target user experience

1. User opens the GitHub Releases page.
2. Downloads two files: `d4extract.exe` and
   `d4extract_blender-<version>.zip`.
3. Double-clicks `d4extract.exe`. Windows SmartScreen may prompt; user
   clicks "More info → Run anyway".
4. The app's existing `SetupCard` flow guides them through:
   - Picking the Diablo IV install (auto-detected for Steam/B.net).
   - Letting d4extract download `d4data` for them (or pointing at an
     existing checkout).
   - Optionally loading TACT keys via the `File → Load TACT Keys…`
     menu — the non-blocking banner reminds them but never forces it.
5. They can browse, preview, and export models. The Blender addon is
   installed separately in Blender via `Edit → Preferences → Get
   Extensions → Install from Disk…`.

No Python, no pip, no terminal at any step.

## The three independent pieces

### 1. PyInstaller bundle (done)

`packaging/d4extract.spec` + `packaging/build.ps1` produce
`dist\d4extract.exe`. The spec handles the PySide6 / VTK / PyVista
landmines and trims ~300 MB of unused Qt modules.

Build it locally for now. Migrate to GitHub Actions once the manual
flow is stable (Windows runner, `pip install -e .[dev]`, run
`build.ps1`, attach `dist\d4extract.exe` to the release).

### 2. d4data downloader (done)

`src/d4extract/setup/d4data_downloader.py` fetches the canonical
`blizzhackers/d4data` zipball from GitHub and installs it under
`%LOCALAPPDATA%\d4extract\d4data\`. Atomic — partial extracts never
clobber a working install.

**Not yet wired into the GUI.** The downloader is a headless module
with full unit-test coverage and no Qt dependency. The GUI integration
is the next step (see "Wiring TODOs" below).

### 3. rustydemon-cli bundling

The existing path-resolution in `src/d4extract/casc/rustydemon.py`
already checks `./rustydemon/target/release/rustydemon-cli` and
`~/.cargo/bin/`. The `build.ps1 -RustydemonExe <path>` flag drops a
copy of the binary into `dist\rustydemon\` next to `d4extract.exe`,
which the resolver finds without any code changes.

**Action item:** check `rustydemon`'s LICENSE (likely MIT/Apache) and
confirm we can redistribute the compiled binary. If we cannot, the
backup plan is to download it from its own GitHub Releases on first
launch, similar to d4data.

## Wiring TODOs

These are the integration steps that connect the downloader module to
the GUI. They each touch existing wired-up files (`main_window.py`,
`settings.py`, the menu bar), so they are best done in a focused
session that can also handle the messy working tree state.

### A. `D4DataCard` widget

Mirror `widgets/setup_card.py`. Shown when `AppSettings.d4data_path()`
returns `None`. Offers two buttons:

- **Download for me** — runs `download_and_install_d4data()` in a
  `QThread`, with a `QProgressBar` driven by the `progress` callback.
  On success, calls `settings.set_d4data_path(default_d4data_dir())`.
- **I have it already** — opens a `QFileDialog` for the user to point
  at an existing checkout. Validates with `is_d4data_dir()` before
  saving.

### B. `D4DataDownloadWorker`

Goes in `gui/workers/d4data_worker.py`. A `QRunnable` or `QThread`
that wraps `download_and_install_d4data()` and forwards the
`(done, total, stage)` tuple as a Qt signal so the progress bar
updates on the main thread.

### C. Menu entries

Add to `main_window.py`'s `File` menu:

- **Set d4data Folder…** — file picker, validates, saves.
- **Re-download d4data** — re-runs the downloader. Useful after D4
  patches.

### D. Banner

The existing TACT-key banner pattern in `main_window.py` is the model.
Add a sibling that fires when `settings.d4data_path()` is `None`, with
a "Set up d4data" button that opens the `D4DataCard`.

### E. CLI mirror

The non-GUI `d4extract` CLI should expose the same downloader as
`d4extract setup d4data [--target PATH]` so headless users on
servers/CI can install d4data without launching the GUI.

## Distribution mechanics

**Release artifacts (on every `v*` tag):**

- `d4extract.exe` — the PyInstaller bundle.
- `d4extract_blender-<version>.zip` — built via
  `blender_addon/make_dist.py`.
- A short release notes block summarizing the changes.

**Version numbering:** keep `pyproject.toml`, the Blender
`blender_manifest.toml`, and the git tag in sync. A `make_release.py`
script that bumps all three is a nice-to-have for later.

**README user-facing section:**

> ### Quick start (no Python required)
>
> 1. Download `d4extract.exe` and `d4extract_blender-<version>.zip`
>    from the [latest release](../../releases/latest).
> 2. Run `d4extract.exe`. The first-launch wizard handles d4data and
>    your Diablo IV install path.
> 3. In Blender, go to `Edit → Preferences → Get Extensions → Install
>    from Disk…` and pick the addon zip.

Move the existing pip/uv instructions under a `### Building from
source (for developers)` heading.

## Things deliberately deferred

- **Inno Setup installer.** A folder of files (`--onedir`) + Inno
  Setup gives a proper "installs to Program Files" experience with a
  Start Menu entry and an uninstaller. Skip for v0 — a single .exe is
  fine to start.
- **Code signing.** A signed binary skips the SmartScreen warning but
  costs $200–$400/year. Wait until usage justifies it.
- **Auto-updater.** For v0, users redownload from GitHub Releases.
  `tufup` or `pyupdater` is a v2+ concern.
- **macOS / Linux builds.** d4extract is Windows-only by virtue of D4
  itself being Windows-only. Linux is technically possible (Diablo
  runs under Proton) but is not a launch target.

## Open questions for the maintainer

1. Is `blizzhackers/d4data` actually the canonical fork to point at,
   or have you been working off a different one? The downloader's
   `D4DataSource` default lives at the top of `d4data_downloader.py`
   — single line to change.
2. What is the rustydemon-cli license? Determines whether we bundle
   the binary or download it.
3. Do you want an in-app "Check for updates" entry that hits the
   GitHub Releases API, or is "redownload from the website" fine
   for v0?
