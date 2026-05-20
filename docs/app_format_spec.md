# Diablo IV `.app` Model Format Specification

**Status:** DRAFT -- Awaiting review  
**Confidence levels:** HIGH = verified across 5+ samples, MEDIUM = consistent pattern but interpretation uncertain, LOW = hypothesis from limited data

---

## 1. Overview

Diablo IV stores 3D model geometry in `.app` (Appearance) files, split into two
parts within the CASC archive:

| Component | CASC path                          | Purpose                        |
|-----------|------------------------------------|--------------------------------|
| **Meta**  | `base/meta/Appearance/<name>.app`  | Structure, layout, materials   |
| **Payload** | `base/payload/Appearance/<name>.app` | Raw geometry buffers       |

Meta and payload files are paired by a **link ID** stored in both
(`meta[0x10] == payload[0x00]`).

### Format lineage

The format descends from Blizzard's engine used in Diablo Immortal (DI) and
shares structural elements with the DI `.mesh` format (MESSIAH magic, vertex
attribute descriptors, half-float UVs). Key similarities confirmed by Durik256's
`AppToOBJ.exe` (.NET) tool which exports D4 `.app` to OBJ and uses:
`readApp`, `FindSubMesh`, `CalcStride`, `ReadHalfFloat`, `attrib`.

All multi-byte values are **little-endian**.

---

## 2. Meta File Format

### 2.1 Meta Header (confidence: HIGH)

| Offset | Size | Type   | Field           | Notes                                |
|--------|------|--------|-----------------|--------------------------------------|
| 0x00   | 4    | uint32 | `magic`         | Always `0xDEADBEEF`                 |
| 0x04   | 12   | -      | (unknown)       | Constant across samples              |
| 0x10   | 4    | uint32 | `link_id`       | Matches `payload[0x00]`             |
| 0x14   | 44   | -      | (unknown)       |                                      |
| 0x40   | 4    | uint32 | `struct_table_offset` | Always 312 (`0x138`)          |
| 0x44   | 4    | uint32 | `struct_entry_size`   | Always 88 (`0x58`)            |
| 0x48   | 24   | -      | (unknown)       |                                      |
| 0x60   | 4    | uint32 | `config_block_offset` | Offset to 416-byte config block; 0 if absent |
| 0x64   | 4    | uint32 | `config_block_size`   | Always 416 (`0x1A0`) when present   |
| 0x68   | 4    | uint32 | `unknown_68`    | Varies; possibly related to vertex/index format flags |
| ...    |      |        |                 |                                      |
| 0xB8   | 4    | uint32 | `meta_data_size`| Total size of variable data after fixed header |
| 0xBC   | 4    | uint32 | `lod_or_submesh_count` | Values seen: 4, 8, 16 (powers of 2 common) |

### 2.2 Struct Table (confidence: MEDIUM)

Located at `meta[0x40]` = offset `0x138`, with entries of size `meta[0x44]` = 88 bytes.

The number of entries = `(meta_file_size - struct_table_offset) / entry_size`, but
this includes ALL data in the meta file — the struct table may not extend to EOF.
Entry count seems to be derived from `meta[0xBC]` or `meta[0xB8]`.

Each 88-byte entry appears to encode a **heterogeneous record set** — the entries
do NOT all have the same schema. Based on analysis, entries follow a repeating
**group pattern** of ~7-10 entries per group:

| Entry in group | Content (hypothesis)                                  |
|---------------|-------------------------------------------------------|
| 0             | Bounding box (6 floats at fields 4-9), plus size/offset pairs at fields 12-13, 16-17, 20-21 |
| 1-3           | LOD level descriptors with increasing float values (detail distances?) and (offset, size) pairs |
| 4             | Sentinel entry (many `0xFFFFFFFF` values), hash/ID    |
| 5             | Bounding extents (6 floats at fields 0-4) + format info (field 12-13, 18-19) |
| 6             | Flags entry (field 4 = `0x200000`, field 5 = `0x7FFF0000`) |
| 7+            | Per-submesh or per-draw-call descriptors              |

**Consistent fields across all entry-0s (all models):**
- `field[12]` = 400 (constant)
- `field[13]` = 200 (constant)  
- `field[17]` = 160 (constant)
- `field[21]` = 48 (constant)

