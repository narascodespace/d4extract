# Diablo IV Material/Texture Format Specification

**Status:** RESEARCH — produced from `d4data/definitions.json` (struct layouts) plus
the JSON dumps of three sample appearances (`barF_H07`, `Goatman_BossTrophy`,
`necF_stor229_HLM`), their referenced `.mat.json` material files, and several
`.tex.json` texture metadata files. No binary `.mat` / `.tex` payloads were
read in this phase — pixel data lives in CASC payloads that d4data does not
ship.

**Confidence:** struct layouts, the SubObject → Material → Texture reference
chain, and the slot-enum mapping in §3.2 are HIGH (verified across 3 sample
models, 5 referenced materials, and 12 texture metadata files). The exact
`eTexFormat` → DXGI mapping (§4.3) is MEDIUM — inferred from
bytes-per-pixel ratios in `serTex` mip records, not from the format enum
itself, which we have not located.

---

## 1. The reference chain

```
AppearanceDefinition  (snoGroup 9)
  └─ ptAppearanceMaterials : array<AppearanceMaterial>          (+0x B0 = 176)
        ├─ dwMaterialHash                                       (+0x00) name hash
        ├─ fPersonaMaterial / dwPersona                          (+0x04 / +0x08)
        └─ ptSOAs : array<SubObjectAppearance>                   (+0x10) one per persona variant
              ├─ dwFlags                                          (+0x00)
              ├─ snoMaterial : DT_SNO<group 57>                   (+0x04) → MaterialDefinition
              ├─ snoOverrideMaterial                              (+0x08)
              ├─ snoCloth / snoHighQualityClothOverride           (+0x0C / +0x10) (group 11)
              ├─ arMountedClothOverrides[4]                       (+0x14)
              ├─ snoEffectGroup                                   (+0x24) (group 14, e.g. eyeGlow)
              └─ arVariantMaterials[]                             (+0x38)

SubObject  (in Structure.ptChunks[].ptLODs[].ptSubObjects[])
  └─ nMaterialIndex : int32  (+0x60)  — index into the parent
                                       AppearanceDefinition.ptAppearanceMaterials[]
```

`SubObject.nMaterialIndex` is the 0-based slot index that the parser already
extracts. To pick a material:

1. `slot = ptAppearanceMaterials[nMaterialIndex]`
2. inside `slot.ptSOAs[]`, pick the entry whose `dwPersona` matches the active
   character persona — for our purposes (no persona system), `ptSOAs[0]` is
   always correct in the three sampled models (every `dwPersona` is `0`).
3. `material_sno = slot.ptSOAs[0].snoMaterial` resolves to a `MaterialDefinition`
   in `base/meta/Material/<name>.mat`.

`SubObjectAppearance` is hash `2088474641` (size 72 bytes). `AppearanceMaterial`
is hash `4040942623` (size 32). Both are `complex` polymorphic-free types.

### 1.1 Sample model material rosters

| Model                    | nMat | snoMaterial(s)                                                                                                 | Notes |
|--------------------------|------|----------------------------------------------------------------------------------------------------------------|-------|
| `Goatman_BossTrophy`     | 3    | 1265518 `Goatman_BossTrophy_Body_mat`<br>931889 `Goatman_brute_extras_mat_Fractured Peaks`<br>1265214 `Goatman_BossTrophy_fur_mat` | one persona variant per slot; `snoEffectGroup=null` |
| `barF_H07` (body armor)  | 1    | 989187 `rogF_H07_mat` (×2 SOA entries — `dwFlags=32769` and `32768`) | both ptSOAs point at the same material; flag bit `0x8000` differs (likely persona/look mask). The model is a barbarian armor piece using a **rogue** material — confirmed in d4data |
| `necF_stor229_HLM` (helm) | 4   | 2158846 `barM_stor235_rays_mat`<br>2161939 `NecM_stor229_crown_weak_mat`<br>2161921 `barM_stor235_ring_bright_loop_twoSided_mat`<br>2058641 `NecM_stor229_HLM_mat` | last SOA (`dwFlags=32771`) carries `snoEffectGroup → bar_stor235_eyeGlow` (group 14) — runtime FX, not a texture |

