---
name: d4-vfx-shaders
description: "Diablo IV VFX shader system for weapon elemental effects. Covers fxShellMat/fxMeshMat identification, vfx_actor_weaponShell ShaderMap with 12 element overrides, eShaderTex VFX texture slots (200=Mask, 193=ColorGradient, 197-199=Noise/Distortion), 28 MaterialValue parameters with defaults, render state, DXBC/DXIL bytecode decompilation path, and Blender/Unreal shader reconstruction. Use this skill whenever identifying FX submeshes, extracting VFX textures, building elemental weapon effect approximations in Blender or Unreal, decompiling D4 shaders, or understanding why submeshes have no PBR textures. Also trigger for weapon enchant effects, elemental overlays, or shader maps."
---

# D4 VFX Shaders

This skill documents the Diablo IV VFX shader system used for weapon elemental effects (fire, lightning, poison, shadow, physical, cold). About 8% of weapon/armor appearances include FX material submeshes alongside the standard PBR submesh. These FX submeshes use a completely different rendering path — no PBR textures, procedural noise-driven effects, additive transparency — and require special handling in the extraction pipeline.

## FX material identification

Two global materials appear across all weapon/armor models that have elemental effects:

### fxShellMat (MaterialDefinition)

The "weapon shell" — a full duplicate of the model's geometry rendered as a semi-transparent elemental overlay.

```
fxShellMat.mat
  ├── snoShaderMap → vfx_actor_weaponShell.shm (12 element overrides)
  ├── ptMatTexList: [] (zero textures — all textures come from the shader)
  ├── dwFlags: 2048
  └── ptRunTimeMaterialValues:
       ├── "Enable Recolor Chr" = 0
       └── "Enable Recolor VFX" = 0
```

The shell submesh typically has the same triangle count as the real mesh (same geometry, different material slot). At runtime the game engine selects which element shader to apply based on the weapon's current enchantment via `dwShaderMapOverride`.

### fxMeshMat (MaterialDefinition)

A small helper mesh (typically 11-33 triangles) used as an anchor for particle/trail VFX.

```
fxMeshMat.mat
  ├── snoShaderMap → fxMesh.shm
  ├── ptMatTexList: [] (zero textures)
  └── ptRunTimeMaterialValues: [] (none)
```

This submesh is usually a strip or set of quads positioned along the weapon's edge or tip. The `fxMesh` shader renders weapon trails during attack animations. Because the geometry is so minimal, it may not appear in the GUI's submesh list if it falls below rendering thresholds.

### Identifying FX materials programmatically

FX materials are identifiable by any of these properties:

1. **Zero textures**: `ptMatTexList` is empty (the definitive signal — real PBR materials always have at least a base color texture)
2. **ShaderMap name**: contains `vfx_actor` or `fxMesh`
3. **Material name**: starts with `fx` (e.g., `fxShellMat`, `fxMeshMat`, `fxMesh_mat_default`)
4. **dwFlags**: 2048 (though this alone is not sufficient)