These constants likely define fixed buffer configuration values rather than per-model data.

### 2.3 Config Block (confidence: MEDIUM)

A 416-byte block at offset `meta[0x60]`. Not present for all models (Goatman
has offset=0). When present, contains mesh-level configuration:

| Offset | Size | Type   | Field                  | Notes                          |
|--------|------|--------|------------------------|--------------------------------|
| 0x00   | 4    | uint32 | `index_buffer_offset`  | Byte offset of index buffer in payload (confirmed for simple models) OR float32 bounding box X (for skinned models) |
| 0x04   | 4    | uint32 | `index_buffer_size`    | Byte size of index buffer OR bounding box Y |
| 0x08   | 4    | uint32 | `sentinel`             | `0xFFFFFFFF` or `0x00000000`   |
| 0x0C   | 4    | uint32 | `flags`                | 0 or 1                         |
| 0x10   | 8    | -      | `hash_or_id`           | Model-specific hash value      |
| 0x20   | 4    | uint32 | `vertex_header_size`   | E.g., 224 (`0xE0`); size of per-vertex format descriptor |
| 0x24   | 4    | uint32 | `geometry_data_size`   | Relates to `payload[0x0C]` (typically `payload[0x0C] - vertex_header_size`) |
| 0x28   | 4    | uint32 | `submesh_count`        | Number of submeshes in this LOD (only present for complex models) |
| ...    |      |        |                        |                                |
| 0xB4   | 4    | float  | `constant_angle`       | Always `0.174533` = 10 degrees in radians |
| 0xE0   | 4    | float  | `uv_scale_u`           | Always `0.5`                   |
| 0xE4   | 4    | float  | `uv_scale_v`           | Always `0.5`                   |
| 0x100  | 4    | uint32 | `sentinel_2`           | `0xFFFFFFFF` when present      |
| 0x150  | 4    | uint32 | `secondary_table_offset` | Offset within meta for additional data |
| 0x154  | 4    | uint32 | `secondary_table_entry_size` | Size per entry           |

**Key observation:** For models without submesh groups (`payload[0x08] == 0`),
the config block's first two fields directly give the index buffer location:
- `config[0x00]` = index buffer byte offset in payload
- `config[0x04]` = index buffer byte size
- `config[0x00] + config[0x04]` = payload file size (verified for offHandsSorc)

For models with submesh groups, the interpretation differs (may be bounding box
or aggregate counts).

---

## 3. Payload File Format

### 3.1 Payload Header (confidence: HIGH)

| Offset | Size | Type   | Field                | Notes                          |
|--------|------|--------|----------------------|--------------------------------|
| 0x00   | 4    | uint32 | `link_id`            | Matches `meta[0x10]`          |
| 0x04   | 4    | uint32 | `reserved`           | Always 0                       |
| 0x08   | 4    | uint32 | `group_count`        | Number of submesh groups / LOD levels (0 = single mesh) |
| 0x0C   | 4    | uint32 | `header_data_size`   | End offset of header/descriptor region; vertex data follows |

When `group_count > 0`, additional descriptor data follows at offset 0x10:

### 3.2 Group Descriptor Table (confidence: LOW)

*Only present when `group_count > 0` (e.g., Goatman with 4 groups).*

The region from offset 0x10 to `header_data_size` contains per-group descriptors
including buffer offset/size pairs. For Goatman (4 groups):

Located at approximately offset 0x58 in the payload, with 16-byte entries:

| Offset in entry | Size | Type   | Field         |
|-----------------|------|--------|---------------|
| 0x00            | 4    | uint32 | `buffer_offset` |
| 0x04            | 4    | uint32 | `buffer_size`   |
| 0x08            | 8    | -      | (unknown)       |

For Goatman, three contiguous buffers were found:
- Buffer 0: offset 0xA0, size 5280 — **pure float32 position data** (440 positions, stride=12)
- Buffer 1: offset 0x1540, size 7296 — **structured records** (uint32 fields, submesh descriptors)
- Buffer 2: offset 0x31C0, size 4960 — **bounding box data** (stride=32, 6 floats + 2 uint32 per entry)

Total: 5280 + 7296 + 4960 = 17536 bytes. These end at offset `header_data_size` (17696 = 0x4520).