The 5-submesh structure of `necF_stor229_HLM` (per `skeleton_format_spec.md`)
maps onto **4** materials, so two submeshes share a material — `nMaterialIndex`
is per-submesh while `ptAppearanceMaterials` is a deduplicated palette.

---

## 2. `MaterialDefinition` (size 568, hash 3110834328, snoGroup 57)

The on-disk material file. All texture and PBR data lives inside `tUberMaterial`;
the surrounding fields control destruction/decay FX and physics surface, not
shading.

| Offset | Type                | Field                           | Notes                                                |
|--------|---------------------|----------------------------------|------------------------------------------------------|
| 0x08   | uint32              | `dwFlags`                       |                                                      |
| 0x10   | `UberMaterial` (56) | **`tUberMaterial`**             | Shader, texture list, named scalar/vector params     |
| 0x48   | array               | `arDecalLookVariantMap`         | Per-look-variant decal overrides                     |
| 0x58   | DT_SNO (group 43)   | `snoSurface`                    | Physics/footstep surface (e.g. `Stone.srf`)          |
| 0x60   | array<BiomeSetting>[4] | `arBiomeSettings`             | Per-biome dampening curves                           |
| 0x160  | `ParametricClutterIndex` | `tParametricClutterIndex`  | Clutter scattering (decal materials)                 |
| 0x178  | uint32              | `unk_93ea20a`                   | Often `30`; possibly destruction-distance threshold  |
| 0x180+ | `InterpolationPath_float` ×2 | `pathPlaybackMultiplier` / `pathPlaybackController` | Animated material params |
| 0x1F0+ | floats              | `flDuration`, `tOutroDuration`, `flStiffness`, `flGravityMagnitude`, … | Decal/destruction lifetime + cloth-style dynamics |
| 0x214  | `FxAnimatedInfluences` | `tFxAnimatedInfluences`      |                                                      |

For the export pipeline only `tUberMaterial` matters.

### 2.1 `UberMaterial` (size 56, hash 377055229)

| Offset | Type                          | Field                    | Notes                                       |
|--------|-------------------------------|--------------------------|---------------------------------------------|
| 0x00   | DT_SNO (group 36)             | `snoShaderMap`           | ShaderMap variant (e.g. `scene_def_gbuff_basic`) — picks the pixel shader. Affects which `eShaderTex` slots are sampled. |
| 0x04   | DT_SNO (group 85)             | `snoMaterialValueSetOverride` | Optional MaterialValueSet override     |
| 0x08   | `Material` (4 B)              | `mat`                    | `nSortPri` only — render-sort priority      |
| 0x10   | array<`MaterialTextureEntry`> | **`ptMatTexList`**       | Per-shader-slot texture references          |
| 0x20   | array<`RunTimeMaterialValues`>| **`ptRunTimeMaterialValues`** | Named scalar/vector shader inputs       |
| 0x30   | uint32                        | `nTexAnimStateCount`     |                                             |
| 0x34   | bool (1 bit)                  | `fHasGraph`              |                                             |

### 2.2 `MaterialTextureEntry` (size 48, hash 552516018)

| Offset | Type                | Field         | Notes                                                  |
|--------|---------------------|---------------|--------------------------------------------------------|
| 0x00   | int32 (DT_ENUM)     | **`eShaderTex`** | Slot enum — see §3.2 for the inferred mapping       |
| 0x08   | `MaterialTexture`   | `tMatTex`     |                                                        |

### 2.3 `MaterialTexture` (size 40, hash 3890408608)

