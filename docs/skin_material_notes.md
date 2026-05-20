# D4 Skin Material System — Research Notes

## The `armor_skin_mat` Stub

Hundreds of armor/equipment pieces reference a shared stub material called `armor_skin_mat` (SNO 399607). This material is a **runtime placeholder** — the game engine swaps it with the player's body material at render time.

### `armor_skin_mat` contents
- Shader: `hero_opaque`
- Slot 1 (BASE_COLOR): `black.tex` — deliberate placeholder
- Slot 3 (NORMAL): null
- Slot 62 (ROUGHNESS): null
- Slot 81 (AO): null
- Runtime value: `Enable Recolor Chr` = 0
- No textures of substance — purely a stub

### Detection
- Material name is exactly `armor_skin_mat`
- Or: BASE_COLOR texture is `black.tex` with null normal/roughness

## Body Material Structure (`hero_opaque_skin` shader)

Each class+gender has one body material: `{prefix}_P00_BOD_mat.mat`

Example: `barM_P00_BOD_mat.mat` (SNO 664835)

### Texture slots

| Slot | Texture | Role |
|------|---------|------|
| 1 | `{prefix}_P00_BOD_color.tex` | Primary skin albedo (lighter tone) |
| 3 | `{prefix}_P00_BOD_normal.tex` | Body normal map |
| 62 | `{prefix}_P00_BOD_rough.tex` | Body roughness |
| 81 | null | AO (not bound) |
| 104 | `RogF_P00_BOD_Trans.tex` | Translucency/SSS (shared across some classes) |
| 145 | `{prefix}_P00_BOD_SkinMask.tex` | Blend mask between slot 1 and slot 234 |
| 234 | `{prefix}_p02_BOD_color.tex` | Secondary skin albedo (darker tone) |
| 235 | `{prefix}_P00_BOD_freckle_color.tex` | Freckle overlay layer |
| 236 | `{prefix}_P00_BOD_Vitiligo_color.tex` | Vitiligo/marking overlay layer |

### Runtime material values

- `Translucency Color Hero` (vec4): SSS tint — (0.117, 0.001, 0, 1) for barM
- `Translucency Intensity` (float): 0.5
- `Translucency Minimum Brightness` (float): 0.1
- `Skin Roughness` (float): 0.1
- `is Body Material` (float): 1.0 — flag engine uses to identify body materials
- `AO Multiplier` (float): 1.0
- `AO Brightness` (float): 0.35
- `Cavity Map Roughness Blend` (float): 0.2

## Skin Tone System

D4 does NOT use 4 separate body materials for 4 skin tones. Instead, ONE material
contains two baked albedos and blends between them:

- **Slot 1** = lighter skin tone (barM P00 avg RGBA ≈ 0.38, 0.20, 0.15)
- **Slot 234** = darker skin tone (barM P02 avg RGBA ≈ 0.20, 0.08, 0.04)
- **Slot 145** (SkinMask) = spatial blend mask
- Player's skin color preset drives the overall blend factor

Face presets P00–P03 ("Caucasian", "Asian", "African", "Persian") likely
select different blend weights and layer intensities rather than entirely
different texture sets.

## Average Colors (from TextureDefinition.rgbavalAvgColor)

These are the fallback tints available without CASC texture extraction:

### barM (Male Barbarian)
- P00 BOD_color: (0.383, 0.198, 0.150, 1.0) — light/medium Caucasian
- p02 BOD_color: (0.198, 0.080, 0.043, 1.0) — darker African tone

## Blender Addon Reconstruction Plan

For full-fidelity skin rendering in Blender:

1. Detect `armor_skin_mat` submeshes (name match or black.tex BASE_COLOR)
2. Replace with a node group:
   - Two Image Texture nodes (slot 1 + slot 234) mixed by SkinMask (slot 145)
   - Freckle (slot 235) and Vitiligo (slot 236) composited on top
   - "Skin Preset" enum property driving the blend factor
3. Same pattern applies to:
   - **Eye color**: `Iris_InnerColor_Eye` runtime vec4, no BASE_COLOR texture
   - **Markings/makeup**: Additional overlay slots with runtime intensity values
   - All three share a "Character Customization" sidebar panel

## Python GUI Approach (viewport-only, NOT export)

For the PyVista viewport, apply `rgbavalAvgColor` from the selected face preset's
BOD_color texture as a flat color tint to `armor_skin_mat` submeshes. This gives a
reasonable approximation without CASC extraction or multi-texture compositing.