For export pipelines, FX submeshes should either be filtered out (they're meaningless without the runtime shader) or tagged with metadata so a target engine can apply its own effects.

## Typical weapon appearance structure

A weapon appearance with elemental FX has 3 material slots and 3 SubObjects at LOD0:

```
ptAppearanceMaterials[]:
  slot 0: fxMeshMat      → trail/particle anchor mesh (~33 verts, ~11 tris)
  slot 1: fxShellMat     → shell overlay mesh (~2800 verts, same geo as slot 2)
  slot 2: <weapon>_mat   → actual PBR mesh (~2500 verts, has textures)

SubObjects at LOD0:
  SubObject 0: matIdx=0, vb=0  ← fxMeshMat (separate vertex buffer, tiny)
  SubObject 1: matIdx=1, vb=1  ← fxShellMat (shares vertex buffer with slot 2)
  SubObject 2: matIdx=2, vb=1  ← real mesh
```

The shell (slot 1) and real mesh (slot 2) share the same vertex/index buffer but reference different index ranges. The fxMesh (slot 0) has its own small vertex buffer.

## ShaderMap: vfx_actor_weaponShell

The ShaderMap is the lookup table that maps runtime element selection to compiled shaders. `vfx_actor_weaponShell.shm` contains 12 element overrides:

| dwShaderMapOverride | Shader Name               | Category |
|---------------------|---------------------------|----------|
| 0                   | GLOBAL_frozen             | Frozen   |
| 18                  | Weapon_Player_Fire        | Player   |
| 33                  | Weapon_Player_Lightning   | Player   |
| 34                  | Weapon_Player_Poison      | Player   |
| 35                  | Weapon_Player_Shadow      | Player   |
| 36                  | Weapon_Player_Physical    | Player   |
| 37                  | Weapon_Monster_Fire       | Monster  |
| 38                  | Weapon_Monster_Cold       | Monster  |
| 39                  | Weapon_Monster_Lightning  | Monster  |
| 40                  | Weapon_Monster_Poison     | Monster  |
| 41                  | Weapon_Monster_Shadow     | Monster  |
| 42                  | Weapon_Monster_Physical   | Monster  |

At runtime, the game engine sets `dwShaderMapOverride` on the actor based on the weapon's active elemental enchantment. The shader map resolves this to a specific `ShaderDefinition` which contains the compiled GPU programs and texture bindings for that element.

## Texture slot mapping (eShaderTex)

Each element shader binds 6-8 textures using `eShaderTex` slot IDs in the `tDefaultTextures` array. The slot structure is consistent across all elements — only the specific textures swapped in differ:

| eShaderTex | Role             | Description                                       | Consistent? |
|------------|------------------|---------------------------------------------------|-------------|
| 200        | Mask             | Where effect appears on mesh surface               | Same for all elements (`fxKit_weaponShell_Mask`, 256x256) |
| 193        | Color Gradient   | Maps noise intensity to element-specific colors    | Different per element |
| 197        | Primary Noise    | Main pattern driver (fire swirls, lightning bolts)  | Different per element |
| 198        | Secondary Noise  | Turbulence/detail layer                            | Different per element |
| 199        | UV Distortion    | Animated UV warping for motion                     | Different per element |
| 91         | Black Fallback   | 4x4 no-op texture                                 | Same for all elements (`black_blackalpha`) |
| 194-196    | Extra (some)     | Lightning and Cold use additional slots             | Element-specific |

### Per-element texture assignments

**Fire** (Weapon_Player_Fire):
- 200: `fxKit_weaponShell_Mask` (256x256)
- 193: `colorGradient_fire_02_ColorGradient` (256x256)
- 197: `curlyFire_noise` (1024x1024)
- 198: `environmentVFX_boilingFire_Noise` (256x256)
- 199: `environmentVFX_fogSwirl_UVDistortion` (512x512)
- 91: `black_blackalpha` (4x4)

**Lightning** (Weapon_Player_Lightning):
- 200: `fxKit_weaponShell_Mask`
- 194: `fxKit_colors_monster_primary_ColorGradient`
- 195-196: `fxKit_lightning_Noise1`
- 197-198: `fxKit_StepUVs`
- 199: `environmentVFX_fogSwirl_sigil_edit_UVDistortion`
- 91: `black_blackalpha`

**Physical** (Weapon_Player_Physical):
- 200: `fxKit_weaponShell_Mask`
- 193: `fxKit_damaging_physical_primary_ColorGradient`
- 197: `environmentVFX_largeScaleFlame_Noise2`
- 198: `environmentVFX_boilingFire_Noise`
- 199: `uvDist_noise_clouds_24`
- 91: `black_blackalpha`

**Poison** (Weapon_Player_Poison):
- 200: `fxKit_weaponShell_Mask`
- 193: `fxKit_aoe_explosion_poison_colorBrown_ColorGradient`
- 197: `painterlyFlow_Noise3`
- 198: `fxKit_demonic_shapes_Noise`
- 199: `uvDist_gaussianNoise_UVDistortion`
- 91: `black_blackalpha`

**Shadow** (Weapon_Player_Shadow):
- 200: `fxKit_weaponShell_Mask`
- 193: `rogue_shadowRealm_ColorGradient`
- 197: `environmentVFX_largeScaleFlame_Noise2`
- 198: `environmentVFX_boilingFire_Noise`
- 199: `uvDist_noise_clouds_24`
- 91: `black_blackalpha`

**Cold** (Weapon_Monster_Cold):
- 200: `fxKit_weaponShell_Mask`
- 193: `fxKit_colors_monster_primary_ColorGradient`
- 197: `iceMat_Noise`
- 198: `iceSurface_Noise4`
- 194: `environmentVFX_fogSwirl_sigil_edit_UVDistortion`
- 196: `environmentVFX_waterfallOffset_Noise2`
- 91: `black_blackalpha`

All textures are stored in CASC under `base/meta/Texture/` (meta) and `base/payload/Texture/` (pixel data). They use standard D4 texture formats (eTexFormat 41=BC1, 42=BC3, 50=BC6H for HDR gradients) and are extractable through the existing texture pipeline.

## Material parameters

Each element shader declares 28 scalar parameters and 1 vector parameter in `tUsedScalarMaterialValues` / `tUsedVectorMaterialValues`. These are the artist-tunable knobs the shader reads at runtime:

### Core visual parameters (with defaults)
| Parameter              | Default | Purpose                                    |
|------------------------|---------|--------------------------------------------|
| Color Intensity        | 1.0     | Overall brightness multiplier              |
| emissive multiplier    | 1.0     | Emission strength                          |
| HSV_HueShift           | 0.0     | Hue rotation (0-1 range)                   |
| Color Saturation       | 1.0     | Saturation multiplier                      |
| Fresnel Double Sided   | 0.0     | Whether fresnel applies to back faces      |
| Use Vertex Color       | 0.0     | Blend in vertex color data                 |
| Enable Damage Remap    | 1.0     | Whether damage state affects the effect    |

### Dye/recolor system
| Parameter                          | Default | Purpose                           |
|------------------------------------|---------|-----------------------------------|
| Enable Recolor VFX                 | 0.0     | Master toggle for recolor system  |
| Recolor Main Hue VFX              | 0.0     | Primary target hue               |
| Recolor Hue Range VFX             | 0.05    | Hue matching tolerance           |
| Recolor Replace Hue VFX           | 0.42    | Replacement hue value            |
| Recolor Saturation Offset VFX     | 0.0     | Saturation adjustment            |
| Recolor Value Offset VFX          | 0.0     | Value/brightness adjustment      |
| Recolor Roughness Offset VFX      | 0.0     | Roughness adjustment             |
| Recolor Value Multiply VFX        | 1.0     | Value multiplier                 |
| Secondary Recolor Main Hue VFX    | 0.0     | Second recolor target hue        |
| Secondary Recolor Hue Range VFX   | 0.0     | Second recolor tolerance         |
| Secondary Recolor Replace Hue VFX | 0.7     | Second recolor replacement       |
| Secondary Recolor Value Offset VFX | 0.0    | Second recolor brightness        |
| Secondary Recolor Saturation Offset VFX | 0.0 | Second recolor saturation      |
| Secondary Recolor Value Multiply VFX | 1.0   | Second recolor value multiplier  |

### Other parameters
| Parameter                          | Default |
|------------------------------------|---------|
| Exposure Compensation Enabled      | 1.0     |
| VFX Dye Gradient                   | 0.0     |
| VFX Dye Color Saturation           | 1.0     |
| VFX Dye Color Intensity            | 1.0     |
| Inherit Armor Dye Color non static | 0.0     |
| Recolor for Helltide Water         | 0.0     |
| Remote Player Alpha Fade Value     | 1.0     |
| Color Global Tint                  | vec4    |

The parameter set is identical across all 12 element shaders — the same 29 knobs with the same defaults. Element-specific look is driven entirely by which textures are bound, not by parameter differences.

## Render state

All weapon shell shaders share the same render configuration:

```
Pass 0 (renderLayer=1):
  Alpha Blend:  ENABLED (src=SrcAlpha, dst=InvSrcAlpha, op=Add)
  Z-Write:      OFF (transparent overlay, reads but doesn't write depth)
  Cull Mode:    2 (back-face culling)
  Alpha Test:   ENABLED (ref=0, func=Greater)
  Stencil:      ENABLED (ref=64, write mask=64)

Pass 1 (renderLayer=34):
  Same blend/depth settings as Pass 0
  Stencil:      DISABLED
```

The two-pass setup suggests: Pass 0 renders the effect with stencil marking (for interaction with other VFX), Pass 1 renders an additional layer without stencil (possibly for screen-space effects or a different compositing stage).

The blend mode `src=SrcAlpha, dst=InvSrcAlpha` is standard alpha blending (not pure additive). With Z-write off and alpha test enabled, the shell renders as a transparent overlay that doesn't occlude other geometry.

## Compiled shader bytecode

Each element shader's `ShaderDefinition` has an external payload containing compiled GPU programs:

```
ShaderDefinition payload:
  dataOffset: 32
  dataSize:   ~375-389KB per element
  Format:     DXBC or DXIL (DirectX 12 shader bytecode)
  Encrypted:  NO (shaders are not in EncryptedSNOs.dat)

Per shader:
  2 render passes x 15 permutations x 4 programs (VS + PS x 2 variants)
  = 120 compiled shader programs per element
```

The `pShaderProgram` arrays in the d4data JSON are empty because the bytecode lives in the payload file, not the meta. The permutations correspond to `dwShaderPermFlags` values (0-4, 4096-4100, 8192-8196) which likely represent feature toggles like skinning on/off, instancing, quality levels.

### Decompilation path

The bytecode is not encrypted and can be decompiled using standard DirectX tooling:

1. **Extract** the shader payload from CASC (`base/payload/Shader/Weapon_Player_Fire.shd`)
2. **Isolate** individual DXBC blobs from the payload (need to parse the permutation table to find offsets — the `dataOffset` + `dataSize` in the meta give the full payload bounds, but individual programs within it need offset calculation)
3. **Decompile** using Microsoft's `dxc` compiler (`dxc -dumpbin`), RenderDoc's shader viewer, or open-source tools like `spirv-cross` (if SPIR-V) or shader decompilers
4. **Read** the resulting HLSL to understand UV scroll speeds, noise combination math, fresnel curves, color ramp sampling logic

This gives you the exact shader logic — it is the actual code that runs on the GPU. The decompiled HLSL can then be translated node-for-node into Blender shader nodes or Unreal material graphs.

## Reconstruction in Blender

Based on the texture roles, parameter names, and render state, the weapon shell effect follows a well-known VFX pattern that can be approximated in Blender's Shader Editor without decompilation:

### Shader architecture (inferred from inputs)

```
UV Coordinates
  |-- + UV Distortion texture (scrolling, animated via driver)
  |     \-- Distorted UVs
  |           |-- Sample Primary Noise -> value A
  |           \-- Sample Secondary Noise -> value B
  |                 \-- Combine (multiply or screen) -> noise_intensity
  |
  |-- noise_intensity -> Color Ramp lookup (element-specific gradient)
  |                        \-- element_color (RGB)
  |
  |-- Fresnel node -> edge_glow (fresnel factor)
  |
  \-- Mask texture -> mask_factor (where effect appears)

Final = element_color * mask_factor * fresnel_mix * Color Intensity
Output via Emission shader + Transparent BSDF mixed by alpha
Material blend mode: Alpha Blend
```

### Blender implementation notes

- Use **EEVEE** for preview (handles transparency and additive blending like a game engine)
- Animate UV scroll with **Drivers** on a Mapping node's Location (tie to `#frame`)
- The Color Gradient textures are 1D ramps — can be loaded as Image Texture with `Extend` or rebuilt as a ColorRamp node
- Fresnel node in Blender is a direct equivalent
- Material settings: Surface = Mix Shader (Transparent + Emission), Blend Mode = Alpha Blend, Shadow Mode = None, Backface Culling = on

### What you can get right without decompilation

- Texture roles and which textures to use per element
- Blend mode and transparency behavior
- Fresnel edge glow (the concept, though the exact curve needs the bytecode)
- Color ramp lookup from noise
- General UV distortion + scrolling noise pattern

### What requires decompilation for accuracy

- Exact UV scroll speeds and directions
- How the two noise layers combine (add? multiply? screen? max?)
- Fresnel power/curve shape
- Whether noise values are remapped before the color ramp lookup
- Any per-vertex animation or time-based oscillation

## Reconstruction in Unreal

The Unreal Material Editor maps even more naturally to this shader since it's essentially a visual HLSL graph:

- **Texture Sample** nodes for each extracted texture
- **Panner** nodes for UV scrolling (equivalent to the animated UV offset)
- **Fresnel** node (built-in, with adjustable exponent)
- **LinearInterpolate** for noise combination
- **ColorGradient** via a Curve Atlas or 1D texture lookup
- Material Domain: **Surface**, Blend Mode: **Translucent**, Shading Model: **Unlit** or **Default Lit** with emissive
- The material can be parameterized with Material Parameter Collections matching the 28 D4 scalar parameters

Since the D4 shader system uses the same slot structure across all elements with only texture swaps, build one **Material Instance** base and create per-element instances that override only the texture parameters.

## Pipeline integration

### Extracting VFX data alongside models

When exporting a weapon model, the pipeline should:

1. **Identify FX submeshes** by checking `ptMatTexList == []` on resolved materials
2. **Extract VFX textures** from the shader's `tDefaultTextures` array (requires resolving the material -> shader map -> shader -> tDefaultTextures chain through d4data JSON)
3. **Tag submeshes** in glTF extras with their FX role:
   ```json
   {"d4_fx_type": "shell", "d4_shader_map": "vfx_actor_weaponShell", "d4_element": "fire"}
   ```
4. **Embed or sidecar** the VFX textures with their slot roles
5. **Include parameter defaults** so a target engine can populate its own material

### d4data JSON resolution chain for VFX textures

```
AppearanceDefinition (e.g., 2HBow_Unique_AF_001.app.json)
  \-- ptAppearanceMaterials[1].ptSOAs[0].snoMaterial
        -> MaterialDefinition (fxShellMat.mat.json)
            \-- tUberMaterial.snoShaderMap
                  -> ShaderMapDefinition (vfx_actor_weaponShell.shm.json)
                      \-- arShaderOverrides[override_id].snoMedDefault
                            -> ShaderDefinition (Weapon_Player_Fire.shd.json)
                                \-- ptPasses[0].tDefaultTextures[]
                                      \-- eShaderTex + snoTexture
                                            -> TextureDefinition (curlyFire_noise.tex.json)
```

This is a 4-hop chain: Material -> ShaderMap -> Shader -> Texture. The existing `material_parser.py` handles the first hop (Material -> Texture for PBR). VFX texture extraction would need to continue through the ShaderMap and Shader hops.

### Key files in codebase

| File | Relevance |
|------|-----------|
| `d4data/json/base/meta/Material/fxShellMat.mat.json` | Shell material definition |
| `d4data/json/base/meta/Material/fxMeshMat.mat.json` | Mesh trail material definition |
| `d4data/json/base/meta/ShaderMap/vfx_actor_weaponShell.shm.json` | Element override lookup table |
| `d4data/json/base/meta/Shader/Weapon_Player_*.shd.json` | Per-element shader definitions |
| `d4data/json/base/meta/Shader/Weapon_Monster_*.shd.json` | Monster element variants |
| `d4extract/src/d4extract/formats/material_parser.py` | Current material resolution (PBR only) |