| Offset | Type              | Field                | Notes                                       |
|--------|-------------------|----------------------|---------------------------------------------|
| 0x00   | DT_SNO (group 44) | **`snoTex`**         | The TextureDefinition this slot binds to    |
| 0x08   | array<`TexAnim`>  | `ptTexAnim`          | UV scale/scroll animation; usually one entry with `flUScale=1, flVScale=1` |
| 0x20   | int32             | `nTexAnimStateIndex` | `-1` for static                             |
| 0x24   | bool              | `unk_e1a4010`        |                                             |

`snoTex` may be `null` — these are placeholder slots that the shader may consume
procedurally (slot 96 in the Goatman body material is null but `slot 97` has
`flUScale=11, flVScale=17`, suggesting a tiled noise lookup baked into the
shader rather than a sampled texture).

### 2.4 `RunTimeMaterialValues` (named PBR inputs, hash 1300029571)

Three parallel arrays carrying shader-uniform overrides keyed by a
`MaterialValue` SNO (group 81 → `base/meta/MaterialValue/<name>.mtv`). Each
entry stores a value plus an optional `nPathValGraphIndex` for animated
curves.

| Sub-array                      | Element type                           | Carries                                  |
|--------------------------------|----------------------------------------|------------------------------------------|
| `arMaterialScalarValues`       | `RunTimeMaterialScalarValueEntry`      | `RunTimeMaterialValue_float` (`value: f32`) |
| `arMaterialScalarGraphs`       | (same shape, animated)                 |                                          |
| `arMaterialVectorValues`       | `RunTimeMaterialVectorValueEntry`      | `RunTimeMaterialValue_bcVec4` (`value: {x,y,z,w}: f32`) |
| `arMaterialVectorGraphs`       | (same shape, animated)                 |                                          |

In `Goatman_BossTrophy_Body_mat` the runtime values include:

| MaterialValue name           | Type | Value          | Likely meaning                       |
|------------------------------|------|----------------|--------------------------------------|
| `Color Intensity`            | f32  | 0              | Base albedo multiplier               |
| `emissive multiplier`        | f32  | 10             | Emissive boost                       |
| `emissive color lerp`        | f32  | 0              | Emissive blend                       |
| `Color Hitflash Power`       | f32  | 5              | Hit-flash intensity                  |
| `Color Hitflash Enabled`     | f32  | 1              | Hit-flash on/off                     |
| `Translucency Alpha Levels Hero` | f32 | -0.06        | SSS thickness offset                 |
| `Translucency Intensity`     | f32  | 1              | SSS scale                            |
| `Emissive Color Source`      | f32  | 0              | Emissive source select               |
| `Emissive Texture Multiplier`| f32  | 1              | Emissive texture multiplier          |
| `emissive color`             | vec4 | (1,1,1,1)      | Emissive RGB                         |
| `Translucency Color Hero`    | vec4 | (0.096, 0.020, 0.0055, 1) | SSS color (skin red)        |

These are the closest analogues to glTF PBR factors:

- `emissive color` × `emissive multiplier` × `Emissive Texture Multiplier`
  → glTF `emissiveFactor` (with HDR clamping)
- No direct `baseColorFactor`, `metallicFactor`, or `roughnessFactor` —
  metallic and roughness are **always texture-driven** (slots 62/63), not
  scalars. `Color Intensity` is the closest scalar but it gates the texture,
  not the base albedo.

The MaterialValue SNO names resolve via `__targetFileName__` in the JSON dump,
so we already have human-readable identifiers for every parameter without
extra hash cracking.

---

## 3. `eShaderTex` — texture-slot enum

This is a `DT_ENUM` whose value-to-name table is **not** present in
`d4data/definitions.json` (only the field name `eShaderTex` itself is in
`!!D4FieldChecksums.yml`, hash `0x2bae5ad`). The mapping below was inferred by
matching slot integers to the suffixes of the texture names they point at,
across the 5 sampled materials.

### 3.1 Confirmed slot/name pairs (12 textures, 5 materials)