### 3.3 Vertex Buffer (confidence: HIGH for stride=36 format)

Vertex data follows the header/descriptor region. The primary vertex format is
**interleaved with stride 36 bytes**:

| Byte offset | Size | Format      | Attribute       | Notes                          |
|-------------|------|-------------|-----------------|--------------------------------|
| 0           | 12   | 3 x float32 | **Position**   | XYZ world-space coordinates    |
| 12          | 4    | 4 x uint8   | **Normal**     | Packed signed normalized (decode: `byte/127.5 - 1.0`) |
| 16          | 4    | 4 x uint8   | **Vertex Color** | RGBA; alpha typically 0xFF   |
| 20          | 4    | 4 x uint8   | **Vertex Color 2** | Secondary color/AO; alpha 0xFF |
| 24          | 4    | 2 x uint16  | **UV / Bone**  | Packed half-float UVs or bone indices (model-dependent) |
| 28          | 4    | -           | **Padding/Extra** | Zeros observed, possibly unused |
| 32          | 4    | 4 x uint8   | **Tangent**    | Packed signed normalized, similar to normal encoding |

**Stride 36 confirmed for:** Goatman (100% valid in 1289 vertices), offHandsSorc (stride=36 pattern, 2414 vertices).

**Sample vertex (Goatman, v0):**
```
Position: (-0.1575, 0.0938, 0.4715)
Normal:   packed [87 11 DE 00] -> (0.059, -0.867, 0.741, -1.0)
VColor1:  [00 00 00 FF] (black, full alpha)
VColor2:  [00 00 00 FF]
Bone/UV:  [1B 39 08 39] (half-float: varies)
Padding:  [00 00 00 00]
Tangent:  [E9 96 BD 7F]
```

**Alternative formats (confidence: LOW):** Azmodan appeared to use stride=52 in
the cross-validation but the vertex data interpretation was ambiguous. Larger
models with bone weights may use extended vertex formats (additional 16 bytes
for bone indices + weights).

### 3.4 Index Buffer (confidence: HIGH)

Immediately follows the vertex buffer. Format depends on max vertex count:

| Condition          | Index format | Bytes per index |
|-------------------|--------------|-----------------|
| vertex_count <= 65535 | uint16      | 2               |
| vertex_count > 65535  | uint32      | 4               |

Winding order: **back-face** (clockwise when viewed from front). The DI Noesis
plugin uses `RPGOPT_TRIWINDBACKWARD`, and the same likely applies here.

Triangle list topology (every 3 indices form one triangle).

**Index buffer location:**
- For simple models (`group_count == 0`): `meta_config_block[0x00]` = offset, `meta_config_block[0x04]` = size
- For grouped models: index data follows vertex data; boundaries determined by submesh descriptors

### 3.5 Payload Layout Summary

**Simple model (e.g., offHandsSorc, `group_count == 0`):**
```
[Header: 32 bytes]
[Vertex data: N_verts x 36 bytes, stride=36 interleaved]
[Possibly additional vertex streams]
[Index data: N_indices x 2 bytes, uint16]
[Optional: padding to alignment]
```

**Grouped model (e.g., Goatman, `group_count > 0`):**
```
[Header: 16 bytes]
[Group descriptor table: variable]
[Pre-computed data buffers (positions, bounding boxes, submesh records)]
  -- ends at payload[0x0C] offset --
[Vertex buffer(s): stride=36 interleaved]
[Index buffer(s): uint16 triangle list]
[Optional: padding/alignment zeros]
```

---

## 4. Cross-Reference Summary

| Field              | Meta location       | Payload location | Verified |
|--------------------|---------------------|------------------|----------|
| Link ID            | `meta[0x10]`        | `payload[0x00]`  | YES (all samples) |
| Magic              | `meta[0x00]` = 0xDEADBEEF | -          | YES      |
| Group count        | `meta[0xBC]` (related) | `payload[0x08]` | Partial  |
| Config block       | `meta[0x60]`+`meta[0x64]` | -          | YES      |
| Index buf offset   | `config[0x00]`      | actual offset    | YES (simple models) |
| Index buf size     | `config[0x04]`      | actual size      | YES (simple models) |
| Struct table       | `meta[0x40]`+`meta[0x44]` | -          | YES      |

