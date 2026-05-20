# D4Extract Importer (Blender addon)

Imports Diablo IV models exported by **d4extract** (`.glb` + the
`.materials.json` sidecar), rebuilds their PBR materials, forces
dithered alpha, and exposes the in-game customization dropdowns —
**Skin, Eyes, Makeup, Hair Color, Markings** — in the 3D View N-panel.

* Target: **Blender 5.1**
* Minimum: **Blender 4.2** (the dithered-alpha `surface_render_method`
  property was added in 4.2)

## Install

1. Build the extension zip (any Python 3.11+, no Blender needed):

   ```sh
   python make_dist.py
   ```

   This writes `dist/d4extract_blender-0.1.0.zip`.

2. In Blender: **Edit > Preferences > Get Extensions > Install from
   Disk...** (the ▾ menu, top-right) and pick the zip. Enable it if it
   does not auto-enable.

   On Blender 4.2 you can alternatively use the legacy
   **Add-ons > Install from Disk...** — the same zip works both ways.

## Usage

Open the 3D View sidebar (press **N**) and switch to the **D4 Tools**
tab.

### Import

* **Import D4 glTF...** — opens a file browser for a d4extract `.glb`.
  The matching `<model>.materials.json` next to it is picked up
  automatically. d4extract writes that sidecar **by default** now —
  if an import warns that no sidecar was found, re-export the model
  (or use the override field below).
* **Auto-setup materials** — when on, rebuilds PBR materials and strips
  D4's `COLOR_0` blend/AO vertex colours right after import.
* **Force dithered alpha** — when on, sets every imported material to
  the `DITHERED` surface render method. Facial-hair / stubble
  (`hero_hair_blend`) is deliberately left `BLENDED`.
* **Disable bone shape** — when on (the default), passes
  `disable_bone_shape=True` to the glTF importer so the armature's
  bones display as plain octahedrals instead of the importer's
  heuristic custom widgets. Added in Blender 4.4; on older versions the
  flag is detected as absent and the import proceeds without it.
* **Materials sidecar (override)** — a collapsible field for pointing
  at a `.materials.json` that does *not* sit next to the `.glb`. Leave
  it empty for the normal next-to-the-glb lookup. The path the importer
  searched is named verbatim in the "no sidecar" warning so a missing
  vs. misnamed file is easy to tell apart.

### Material Tools

* **Setup PBR Materials** — rebuilds the Principled BSDF graph for the
  selected objects from the sidecar: base colour (sRGB) with AO
  multiplied in, normal map with the DX→GL green-channel flip,
  roughness/metallic unpacked from d4extract's combined `MR_` image,
  emissive, Hero_Hair alpha. Idempotent — re-running never grows the
  node graph. Dye / skin masks are created as labelled, unconnected
  nodes for you to wire manually.
* **Set All → Dithered** — forces `DITHERED` on the selected objects'
  materials (whole scene when nothing is selected), sparing facial hair.

### Character Variants

* **Target** — the armature whose materials the variant swaps apply to.
  Auto-filled on import.
* **Skin** — a *parametric* tone picker: the 28 named player skin tones
  (Pale, Ivory, Bronze, Chocolate, … Expresso), each item showing its
  real colour swatch. It drives D4's `PersonaSkinColor` HSV system,
  **not** a texture swap: choosing a tone and pressing **Apply
  Variants** inserts a `d4_skin_hsv` (Hue/Saturation/Value) +
  `d4_skin_darken` (brightness multiply) node chain on **every skin
  material — face and body alike**.

### Why exposed-skin patches under armor tint correctly

Every armor piece ships `armor_skin_mat` placeholder materials for the
bare-skin cutouts — biceps, midriff windows, wrist gaps. Their static
export is a black stub (shader `hero_opaque`, base colour pointing at
`black.tex`); in-game D4's customization runtime replaces them with the
player's body skin. On import the addon does the same: with the
**Swap armor_skin_mat → body skin** toggle on (the default), it
reassigns every `armor_skin_mat` material slot to the character's body
skin material — the one carrying the real `SKIN_MASK` pore detail,
normal and roughness maps. After the swap all the cutouts share that
one datablock, so applying a skin tone tints it once and every patch
across torso / gloves / legs / boots inherits the change for free.