| Slot | Goatman BossTrophy body | Goatman fur / extras | NecM helm                          | rogF body | barM rays |
|------|--------------------------|----------------------|-------------------------------------|-----------|-----------|
| 1    | `Frac_color`            | (slot used)          | `HLM_Color`                         | —         | —         |
| 3    | `Frac_normal`           | —                    | `HLM_Normal`                        | `Normal`  | —         |
| 19   | —                        | —                    | —                                   | `Color`   | —         |
| 54   | —                        | —                    | `HLM_DyeMask`                       | —         | —         |
| 56   | —                        | —                    | `HLM_DyeRamp`                       | —         | —         |
| 62   | `Frac_rough`            | —                    | `HLM_Rough`                         | —         | —         |
| 63   | `Body_Metal`            | —                    | `HLM_Metal`                         | —         | —         |
| 81   | `Frac_ao`               | —                    | `HLM_AO`                            | (null)    | —         |
| 86   | `Body_Emissive`         | —                    | `HLM_Emissive`                      | —         | —         |
| 88   | —                        | —                    | —                                   | (null)    | —         |
| 96   | (null, procedural)      | —                    | `HLM_FurMask`                       | `Mask`    | —         |
| 97   | (null, procedural, UV 11×17) | —              | —                                   | `Hair_noise` | —      |
| 104  | `Body_Translucency`     | —                    | —                                   | —         | —         |
| 108  | —                        | —                    | `HLM_DyeMask` (second binding)       | —         | —         |
| 145  | `Body_skinMask`         | —                    | —                                   | —         | —         |
| 212  | —                        | —                    | `Metal_Iron_Scratched_DM_Normal`   | —         | —         |
| 213  | —                        | —                    | `Fabric_cotton_blend_shirt_DM_Normal`| —         | —         |
| 214  | —                        | —                    | `Leather_fine_grain_rough_DM_Normal`| —         | —         |
| 218  | —                        | —                    | `Metal_Iron_Scratched_DM_Rough`    | —         | —         |
| 219  | —                        | —                    | `Fabric_cotton_blend_shirt_DM_Rough`| —         | —         |
| 220  | —                        | —                    | `Leather_fine_grain_rough_DM_Rough`| —         | —         |

### 3.2 Inferred enum (HIGH for confirmed slots; rest are conjectures)

| Slot | Mapped role                 | glTF target          | Notes                                       |
|------|------------------------------|----------------------|---------------------------------------------|
| 1    | `BASE_COLOR` (creature/world)| `pbrMetallicRoughness.baseColorTexture` | sRGB |
| 3    | `NORMAL`                     | `normalTexture`      | tangent-space; format 42 BC5_UNORM XY        |
| 19   | `BASE_COLOR` (character)     | `pbrMetallicRoughness.baseColorTexture` | dye-pipeline character albedo |
| 54   | `DYE_MASK`                   | extras / aux         | mask selecting which channels accept dye    |
| 56   | `DYE_RAMP`                   | extras / aux         | LUT for dye colors                          |
| 62   | `ROUGHNESS`                  | metallicRoughness `g`| single-channel BC4                          |
| 63   | `METALLIC`                   | metallicRoughness `b`| single-channel BC4                          |
| 81   | `AMBIENT_OCCLUSION`          | `occlusionTexture`   | single-channel BC4                          |
| 86   | `EMISSIVE`                   | `emissiveTexture`    | sRGB                                        |
| 88   | `?` (rogF only, null)        | —                    | likely a hair / character-specific mask    |
| 96   | `MASK_PRIMARY` (fur/skin/hair) | extras            | Goatman: leaves null and uses a procedural mask via `flUScale/V` ≠ 1; NecM helm: `FurMask`; rogF: a generic `Mask` |
| 97   | `NOISE_PROCEDURAL` (hair)    | extras               | Goatman: `flUScale=11, flVScale=17`; rogF: `Hair_noise.tex` |
| 104  | `TRANSLUCENCY` / SSS         | `KHR_materials_subsurface` (extension) | Goatman skin |
| 108  | `DYE_MASK_2`                 | extras               | second dye-mask binding (helm reuses HLM_DyeMask) |
| 145  | `SKIN_MASK`                  | extras               | identifies skin-vs-fur regions for shader   |
| 212–214 | `DM_NORMAL_*`             | extras               | "Detail Material" tile-normals (iron/cotton/leather) — overlay normals blended on top of slot 3 |
| 218–220 | `DM_ROUGH_*`              | extras               | matching tile roughness                    |