---

## 5. Verified Sample Data

| Model                | Payload    | Meta   | Groups | Verts   | Indices | VStride | IFmt   |
|----------------------|------------|--------|--------|---------|---------|---------|--------|
| Goatman_BossTrophy   | 227,616    | 5,328  | 4      | 1,289   | 7,871   | 36      | uint16 |
| offHandsSorc_stor029 | 780,800    | 7,392  | 0      | 2,414   | 19,968  | 36      | uint16 |
| barF_H07             | 2,234,944  | 3,304  | 2      | ~12,648 | varies  | 36(?)   | uint16 |
| necF_stor229_HLM     | 818,960    | 8,648  | 6      | 3,664   | 16,984  | 36(?)   | uint16 |
| Azmodan              | 8,118,736  | 19,696 | 133    | ~4,865+ | varies  | 52(?)   | uint16 |

---

## 6. Open Questions

These items need further investigation before parser implementation:

1. **Multi-stream vertex layout**: For models where total vertex data doesn't
   divide cleanly by stride x vertex_count, there may be multiple interleaved
   vertex attribute streams (like DI's 4 attrib arrays). The exact stream
   boundaries and per-stream strides are unclear.

2. **Bone weights**: Skinned models (character armor, Azmodan) likely have bone
   index + weight data. The format (4x uint8 indices + 4x uint8 weights? Or
   float32 weights?) and location (embedded in the 36-byte stride or in a
   separate stream) is unknown.

3. **LOD structure**: Models with `group_count > 0` contain multiple detail
   levels. The exact LOD selection mechanism and how vertex/index ranges are
   assigned per LOD is not fully decoded.

4. **Submesh material assignment**: How submeshes map to materials/textures.
   The struct table entries likely contain material references but the mapping
   is unclear.

5. **Azmodan's stride=52**: The largest model appears to use a different vertex
   stride. This may include additional bone weights, morph targets, or
   per-vertex lighting data.

6. **The descriptor buffers** in grouped models (Buffer 0/1/2 in Goatman):
   Buffer 0 appears to be pre-computed positions (spatial index?), Buffer 1
   is submesh records, Buffer 2 is bounding boxes. These may be needed for
   correct submesh segmentation.

---

## 7. Proposed Parser Implementation Strategy

Given the open questions, the parser should be implemented in stages:

### Stage 1: Simple models (high confidence)
- Parse meta header and extract link_id, config block
- Parse payload for models with `group_count == 0`
- Read vertex buffer (stride=36) and index buffer (uint16) from config block offsets
- Output single-mesh glTF with positions, normals, and UVs

### Stage 2: Grouped models (medium confidence)
- Parse group descriptor table
- Handle models with `group_count > 0` by locating vertex/index data after `header_data_size`
- Support multiple submeshes per model

### Stage 3: Extended vertex formats (low confidence, needs more research)
- Detect and handle stride=52 and bone-weighted vertices
- LOD level selection
- Material/texture mapping

---

## 8. Reference Materials

- **Durik256 Noesis plugin** (`fmt_mesh_diablo.py`): Diablo Immortal `.mesh`
  format parser showing MESSIAH magic, chunk-based structure, vertex attribute
  strings like `"P3F"` (position, 3 components, float32), `"T2H"` (texcoord,
  2 components, half-float), `"N4B"` (normal, 4 components, byte).

- **Durik256 AppToOBJ.exe**: .NET 4.7.2 WinForms tool (2023) for converting
  D4 `.app` to OBJ. Key function names: `readApp`, `readMesh`, `FindSubMesh`,
  `CalcStride`, `ReadHalfFloat`, `tVertex`, `tFacet`, `attrib`, `ConvertMulty`.

- **Analysis scripts**: `tools/archive/analyze_samples.py`,
  `tools/archive/analyze_v2.py` through `tools/archive/analyze_v5.py`
  contain the raw binary analysis that produced this spec.

- **DiabloTools/d4data** (`definitions.json`, `json/base/meta/Appearance/*.app.json`):
  RTTI dump of the Diablo IV data format. Section 9 below cross-references the
  `VertexElem`/`GeoChunkVertexBuffer` definitions against the glTF attribute
  lists produced by the current parser.

---

## 9. Vertex Buffer Layout (cross-referenced with d4data)

The `.app` meta file stores its vertex layout as a `GeoChunkVertexBuffer`
struct (definitions.json hash `3646617580`). Each buffer carries an array of
`VertexElem` (`1536548129`) records that describe the interleaved vertex
attributes. This section was produced by:

1. Parsing all 10 `.gltf` files in `d4extract/exports/` with `pygltflib`
   (script: `tools/analyze_gltf_attribs.py`, raw output:
   `tools/gltf_attribs_report.txt`, JSON: `tools/gltf_attribs.json`).
2. Cross-referencing the layouts against the corresponding
   `GeoChunkVertexBuffer` records in `d4data/json/base/meta/Appearance/*.app.json`
   (sampled across 300 appearance files).

### 9.1 Struct definitions (from `d4data/definitions.json`)

```
VertexElem (hash 1536548129, size 12)
  +0x00 uint32  eSemantic    // DT_ENUM
  +0x04 uint32  eFormat      // DT_ENUM
  +0x08 uint32  nOffset      // byte offset of this attribute within a vertex

GeoChunkVertexBuffer (hash 3646617580, size 80)
  +0x00 uint32           eVBFormat                  // overall buffer format tag
  +0x04 uint32           dwVertStride               // bytes per vertex
  +0x08 array<VertexElem> ptVertexElems             // attribute descriptors
  +0x18 array<uint8>     pnVertexElemPerSemantic   // semantic -> elem-index LUT (255 = unused)
  +0x28 int32            vfid
  +0x30 array<uint8>     ptChunkVertices            // raw vertex bytes (external)
  +0x40 int32            vbid
  +0x44 int32            baid
  +0x48 uint32           unk_4c43adc
  +0x4C bool             fOptional

GeoChunk (hash 3974107604, size 88)
  +0x00 AABB                      aabbBounds
  +0x18 array<...>                ptLODs
  +0x28 array<GeoChunkVertexBuffer> ptChunkVertexBuffers
  +0x38 array<...>                ptChunkIndexBuffers
  +0x48 array<...>                unk_8c8b576

SubObject (hash 4121622049, size 240)
  +0x00 uint32  dwFlags
  +0x04 uint32  unk_334eb2d
  +0x08 array   ptClothData
  +0x18 array   unk_26f39c1
  +0x28 uint32  dwBASegment
  +0x2C uint32  unk_87b4c64
  +0x30 uint32  dwBASize
  +0x34 uint32  unk_4c43adc
  +0x38 NameInfo tNameInfo
  +0x60 int32   nMaterialIndex
  +0x64 uint32  dwSubObjectHash
  +0x68 int32   nVertBufferIndex          // index into ptChunkVertexBuffers
  +0x6C int32   nIndexBufferIndex         // index into ptChunkIndexBuffers
  +0x70 int32   nBaseLODSubObjectIndex
  +0x74 int32   unk_a1b71f5
  +0x78 int32   nSubObjectMaxLOD
  +0x7C uint32  dwShaderMapOverride
  +0x80 SnoRef  snoCampaignVisibilityCondition
  +0x84 AABB    aabbBounds
  +0xA0 array   ptShapes
  +0xB0 array   ptBaseBoneInfluences
  +0xC0 ptr     ptPostprocessed
  +0xC8 array<SubObjectSegment> ptSegments
  +0xD8 vec3    wpFixedPointPosOffset
  +0xE4 vec3    wpFixedPointPosScale

SubObjectSegment (hash 1370930836, size 32)
  +0x00 array<bone_id> pBoneIDs
  +0x10 uint32         nVertCount
  +0x14 uint32         nVertOffset
  +0x18 uint32         nIndexCount
  +0x1C uint32         nIndexOffset
```

### 9.2 Observed `eSemantic` values

Aggregated across 300 appearance files:

| eSemantic | Mapped name (glTF)          | Typical eFormat | Typical offset | Notes                                           |
|-----------|------------------------------|-----------------|----------------|-------------------------------------------------|
| 0         | `POSITION`                   | 1               | 0              | Always first; 12-byte float3                    |
| 1         | `TEXCOORD_0`                 | 7 (also 2)      | 24 / 28        | 4-byte half-float pair (fmt 7) or 4-byte SNORM (fmt 2) |
| 2         | `TEXCOORD_1`                 | 7 (also 2)      | 28 / 32        | Second UV channel; same encoding family as TEXCOORD_0 |
| 3-6       | (unused in samples)          | -               | -              | `pnVertexElemPerSemantic[3..6]` is always 255   |
| 7         | `COLOR_0` / vertex color    | 5               | 16 / 20        | 4-byte UNORM (RGBA)                             |
| 8         | `COLOR_1` / 2nd vertex color | 5               | 20 / 24        | 4-byte UNORM (often AO/mask channel)            |
| 9         | `NORMAL`                     | 8               | 12             | Always offset 12; 4-byte packed normal          |
| 10        | `TANGENT`                    | 8               | 16 / 32 / 40   | 4-byte packed tangent (skinned moves it earlier) |
| 11        | `JOINTS_0` (`BLENDINDICES`) | 4               | 36             | Skinned-only; 4-byte UINT8 quad                 |
| 12        | `WEIGHTS_0` (`BLENDWEIGHTS`)| 5               | 40             | Skinned-only; 4-byte UNORM quad                 |

`pnVertexElemPerSemantic` is an 11-element (simple) or 13-element (skinned)
LUT mapping eSemantic -> the index into `ptVertexElems`, or 255 when that
semantic is absent. The two known LUT shapes are:

```
simple   (stride 36): [0, 4, 5, 255, 255, 255, 255, 2, 3, 1, 6]
skinned  (stride 44): [0, 5, 6, 255, 255, 255, 255, 3, 4, 1, 2, 7, 8]
```

The reserved `attrib_count` constants in `app_parser.py`
(`{11: stride 36, 13: stride 44}`) correspond directly to the length of this
LUT, not to the number of `VertexElem` entries (which is 7 and 9 respectively).

### 9.3 Observed `eFormat` values

Inferred by combining the byte-size implied by adjacent `nOffset` values with
the glTF accessor types written by the current parser:

| eFormat | Bytes | DXGI-equivalent name        | Used by semantics | Notes                                      |
|---------|-------|------------------------------|-------------------|--------------------------------------------|
| 1       | 12    | `R32G32B32_FLOAT`            | 0 (POSITION)       | float3                                     |
| 2       | 4     | `R16G16_SNORM` (likely)      | 1, 2 (TEXCOORD)    | Alternate compressed UV encoding           |
| 4       | 4     | `R8G8B8A8_UINT`              | 11 (BLENDINDICES)  | Bone indices, byte4                        |
| 5       | 4     | `R8G8B8A8_UNORM`             | 7, 8, 12           | Vertex colors and bone weights             |
| 7       | 4     | `R16G16_FLOAT`               | 1, 2 (TEXCOORD)    | UV as half-float pair (`<2e` in parser)    |
| 8       | 4     | `R8G8B8A8_SNORM` / packed dec | 9, 10 (NORMAL/TANGENT) | Decoded by `_unpack_normal()` in app_parser.py |

Formats 0, 3, 6, and 9+ were not observed in the 300-file sample.

### 9.4 Concrete layouts

**Stride 36 (`eVBFormat=4`, simple/static)** -- 513/621 sampled buffers:

| Index | eSemantic | eFormat | nOffset | Meaning                  |
|-------|-----------|---------|---------|--------------------------|
| 0     | 0         | 1       | 0       | POSITION (float3)         |
| 1     | 9         | 8       | 12      | NORMAL (packed4)          |
| 2     | 7         | 5       | 16      | COLOR_0 (UNORM4)          |
| 3     | 8         | 5       | 20      | COLOR_1 (UNORM4)          |
| 4     | 1         | 7       | 24      | TEXCOORD_0 (half2)        |
| 5     | 2         | 7       | 28      | TEXCOORD_1 (half2)        |
| 6     | 10        | 8       | 32      | TANGENT (packed4)         |

**Stride 44 (`eVBFormat=6`, skinned)** -- 106/621 sampled buffers:

| Index | eSemantic | eFormat | nOffset | Meaning                  |
|-------|-----------|---------|---------|--------------------------|
| 0     | 0         | 1       | 0       | POSITION (float3)         |
| 1     | 9         | 8       | 12      | NORMAL (packed4)          |
| 2     | 10        | 8       | 16      | TANGENT (packed4)         |
| 3     | 7         | 5       | 20      | COLOR_0 (UNORM4)          |
| 4     | 8         | 5       | 24      | COLOR_1 (UNORM4)          |
| 5     | 1         | 7       | 28      | TEXCOORD_0 (half2)        |
| 6     | 2         | 7       | 32      | TEXCOORD_1 (half2)        |
| 7     | 11        | 4       | 36      | JOINTS_0 (uint8x4)        |
| 8     | 12        | 5       | 40      | WEIGHTS_0 (UNORM4)        |

A rare third combo (`stride=44, eVBFormat=27`, 2/621 files) reuses the
simple 7-element layout but with stride 44 -- likely 8 trailing bytes of
padding or a deprecated encoding.

### 9.5 Cross-model glTF comparison (April 2026 export)

Stats from `tools/analyze_gltf_attribs.py` over the 10 files in
`d4extract/exports/`:

| File                                          | Meshes | Primitives | Stride | Attribute set                  |
|-----------------------------------------------|--------|------------|--------|--------------------------------|
| `axe_stor052`                                 | 1      | 3          | 32     | POSITION, NORMAL, TEXCOORD_0   |
| `barbarian_upheaval_payload_meshemitter`     | 1      | 1          | 32     | POSITION, NORMAL, TEXCOORD_0   |
| `BarLair_twoHandSword_base03`                 | 1      | 3          | 32     | POSITION, NORMAL, TEXCOORD_0   |
| `Character_Select_Warlock_World`              | 4      | 8          | 32     | POSITION, NORMAL, TEXCOORD_0   |
| `Cultist_Prop_Warlock_Altar_01`               | 1      | 2          | 32     | POSITION, NORMAL, TEXCOORD_0   |
| `Goatman_BossTrophy`                          | 1      | 3          | 32     | POSITION, NORMAL, TEXCOORD_0   |
| `necF_stor229_HLM`                            | 1      | 5          | 32     | POSITION, NORMAL, TEXCOORD_0   |
| `npcF_drys_barbarian_01_HED`                  | 1      | 2          | 32     | POSITION, NORMAL, TEXCOORD_0   |
| `npcF_drys_barbarian_03_TRS`                  | 1      | 2          | 32     | POSITION, NORMAL, TEXCOORD_0   |
| `sword_uniq05`                                | 1      | 3          | 32     | POSITION, NORMAL, TEXCOORD_0   |

Each primitive uses identical attribute types and component sizes:

| Attribute   | glTF type | Component   | Bytes/vertex |
|-------------|-----------|-------------|--------------|
| POSITION    | VEC3      | FLOAT       | 12           |
| NORMAL      | VEC3      | FLOAT       | 12           |
| TEXCOORD_0  | VEC2      | FLOAT       | 8            |
| **Total**   |           |             | **32**       |

**Differences across models:** none. All ten exports advertise the same
three-attribute layout regardless of whether the source `.app` is static
(stride 36) or skinned (stride 44). This is an exporter limitation rather
than a source-format property -- semantics 7, 8, 10, 11, 12 (vertex colors,
tangent, bone indices/weights) are present in the source meta but are not
emitted by `gltf_export.py`. Skinned models in particular lose all rigging
data on export.

### 9.6 Implications for the parser/exporter

1. The current `app_parser.py` decodes positions, normals, two color
   channels, tangent, and a single UV pair from the simple stride-36
   layout, but only POSITION, NORMAL, and TEXCOORD_0 reach the glTF.
   Wiring `colors`, `tangents`, and the second UV through to
   `gltf_export.py` would lose nothing on the parse side.
2. The skinned layout (stride 44) carries `JOINTS_0` (eSemantic 11,
   eFormat 4) at offset 36 and `WEIGHTS_0` (eSemantic 12, eFormat 5) at
   offset 40. These are not currently decoded; adding them would unlock
   skeletal export for character/armor models.
3. The `pnVertexElemPerSemantic` LUT length (11 vs 13) is a more reliable
   stride discriminator than the current `attrib_count` heuristic and can
   be read directly from the meta when the `GeoChunkVertexBuffer` struct
   is located.