Monster, weapon-only and partial imports have no body skin material —
for those the swap is skipped (an INFO note says so) and the older
placeholder-grey HSV fallback in `setup_skin_tone_chain` still handles
any stray `armor_skin_mat`-like materials. Turn the toggle off to keep
the placeholder materials and fall back to that path on purpose.
* **Hair Color** — a *parametric* tone picker, sitting where the unused
  **Material** dropdown used to be. It exposes D4's 31 named hair
  colours (DeepBrown … BlackWhite), each item showing its real colour
  swatch. Like Skin it is **not** a texture swap: D4 ships every hair
  `BASE_COLOR` texture near-white as a tint target, so choosing a
  colour and pressing **Apply Variants** inserts a `d4_hair_tint`
  Multiply node on every hair material (`hero_hair` head/facial hair
  and `hair_pbr_igc` eyelashes) and feeds the chosen RGBA tint into it.
  The tint strength tracks each preset's `flHairColorInfluence`.
  *Caveat — duotone:* several palette entries (e.g. BlackTeal,
  BlueStorm, TealWhite) are two-colour in-game, blending a primary and
  a secondary tone through the hair root/tip mask. The current MVP
  applies the **primary** colour (`rgbaColors[0]`) only; the secondary
  / tertiary tones are carried through the sidecar for a future
  duotone pass but are not yet composited.
* **Eyes / Makeup / Markings** — image-swap dropdowns populated from
  the sidecar's `variants` block. **Apply Variants** reassigns the
  image datablock on the relevant texture node.

Image-swap variant textures are embedded in the `.glb` at export time
and loaded on import as `variant_<kind>_<id>` datablocks. An entry whose
texture was not embedded (`image_index = -1` in the sidecar) is listed
but its swap is skipped with a warning — re-export with `--with-textures`
after `d4extract extract-textures-for` to make those textures available.

The skin-tone palette is read from the player Actor definitions in the
d4data dump (`Actor/<class>.acr.json` → `ptPlayerData.arSkinColorChoices`,
identical across all classes) at export time; regenerate the checked-in
cache with `d4extract extract-skin-tones --d4data-path <d4data/json>`.

The hair-colour palette is read from `base/meta/HairColor/*.hcl.json`
(31 `HairColorDefinition` entries; the dev-garbage `Axe Bad Data` file
is filtered out); regenerate the checked-in cache with `d4extract
extract-hair-colors --d4data-path <d4data/json>`.

## Notes & limitations

* **Skin-tone HSV math.** D4's `flHue/flSaturation/flValue/flDarken` are
  runtime shader uniforms; Blizzard's exact HSV shader is not
  documented. The addon uses Blender's standard `Hue/Saturation/Value`
  node (hue rotated by `flHue`; saturation/value scaled by `1 + delta`)
  and multiplies by `flDarken` directly (`flDarken` is a brightness
  multiplier — 1.0 = unchanged, lower = darker). Expect some divergence
  from in-game at the extremes of the palette; a manual-override "Skin
  Tuning" sub-panel is a planned follow-up.
* The **Eyes** dropdown is normally empty. D4 eye colours are
  parametric (iris/sclera RGBA + shader uniforms, no per-colour
  texture); the exporter emits an empty block — this is expected, not
  a bug.
* The old **Material** (full-material persona swap) dropdown is gone
  from the panel — the Hair Color picker took its slot. The deferred
  full-material-swap feature is still not enumerable from the sampled
  data; the sidecar keeps emitting an empty `material` block and the
  `variant_material` property still exists internally, so the feature
  can be picked back up later without a schema change.
* **Makeup** and **Markings** are face overlays. Their textures are
  embedded and swappable onto the `d4_makeup` / `d4_markings` nodes
  (created on first apply), but they are **not** auto-composited over
  the skin — wire them in manually if you need the full look.
* The importer is single-piece for this version; multi-piece skeleton
  assembly is a separate effort.

## Layout

```
blender_addon/
├── blender_manifest.toml   extension manifest (Blender 4.2+)
├── __init__.py             register / unregister + bl_info fallback
├── preferences.py          AddonPreferences
├── properties.py           Scene.d4_props PropertyGroup
├── operators/              import, setup-materials, set-dithered, apply-variants
├── panels/                 the "D4 Tools" N-panel
├── core/                   sidecar parsing, material builder, variant catalog
└── make_dist.py            builds dist/d4extract_blender-0.1.0.zip
```
