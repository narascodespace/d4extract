# d4extract

This is a vibe coded slopfest but it works, for now. I don't have any more time to dedicate to the project so anyone who wants to fork this and take it to the finish line feel free. I will not be responding to issues, etc.

There will be animation decoder and texture decoder errors as I couldn't figure out how to read everything, but things should still export properly regardless of the errors. There are still some skin material bugs that the blender addon does not fix - not sure why, but since the textures definitely get extracted you can wire them up in the shader editor on your own as you like.

Anything in the game that is primarily composed of VFX shaders (banshees for example) will just not be extracted all that well. And weapons, etc. those effects are just not happening - again, Claude did all this on its own with MEGAGOOD prompting on my part but I feel like if someone out there wants to take it even further they could properly read the shaders.

Extract Diablo IV character, weapon, and environment models out of the
game's CASC archive and convert them to glTF for use in Blender,
Maya, Unreal, or anywhere else that consumes the format.

The project ships three pieces:

- **`d4extract-gui`** — a PySide6 desktop app for browsing the
  archive, previewing models with their textures, building combined
  character outfits, and exporting to `.glb` / `.gltf`.
- **`d4extract`** — a Click CLI that exposes the same primitives for
  scripts, CI, and batch jobs.
- **`d4extract_blender`** — a Blender 5.1+ add-on that imports the
  exported `.glb` files with the right material setup
  (PBR + alpha + dither + parametric variants like skin/hair colour).

## Requirements

- A local Diablo IV installation (Steam or Battle.net)
- For the Blender side: Blender 5.1 or newer

## First-run setup

The GUI walks you through this; the CLI takes the same steps
explicitly.

1. **Point d4extract at your Diablo IV install.** Steam (`…/steamapps/
   common/Diablo IV`) and Battle.net (`…/Diablo IV/`, contains
   `.build.info`) are both auto-detected on Windows.
2. **Install d4data separately** — see
   [§ d4data](#d4data--required-install-separately) below. d4extract
   does **not** bundle or download it; you clone or download the ZIP
   yourself and point the app at the folder.
3. (Optional) **Load TACT keys** to decrypt unreleased / seasonal
   content. The base game extracts fine without them — see the TACT
   Keys section below.

The GUI keeps these settings in QSettings; the CLI honours the same
values and accepts overrides via flags or environment variables.

### d4data — required, install separately

d4extract needs the community-maintained metadata repository
**d4data** to resolve model and material references. **d4extract
does not bundle or download it for you** — d4data updates on every
Diablo IV patch and is best maintained as a separate clone you
control.

1. Clone or download d4data from
   [https://github.com/blizzhackers/d4data](https://github.com/DiabloTools/d4data).

   Download the ZIP from the GitHub page and extract it
   somewhere stable (e.g. `%LOCALAPPDATA%\d4extract\d4data` or
   next to your d4extract install).

2. On first launch, d4extract will ask you to select the d4data
   folder. Pick its `json/`
   subdirectory.

3. To get newer game patch metadata later, re-download the ZIP and extract inside that folder. d4extract picks up the
   new files immediately; no app reinstall needed.

## GUI quick tour

```bash
d4extract-gui
```

- **Model Browser** — left pane is a searchable list of every
  appearance SNO in the archive. Click one to load it into the 3-D
  viewport on the right; per-submesh visibility toggles live in the
  far-right pane.
- **Character Builder** — pick a class, then mix-and-match equipment
  pieces under one shared skeleton. Drag-to-rotate; export the
  combined outfit to a single glTF that imports as one character.
- **File menu** — directory pickers (game install, d4data folder)
  and TACT-key load/clear all live here.

The GUI exports to `.glb` (recommended) or `.gltf` + textures.
Skinned models export with a full skeleton and `JOINTS_0` / `WEIGHTS_0`
attributes by default.

## CLI

```bash
# Inspect what's in the archive
d4extract info "C:\Program Files (x86)\Diablo IV"
d4extract list "C:\Program Files (x86)\Diablo IV" --filter "base/meta/Appearance/sorc*"

# Pull a model pair and convert it
d4extract sample "C:\Program Files (x86)\Diablo IV" --count 1
d4extract export <meta.app> <payload.app> -o sorc.glb

# Pull every texture referenced by a model in one batched call
d4extract extract-textures-for "C:\Program Files (x86)\Diablo IV" warM_H01 \
    --d4data-path C:\path\to\d4data

# Animation extraction (meta + payload pair, with shared-payload alias resolution)
d4extract extract-anim "C:\Program Files (x86)\Diablo IV" barM_HTH_nav_idle \
    --d4data-path C:\path\to\d4data
```

Useful `export` flags:

- `--no-skin` — omit the skeleton, write a static mesh
- `--prune-bones` — trim the skeleton to weighted bones + parent chain
- `--validate` — run the Khronos glTF validator if installed
- `--tact-keys <path>` — supply TACT keys for this invocation

Run `d4extract <command> --help` for the full surface of any
subcommand.

## TACT Keys (optional)

Diablo IV encrypts a small slice of content (unreleased cosmetics,
seasonal items). Decrypting it requires
[TACT keys](https://wowdev.wiki/TACT), which this project does **not**
distribute due to DMCA risk.

Most users never need keys — base game models extract without them.

If you do need keys:

1. Obtain a `.txt` or `.csv` key file in
   [wowdev format](https://github.com/wowdev/TACTKeys). One key per
   line, `KEYID KEYVALUE` separated by a space or semicolon, `#` for
   comments.
2. **GUI:** `File → Load TACT Keys…`. The app validates the file and
   copies it into your local app data directory.
3. **CLI:** pass `--tact-keys <path>` to any subcommand, or set the
   `D4EXTRACT_TACT_KEYS` environment variable.

Resolution order: explicit `--tact-keys` flag → GUI-saved path →
`D4EXTRACT_TACT_KEYS` env var → none (encrypted content is silently
skipped).

## Blender add-on

`blender_addon/` is a self-contained Blender 5.1+ add-on for
importing the `.glb` files d4extract produces. It hooks the standard
glTF importer to:

- Build PBR materials with the right alpha mode (cutout / dithered),
  normal-map decoding, and emissive multipliers
- Wire up parametric variants (skin tones, hair colours, eye colours,
  body markings) as per-material colour controls
- Swap "armor skin" material slots to the body's skin material so
  exposed-skin surfaces match the rest of the character

The addon is included in the releases section.

## Project layout

```
src/d4extract/
    casc/         # rustydemon-cli wrapper, payload resolver
    cli.py        # Click commands
    config.py     # User-level TACT-keys config (QSettings-backed)
    export/       # glTF writer
    formats/      # .app / .ani / .tex / material parsers
    gui/          # PySide6 desktop app
    setup/        # Headless d4data path validation
blender_addon/    # Blender 5.1+ add-on
packaging/        # PyInstaller spec + build scripts
docs/             # Format specs, research notes, roadmap
tests/            # pytest suite (Qt-free)
```

## Licence
- TACT keys and any Diablo IV game assets you extract belong to
  Blizzard. d4extract is a tool; what you do with the output is on
  you.