Strong observations:

- **Two distinct albedo conventions exist.** Creatures and props use **slot 1**;
  player armor uses **slot 19** (so the dye system at slots 54/56/108 can
  recolor it). A glTF exporter must accept either as the `baseColorTexture`,
  preferring whichever is non-null.
- **Slots 62/63/81 are always single-channel BC4-style textures.** Packing
  them into glTF's metallicRoughness convention (which expects a 3-channel
  texture with R=AO, G=roughness, B=metallic) requires either combining the
  three at extract time or emitting them as separate `KHR_materials_*` extras.
- **Slots 212–220 (DM_*) are tiling detail textures shared across many
  materials.** They never appear with the `<modelname>_…` prefix — they pull
  from a generic library. A texture cache keyed by slot+SNO would dedupe them.

### 3.3 What we still don't have

- The integer values for slots 1 vs 3 vs 19 etc. are **not** documented in
  d4data; they must come from the game executable. If the eventual shader
  binary or a header from D4Analyzer becomes available, the table above
  should be cross-checked. For now we treat the empirical inference as the
  enum.
- Slots between the values we've seen (e.g. 4–18, 20–53) almost certainly
  exist for shader variants we haven't sampled (tessellation, hair, water,
  cloth, etc.). Building a full enum requires scanning more `.mat.json`
  files — `d4data/json/base/meta/Material/` has 100,003 entries and a
  one-pass extractor would land all of them.

---

## 4. `TextureDefinition` (size 120, hash 3631735738, snoGroup 44)

| Offset | Type                | Field                  | Notes                                              |
|--------|---------------------|------------------------|----------------------------------------------------|
| 0x08   | DT_SNO (group 153)  | `sUIStylePreset`       | UI-only; null on game textures                     |
| 0x0C   | int32 (DT_ENUM)     | **`eTexFormat`**       | Internal pixel-format enum (see §4.3)              |
| 0x10   | uint16              | `dwVolumeXSlices`      | 1 for 2D, >1 for atlas/volume                     |
| 0x12   | uint16              | `dwVolumeYSlices`      | 1 for 2D                                          |
| 0x14   | uint16              | **`dwWidth`**          | mip 0 width                                       |
| 0x16   | uint16              | **`dwHeight`**         | mip 0 height                                      |
| 0x18   | uint32              | `dwDepth`              | 1 for 2D                                          |
| 0x1C   | uint8               | `dwFaceCount`          | 6 for cubemap, 1 otherwise                       |
| 0x1D   | uint8               | `dwMipMapLevelMin`     |                                                   |
| 0x1E   | uint8               | `dwMipMapLevelMax`     | usually `9` (2048→1)                              |
| 0x20   | uint32              | `dwImportFlags`        |                                                   |
| 0x24   | int32 (DT_ENUM)     | `eTextureResourceType` |                                                   |
| 0x28   | RGBA float          | **`rgbavalAvgColor`**  | Average linear-RGBA — usable as fallback `baseColorFactor` when payload is missing |
| 0x38   | vec2                | `pHotspot`             | Atlas hotspot                                     |
| 0x40   | array<`SerializeData`>(16) | **`serTex`**     | Mip serialization records — see §4.2              |
| 0x50   | array<`TexFrame`>   | `ptFrame`              | Atlas frame UV ranges                             |
| 0x60   | array<`GCoeffs`>    | `ptGCoeffs`            | Spherical-harmonic coefficients (sky/IBL only)    |
| 0x70   | uint64              | `ptPostprocessed`      | Always streamed; runtime-only handle              |

Texture file layout: `MaterialTexture.snoTex.__targetFileName__` resolves to
`base/meta/Texture/<name>.tex`; the actual pixel bytes are in the **paired
payload** at `base/payload/Texture/<name>.tex` (group 44 has
`snoGroupHasPayload=true`).

### 4.1 `serTex` mip table (each entry 8 B)

| Offset | Type   | Field             |
|--------|--------|-------------------|
| 0x00   | uint32 | `dwOffset`        |
| 0x04   | uint32 | `dwSizeAndFlags`  |

The first entry (`dwOffset=0`, `dwSizeAndFlags = mip0_size`) is the **highest
detail mip stored separately** (streamed-from-disk top mip); the remaining
entries are the descending mip pyramid packed into the `.tex` payload. For
`Goatman_Brute_Body_Frac_color` (1024 × 1024, 9 mips):

| Idx | dwOffset | dwSizeAndFlags | Inferred mip                        |
|-----|----------|----------------|-------------------------------------|
| 0   | 0        | 524 288        | 1024×1024 streamed                  |
| 1   | 0        | 131 072        | 512×512                             |
| 2   | 131 072  | 32 768         | 256×256                             |
| 3   | 163 840  | 8 192          | 128×128                             |
| 4   | 172 032  | 4 096          | 64×64                               |
| 5   | 176 128  | 2 048          | 32×32                               |
| 6   | 178 176  | 1 024          | 16×16                               |
| 7   | 179 200  | 512            | 8×8                                 |
| 8   | 179 712  | 256            | 4×4                                 |

So mips 1-8 live in a packed payload of 179 968 B, and mip 0 (524 288 B) is
in a separate stream. The 4× ratio between consecutive entries matches a
0.5 byte-per-pixel BC1/BC4 codec.

### 4.2 Bytes-per-pixel ratios across slot types (Goatman body)

| Texture                       | eTexFormat | Resolution | Mip 0 bytes | bpp  | Likely codec       |
|-------------------------------|------------|------------|-------------|------|--------------------|
| `Frac_color`                  | 46         | 1024²       | 524 288     | 0.5  | BC1 / BC7-low      |
| `Frac_normal`                 | 42         | 1024²       | 1 048 576   | 1.0  | BC5 (XY normal)    |
| `Frac_rough`                  | 41         | 1024²       | 524 288     | 0.5  | BC4 (single)       |
| `Body_Metal`                  | 41         | 512²        | 131 072     | 0.5  | BC4 (single)       |
| `Body_Emissive`               | 46         | 256²        | 32 768      | 0.5  | BC1 (sRGB)         |
| `Frac_ao`                     | 41         | 128²        | 8 192       | 0.5  | BC4 (single)       |
| `Body_Translucency`           | 46         | 512²        | 131 072     | 0.5  | BC1 (sRGB)         |
| `Body_skinMask`               | 9          | 2048²       | 2 097 152   | 0.5  | BC4 / BC1          |

### 4.3 `eTexFormat` enum (inferred)

| Value | Inferred DXGI                  | Used for             |
|-------|--------------------------------|----------------------|
| 9     | `BC4_UNORM` (single channel)   | masks (skin/hair)    |
| 41    | `BC4_UNORM`                    | rough / metal / AO   |
| 42    | `BC5_UNORM`                    | tangent-space normals |
| 46    | `BC1_UNORM_SRGB` or `BC7_UNORM_SRGB` | albedo, emissive, translucency |

This enum is **not** the standard DXGI numbering (where BC1=71, BC4=80, BC5=83,
BC7=98). It is a D4-internal table. Without the executable we cannot
distinguish BC1 from BC7 directly — but the bpp ratio (0.5) is the same for
both, and the visual result of decoding either is the same color image. A
robust parser would attempt BC7 first and fall back to BC1 if the magic
bytes do not validate.

### 4.4 Implications

- **All texture pixel data lives in CASC payload files we do not have access
  to in this repo.** d4data ships meta only.
- The `.tex` payload begins with the BC-compressed mip pyramid in the order
  given by `serTex[1..]`; the streamed top mip (`serTex[0]`) is in a separate
  CASC entry — likely the same path with a streaming suffix, but this is
  unverified.
- Even without payloads we have:
  - the texture's resolution and mip count (`dwWidth/dwHeight/dwMipMapLevelMax`)
  - its inferred codec (`eTexFormat`)
  - its average linear-RGBA color (`rgbavalAvgColor`)
- For glTF export this is enough to populate a placeholder PBR material
  whose tint and slot wiring are correct, leaving the actual image bytes for
  a Phase 7-gated CASC step.

---

## 5. What we can do without CASC extraction

The current exporter writes empty `Material_<n>` slots. Without touching the
binary `.mat`/`.tex` payloads we can already produce **named, factor-tinted,
texture-slot-documented** materials by reading only the d4data JSON.

| Capability                                                      | Achievable? | Source                                                               |
|-----------------------------------------------------------------|-------------|----------------------------------------------------------------------|
| Name materials from the SNO data                                | ✅          | `ptAppearanceMaterials[].ptSOAs[0].snoMaterial.name`                 |
| Name material slots in glTF (`Material_<n>` → `<material_name>`) | ✅         | same                                                                 |
| Set `pbrMetallicRoughness.baseColorFactor`                      | ✅          | `Texture[slot=1 or 19].rgbavalAvgColor`                              |
| Set `emissiveFactor`                                            | ⚠ partial   | needs `emissive color` × `emissive multiplier` × `Emissive Texture Multiplier` from `ptRunTimeMaterialValues`; correct in linear space. HDR clamp to glTF's [0..1] expected. |
| Set `metallicFactor` / `roughnessFactor`                        | ❌          | always texture-driven; no scalar exists. Default to 1.0/1.0.         |
| Document required textures per material                         | ✅          | `ptMatTexList[].tMatTex.snoTex.__targetFileName__`                   |
| Embed actual texture images                                     | ❌          | requires CASC `base/payload/Texture/<name>.tex`                      |
| Identify texture format/resolution                              | ✅          | `TextureDefinition.eTexFormat / dwWidth / dwHeight`                  |
| Sidecar JSON listing slot → expected file path for a manual import | ✅       | combine all of the above                                             |

### 5.1 Recommended exporter changes (Phase 5)

1. **Parser side** (`material_parser.py`, new module):
   - Walk `AppearanceDefinition.ptAppearanceMaterials[].ptSOAs[0]` and
     resolve each `snoMaterial` to a `MaterialDefinition` dict (read the
     `.mat.json` directly while we are JSON-only; later read the binary
     `.mat` from CASC).
   - For each material, collect `(eShaderTex, snoTex.__targetFileName__,
     snoTex.__snoID__)` triples from `tUberMaterial.ptMatTexList`.
   - For each texture, read `TextureDefinition.rgbavalAvgColor`,
     `dwWidth`, `dwHeight`, `eTexFormat`.
   - Pull `emissive color`, `emissive multiplier`,
     `Translucency Color Hero`, etc. from `ptRunTimeMaterialValues` if
     present.

2. **MeshData** carries a new `materials: list[Material]` where
   `Material` is:
   ```python
   @dataclass
   class TextureRef:
       slot:    int            # raw eShaderTex
       role:    str            # mapped name from §3.2 ("BASE_COLOR", …)
       sno_id:  int
       path:    str            # base/meta/Texture/<name>.tex
       width:   int
       height:  int
       format:  int            # eTexFormat
       avg_rgba: tuple[float, float, float, float]

   @dataclass
   class Material:
       name:           str         # MaterialDefinition file stem
       sno_id:         int
       shader_map:     str         # "scene_def_gbuff_basic"
       textures:       list[TextureRef]
       base_color_factor: tuple[float, float, float, float]  # from BASE_COLOR avg_rgba (else 1,1,1,1)
       emissive_factor:   tuple[float, float, float]
       metallic_factor:   float = 1.0
       roughness_factor:  float = 1.0
   ```

3. **Exporter side** (`gltf_export.py`): for each submesh, look up
   `mesh.materials[submesh.material_index]` and emit a glTF material with:
   - `name = mat.name`
   - `pbrMetallicRoughness.baseColorFactor = mat.base_color_factor`
   - `pbrMetallicRoughness.metallicFactor` / `roughnessFactor` from
     `mat.metallic_factor` / `roughness_factor`
   - `emissiveFactor = mat.emissive_factor`
   - `extras = {"d4_textures": [{role, slot, sno, path, width, height, format} for t in mat.textures]}`
4. Optionally write a sidecar `<model>.materials.json` next to the `.glb`
   listing the same data so a Blender-side importer can resolve textures
   once CASC extraction is wired up in Phase 7.

This gets us to "models import named, tinted, with full texture inventory in
extras" without solving TACT decryption. When Phase 7 unlocks raw `.tex`
extraction the same code path drops in actual images.

---

## 6. Confidence summary

| Claim                                                                           | Confidence |
|---------------------------------------------------------------------------------|------------|
| Reference chain `SubObject.nMaterialIndex → AppearanceMaterial → SubObjectAppearance.snoMaterial → MaterialDefinition` | HIGH (RTTI + 3 sample models) |
| `tUberMaterial.ptMatTexList[]` is the texture list                              | HIGH       |
| `MaterialTextureEntry.eShaderTex` is the slot identifier                        | HIGH       |
| Slot mapping in §3.2 (1=color, 3=normal, 62=rough, 63=metal, 81=AO, 86=emissive) | HIGH (5 materials × multiple slots × consistent suffix naming) |
| Slot 19 = character albedo, slots 54/56/108 = dye system                        | MEDIUM (1 material per pair sampled) |
| Slots 96/97 are procedural / fur / hair                                         | MEDIUM     |
| Slots 212–220 are detail-material overlays                                      | MEDIUM     |
| `eTexFormat` 41/42/46/9 inferred codecs                                         | MEDIUM (bpp ratios match BC1/4/5; exact BC7 vs BC1 for fmt 46 unverified) |
| `rgbavalAvgColor` is the linear-RGBA average                                    | HIGH       |
| `ptRunTimeMaterialValues` carries shader-uniform overrides keyed by MaterialValue SNO | HIGH |
| Texture pixel data is in `base/payload/Texture/<name>.tex`, not in d4data JSON  | HIGH       |
| MaterialDefinition dwFlags/snoSurface/biome fields are not needed for export    | HIGH       |
| Exact integer eShaderTex enum table                                             | LOW (no source of truth located) |
| Whether `serTex[0]` lives in a sibling CASC entry vs. inside the `.tex` payload  | LOW        |

---

## 7. Reference materials

- `d4data/definitions.json` — RTTI dump used for every struct in this spec
  (hashes 2214406937, 4040942623, 2088474641, 3110834328, 377055229,
  552516018, 3890408608, 3631735738, 1352167279).
- `d4data/json/base/meta/Appearance/{barF_H07,Goatman_BossTrophy,necF_stor229_HLM}.app.json`
  — sample appearances providing the `ptAppearanceMaterials` rosters.
- `d4data/json/base/meta/Material/*.mat.json` — 5 sampled material files
  driving the slot-enum inference.
- `d4data/json/base/meta/Texture/*.tex.json` — 12 sampled texture metadata
  files driving §4.
- `d4data/definitions/!!D4FieldChecksums.yml` — verified that
  `eShaderTex`/`eShaderTexOverride` are the only field names with that hash;
  the enum **values** are not present in d4data.
- `d4data/DirectX/bc*.cpp` — DirectXTex BC encoder/decoder reference, ships
  in the d4data tree presumably to convert payloads at build time.
- `docs/app_format_spec.md` §9 — `SubObject.nMaterialIndex` is at offset 0x60
  in the SubObject record and is already extracted by the parser.
