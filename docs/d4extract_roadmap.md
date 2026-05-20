# d4extract — Project Roadmap

> Diablo IV model extraction pipeline: CASC → .app → glTF → Blender
> Last updated: 2026-05-01

## Project goal

Build a Python CLI tool that extracts Diablo IV 3D models from the game's
CASC archive, parses the proprietary `.app` format, and exports to glTF 2.0
for import into Blender. Includes a TUI file browser for navigating game assets.

## Key resources

| Resource | What it gives us |
|----------|-----------------|
| [DiabloTools/d4data](https://github.com/DiabloTools/d4data) | `definitions.json` — complete binary struct layouts for all SNO types including AppearanceDefinition, GeoChunkVertexBuffer, VertexElem, SubObject, etc. |
| [DiabloTools/Diablo4Tools-Releases](https://github.com/DiabloTools/Diablo4Tools-Releases) | D4Analyzer — closed-source but exports ground truth glTF/glb files |
| [Dakota628/d4parse](https://github.com/Dakota628/d4parse) | Go-based SNO meta parser, generates code from d4data definitions |
| [HoldMyBeer-gg/rustydemon](https://github.com/HoldMyBeer-gg/rustydemon) | Rust CASC reader, supports both Steam and Battle.net D4 installs |
| [ladislav-zezula/CascLib](https://github.com/ladislav-zezula/CascLib) | Reference C CASC implementation with TACT encryption support |
| [Durik256/Noesis-Plugins](https://github.com/Durik256/Noesis-Plugins) | AppToOBJ.exe (closed-source) + MeshFinder tool (open source) |

## What we know so far

### The .app format (from d4data definitions.json)

The format is fully documented as binary struct definitions. Key structs:

- **AppearanceDefinition** — top-level, contains Looks, Materials, confirms meta/payload split (`snoGroupHasPayload=true`)
- **GeoChunk** (88 bytes) — mesh chunk with bounding box, LODs, vertex buffer array, index buffer array
- **GeoChunkVertexBuffer** (80 bytes) — `eVBFormat` (format enum), `dwVertStride` (stride in bytes), `ptVertexElems` (array of VertexElem), `ptChunkVertices` (raw vertex data), `fOptional` flag
- **VertexElem** (12 bytes) — `eSemantic` (enum: position/normal/UV/etc), `eFormat` (enum: float32/float16/etc), `nOffset` (byte offset within stride)
- **GeoChunkIndexBuffer** (24 bytes) — `pdwChunkIndices`, `ibid`, `fOptional`
- **SubObject** (240 bytes) — links materials to geometry via `nMaterialIndex`, `nVertBufferIndex`, `nIndexBufferIndex`, and contains `ptSegments`
- **SubObjectSegment** (32 bytes) — per-draw-call ranges: `nVertCount`, `nVertOffset`, `nIndexCount`, `nIndexOffset`, `bone_palette`

### Vertex format variants (verified across 300 .app.json files via d4data)

**Stride 36 (eVBFormat=4, static meshes) — 513/621 sampled buffers:**

| Index | eSemantic | eFormat | Offset | Attribute |
|-------|-----------|---------|--------|-----------|
| 0 | 0 | 1 (float3, 12B) | 0 | POSITION |
| 1 | 9 | 8 (packed4, 4B) | 12 | NORMAL |
| 2 | 7 | 5 (unorm4, 4B) | 16 | COLOR_0 |
| 3 | 8 | 5 (unorm4, 4B) | 20 | COLOR_1 |
| 4 | 1 | 7 (half2, 4B) | 24 | TEXCOORD_0 |
| 5 | 2 | 7 (half2, 4B) | 28 | TEXCOORD_1 |
| 6 | 10 | 8 (packed4, 4B) | 32 | TANGENT |

**Stride 44 (eVBFormat=6, skinned meshes) — 106/621 sampled buffers:**

| Index | eSemantic | eFormat | Offset | Attribute |
|-------|-----------|---------|--------|-----------|
| 0 | 0 | 1 (float3, 12B) | 0 | POSITION |
| 1 | 9 | 8 (packed4, 4B) | 12 | NORMAL |
| 2 | 10 | 8 (packed4, 4B) | 16 | TANGENT <- moves earlier than stride 36! |
| 3 | 7 | 5 (unorm4, 4B) | 20 | COLOR_0 |
| 4 | 8 | 5 (unorm4, 4B) | 24 | COLOR_1 |
| 5 | 1 | 7 (half2, 4B) | 28 | TEXCOORD_0 |
| 6 | 2 | 7 (half2, 4B) | 32 | TEXCOORD_1 |
| 7 | 11 | 4 (uint8x4, 4B) | 36 | JOINTS_0 |
| 8 | 12 | 5 (unorm4, 4B) | 40 | WEIGHTS_0 |

**eSemantic enum** (verified): 0=POSITION, 1=TEXCOORD_0, 2=TEXCOORD_1,
3-6=unused, 7=COLOR_0, 8=COLOR_1, 9=NORMAL, 10=TANGENT, 11=JOINTS_0, 12=WEIGHTS_0

**eFormat enum** (verified): 1=R32G32B32_FLOAT(12B), 2=R16G16_SNORM(4B),
4=R8G8B8A8_UINT(4B), 5=R8G8B8A8_UNORM(4B), 7=R16G16_FLOAT(4B),
8=packed_normal/tangent(4B)

**Note:** Stride 52 does NOT exist — early hex analysis misidentified multiple
vertex buffers (optional + non-optional) as a single wide stream. There is a
rare eVBFormat=27 (2/621 files) with stride 44 but only 7 elements (padding).

### TACT encryption

D4's CASC archive uses TACT (Salsa20) encryption. D4Analyzer shows 183 total
keys with only 4 decrypted (for game version 3.0.1.71696).

**Key finding (from D4 modding community):** TACT keys only encrypt
**unreleased content and cosmetics** — pre-release data for future updates.
Base game models (monsters, NPCs, weapons, released armor sets, bosses)
are NOT encrypted and should be extractable without TACT keys. This means:
- rustydemon may already be able to extract most `.app` files directly
- D4Analyzer's crashes on expansion content are likely due to missing keys
  for NEW unreleased cosmetics, not for the expansion's base content
- TACT key resolution (Phase 6) is a "completeness" task, not a blocker

### Actor → Animation chain (from D4 modding community, confidence: MEDIUM)

The `ActorDefinition` SNO type is believed to be the hub that connects
appearance, skeleton, and animation. Source: D4 modding Discord —
**not yet verified against d4data definitions.json:**

```
ActorDefinition (unverified struct layout)
  ├── snoAppearance                    → .app file (geometry + skeleton)
  ├── arCustomizationAppearances[]     → alternate appearance variants
  ├── arAppearanceSets[]               → appearance set collections
  ├── arAnimSets[]                     → AnimSet SNOs (animation clips)
  ├── arStoreAnimSets[]                → store/MTX animation overrides
  ├── snoAnimTree                      → AnimTree SNO (state machine)
  └── tAnimTreeOverride                → per-actor animation overrides
```

The `.app` file does NOT reference animations directly. You need the
ActorDefinition to find which AnimSet files drive a given model.
Phase 8C will verify this against definitions.json.

### Skeleton system (from Phase 4A research)

The skeleton is **embedded inside each `.app` file**, NOT in a separate SNO.
It lives at `Structure.ptBoneData[0].ptBoneStructure` — an array of
BoneStructure records (232 bytes each) stored in the payload file.

**Skeleton templates:** Equipment pieces for the same character class share
a skeleton template, identified by `BoneData.unk_a3acec8`:

| Template ID | Class | Base bones | Cloth bones | Total |
|-------------|-------|-----------|-------------|-------|
| 188232014 | Barbarian | 190 | 0 | 190 |
| 3340713596 | Necromancer | 192 | 106 | 298 |
| 3008026787 | Azmodan (boss) | 95 | 149 | 244 |

Each file embeds a FULL COPY — a helmet carries 298 bones even though only
1 is weighted. The parser should prune to active bones (weighted + parent
chains) for export. Models with the same template ID have compatible bone
indices, enabling multi-piece outfit assembly.

### Current state

- Phases 1–6 complete. 5/5 test models parse and export correctly.

---

## Phase 1 — Extraction pipeline ✅

**Status: Complete**

- Project scaffolding with click CLI, rich output
- rustydemon-cli wrapper for CASC extraction
- Sample .app file extraction
- Stub modules for future phases

---

## Phase 2 — Format analysis + parser ✅

**Status: Complete — 5/5 test models parse correctly**

**Goal:** Parse .app meta + payload file pairs into a structured `MeshData` object
with correct vertices, normals, UVs, indices, and submesh boundaries.

### 2A. Format specification (use d4data) ✅

**Status: Complete.** Section 9 of `docs/app_format_spec.md` contains the
authoritative struct definitions and verified enum mappings from d4data
cross-referenced across 300 appearance files.

### 2B. Vertex format discovery (use ground truth glTFs) ✅

**Status: Complete.** glTF analysis of 10 D4Analyzer exports confirmed
all attributes. The d4data cross-reference mapped all eSemantic and eFormat
enum values. See Section 9 of `docs/app_format_spec.md`.

**Important finding:** The current `gltf_export.py` only emits POSITION +
NORMAL + TEXCOORD_0 (stride 32 in glTF). The source `.app` files contain
7 attributes (stride 36) or 9 attributes (stride 44) — vertex colors,
tangent, second UV, and bone data are parsed but not exported.

### 2C. Parser implementation ✅

**Status: Complete — 5/5 models correct**

Claude Code prompt:

```
ultrathink. Rewrite src/d4extract/formats/app_parser.py using the
AUTHORITATIVE format spec in docs/app_format_spec.md.

IMPORTANT: docs/app_format_spec.md has two layers of information.
Sections 2-3 are from early hex analysis and contain errors.
Section 9 is from d4data definitions.json cross-referenced across
300 appearance files and is AUTHORITATIVE. When anything in
Sections 2-3 conflicts with Section 9, Section 9 wins.

Specifically:
- Use the vertex layouts from Section 9.4, NOT Section 3.3
- Stride 52 does not exist; ignore all references to it
- The two formats are stride 36 (eVBFormat=4) and stride 44 (eVBFormat=6)
- The attribute ORDER differs between stride 36 and stride 44:
  stride 36: POS, NORM, COLOR0, COLOR1, UV0, UV1, TAN
  stride 44: POS, NORM, TAN, COLOR0, COLOR1, UV0, UV1, JOINTS, WEIGHTS
  (tangent moves from offset 32 to offset 16 in skinned meshes!)
- Read the VertexElem array from the meta file to get the layout
  dynamically — don't hardcode attribute offsets
- The attrib_count constants (11/13) are the length of
  pnVertexElemPerSemantic, NOT the number of VertexElem entries
  (which is 7 and 9 respectively)

Unpack vertices according to the eFormat values from Section 9.3:
  - eFormat 1: float32x3 (12B) — positions, read as-is
  - eFormat 7: float16x2 (4B) — UVs, convert half-float → float32
  - eFormat 8: packed4 (4B) — normals/tangents, decode via _unpack_normal()
  - eFormat 5: unorm4 (4B) — vertex colors/bone weights, normalize to [0,1]
  - eFormat 4: uint8x4 (4B) — bone indices, read as-is
  - eFormat 2: snorm16x2 (4B) — alternate UV encoding, convert to float32

Parse meta file to extract:
  - GeoChunkVertexBuffer descriptors (format, stride, vertex element layout)
  - SubObject → material/buffer index mappings
  - SubObjectSegment draw call ranges (nVertCount, nVertOffset,
    nIndexCount, nIndexOffset)

Read payload file vertex + index data at the specified offsets.

Return a MeshData dataclass with all mesh data including vertex colors,
tangents, second UV channel, and bone data (for stride 44).

CRITICAL KNOWN ISSUES to fix:
1. Normals are packed as 4x uint8 (eFormat 8), NOT float32x3.
   Decode with: (byte / 127.5) - 1.0 or equivalent SNORM unpack.
2. UVs are half-float pairs (eFormat 7), NOT float32x2.
   Convert float16 → float32.
3. Multi-submesh models need per-submesh vertex/index offsets
   from SubObjectSegment (nVertOffset, nIndexOffset, nVertCount, nIndexCount)
4. Coordinate system: D4 uses left-handed Z-up;
   glTF uses right-handed Y-up. Apply axis conversion.
5. The attribute order CHANGES between Format 4 and Format 6 —
   do not assume the same offsets for both formats.
```

### 2D. Validation ✅

All 5 test models validated against D4Analyzer ground truth exports.
Vertex counts, index counts, normal directions, and UV mappings all match.

### Acceptance criteria

- [x] eSemantic enum values fully mapped (0=POS through 12=WEIGHTS)
- [x] eFormat enum values fully mapped (1,2,4,5,7,8)
- [x] Vertex format catalog documented (stride 36 and stride 44 layouts)
- [x] Parser reads format/stride dynamically from meta file (not hardcoded)
- [x] Parser uses VertexElem array from meta to determine attribute layout
- [x] All 5 test models parse without crashing
- [x] Vertex counts match ground truth glTFs
- [x] Normals render correctly (no inside-out faces, no black shading)
- [x] UVs are correct (textures from ground truth can be applied)
- [x] Multi-submesh models have correct submesh boundaries
- [x] Coordinate system matches Blender conventions (Y-up, right-handed)

---

## Phase 3 — Static mesh glTF export ✅

**Status: Complete**

**Goal:** Export parsed MeshData to glTF 2.0 (.glb) that opens correctly in Blender.

### Claude Code prompt

```
ultrathink. Rewrite src/d4extract/export/gltf_export.py to export
all attributes that app_parser.py now provides.

The parser already handles ALL data conversion (packed normals →
float3, half-float UVs → float2, unorm colors → float4, etc).
The exporter just needs to wire the MeshData fields through to
glTF accessors. Do NOT re-decode or re-convert anything.

MeshData fields available from the parser:
  .positions    — list of (x, y, z) float32 tuples
  .normals      — list of (nx, ny, nz, nw) float32 tuples (4 components; exporter uses first 3)
  .uvs          — list of (u, v) float32 tuples (TEXCOORD_0)
  .uvs_1        — list of (u, v) float32 tuples (TEXCOORD_1), may be empty
  .tangents     — list of (tx, ty, tz, tw) float32 tuples, may be empty
  .colors       — list of (r, g, b, a) float32 tuples (COLOR_0), may be empty
  .colors_1     — list of (r, g, b, a) float32 tuples (COLOR_1), may be empty
  .joints       — list of (j0, j1, j2, j3) uint8 tuples, may be empty
  .weights      — list of (w0, w1, w2, w3) float32 tuples, may be empty
  .indices      — flat list of uint32 index values
  .submeshes    — list of Submesh with:
      .vertex_offset, .vertex_count, .index_offset, .index_count,
      .material_index (int, indexes into ptAppearanceMaterials)
  .layout       — VertexLayout with stride and element descriptors

Export rules:
- Each Submesh becomes a separate glTF primitive
- Use submesh.material_index to create material slots. For now,
  name them "Material_{index}" — Phase 5 will resolve SNO names.
- POSITION and NORMAL: always export (float32x3)
- TEXCOORD_0: always export (float32x2)
- TEXCOORD_1: export if uvs_1 is non-empty (float32x2)
- TANGENT: export if tangents is non-empty. glTF requires float32x4
  with w component for handedness — append w=1.0 to each tangent.
  Replace zero-length tangent vectors (0,0,0,w) with default (1,0,0,1)
  for Khronos validator compliance.
- COLOR_0: export if colors is non-empty (float32x4).
  Note: suppressed in Phase 5C when textures are embedded (D4
  vertex colors are blend/AO masks, not PBR tints).
- Cloth mesh filtering: skip cloth-only submeshes by default
  (include_cloth=False). A submesh is "cloth-only" when its
  ptAppearanceMaterials entry has snoMaterial=null and only snoCloth
  set — visible only to D4's physics simulation. Without filtering
  they appear as flat-white overlapping geometry.
- JOINTS_0 and WEIGHTS_0: skip for now. glTF requires a skin node
  with joint hierarchy to validate skinned meshes, which is Phase 4.
  Just confirm the data is present in MeshData and log it.
- Index buffer: uint16 if max vertex index ≤ 65535 (0xFFFF), else uint32
- Apply convert_to_gltf_coords() before writing — the parser
  preserves D4-native coordinates (left-handed Z-up), the exporter
  converts to glTF (right-handed Y-up)

Per-submesh vertex/index slicing:
  For each submesh, slice vertices[vo:vo+vc] and indices[io:io+ic]
  where vo=vertex_offset, vc=vertex_count, io=index_offset, ic=index_count.
  Rebase indices so they start from 0 within each primitive.

CLI commands (update existing if they exist):
  d4extract export <meta.app> <payload.app> [-o output.glb]
  d4extract export-batch <directory> [-o output_dir/] [--csv summary.csv]

Validate: export all five test models and confirm vertex/index counts
per primitive match the d4data ground truth. Print a summary table
showing per-primitive vertex count, index count, material_index, and
attribute presence (has tangents, has colors, etc).
```

**After Claude Code finishes:** Open each exported .glb in Blender
(File → Import → glTF 2.0) and visually confirm models look correct —
upright orientation, smooth normals, no garbled geometry. Apply ground
truth textures from D4Analyzer exports to check UV correctness.

### Acceptance criteria

- [x] `d4extract export` produces a .glb that passes Khronos glTF validator
- [x] Models open in Blender and display recognizable 3D shapes
- [x] Submeshes appear as separate primitives with correct material slots
- [x] Vertex/index counts per primitive match d4data ground truth
- [x] Normals look correct (smooth shading, no artifacts)
- [x] UVs are correct (ground truth textures from D4Analyzer can be applied)
- [x] Tangents are exported with w=1.0 (enables normal mapping in Blender)
- [x] Vertex colors are exported (COLOR_0 visible in Blender vertex paint mode)
- [x] TEXCOORD_1 exported when present in source data
- [x] Coordinate system is correct (model stands upright in Blender, not on its side)
- [x] JOINTS_0/WEIGHTS_0 presence logged but not exported (deferred to Phase 4)
- [x] Batch export processes all test models without crashing

---

## Phase 4 — Skeleton + skinning ✅

**Status: Complete**

**Goal:** Add bone hierarchy and skin weights to exported models so they
can be posed and animated in Blender.

### 4A. Research — find the skeleton SNO type ✅

**Status: Complete.** Key finding: the skeleton is NOT in a separate SNO
file. It is embedded inside the same `.app` file at
`Structure.ptBoneData[0].ptBoneStructure` — an array of BoneStructure
records (232 bytes each). See `docs/skeleton_format_spec.md` for full details.
The 4A prompt has already been executed — skip directly to 4B.

### 4B. Implementation — skeleton parser + skinned export ✅

**Status: Complete.**

Run this after 4A is complete and docs/skeleton_format_spec.md exists:

```
ultrathink. Read docs/skeleton_format_spec.md, docs/app_format_spec.md,
and d4extract_roadmap.md for context.

KEY FINDING FROM 4A: The skeleton is EMBEDDED inside the same .app
file as the geometry. It lives at Structure.ptBoneData[0].ptBoneStructure
— an array of BoneStructure records (232 bytes each). There is NO
separate skeleton SNO file.

STEP 1: Locate the bone array in the binary.

The meta files are too small to hold the bone data (barF_H07 meta is
3,304 bytes but its bone array is 44,080 bytes). The bones must be in
the payload file. Use the same known-plaintext approach that worked
for vertex/index buffers:

From d4data JSON ground truth:
  barF_H07: ptBoneStructure dataOffset=224, dataSize=44080, bones=190
  necF_stor229_HLM: ptBoneStructure dataOffset=224, dataSize=69136, bones=298

Each BoneStructure is 232 bytes. Key searchable fields:
  +0x28: nParentIndex (int16) — root bones have 0xFFFF
  +0x44: transform (BoneTransform, 40 bytes starting with quaternion)
  +0x6C: transformInv (BoneTransform)
  +0x94: transformParentRel (BoneTransform)
  +0xBC: transformSkinningInv (BoneTransform)

Search the payload files for the root bone signature: a 232-byte
aligned record where offset +0x28 contains 0xFFFF (int16 = -1,
marking a root bone). The first bone in the array should be a root.

Verify: read 190 consecutive BoneStructure records from barF_H07's
payload at the discovered offset. Check that:
  - nParentIndex values form a valid tree (every parent < self index)
  - BoneTransform quaternions have magnitude ~1.0
  - dwHash values are non-zero 32-bit hashes

STEP 2: Build the skeleton parser.

Add to src/d4extract/formats/app_parser.py (or a new skeleton_parser.py):

Dataclasses:
  BoneTransform: q (x,y,z,w float32), wp (x,y,z float32), scale (x,y,z float32)
  Bone: hash (uint32), parent_index (int16), local_trs (BoneTransform),
        inv_bind_trs (BoneTransform), lod (int16), flags (uint32)
  Skeleton: bones (list[Bone]), base_bone_count (int), cloth_bone_count (int)

Parsing:
  - Read the bone array from the payload at the discovered offset
  - For each 232-byte record, extract:
    +0x20: dwHash (uint32) — bone name hash
    +0x24: dwFlags (uint32)
    +0x28: nParentIndex (int16, -1 = root)
    +0x2A: nLOD (int16)
    +0x94: transformParentRel (BoneTransform) — local pose for glTF nodes
    +0xBC: transformSkinningInv (BoneTransform) — inverse bind matrix for glTF
  - Build parent→children tree from nParentIndex values
  - Populate MeshData.skeleton (or add skeleton field to MeshData)

Bone naming:
  - Try to resolve dwHash via d4data/dict.txt or dict.json if available
  - Fall back to "bone_{dwHash:08x}" format

STEP 3: Update gltf_export.py for skinned export.

When MeshData has a non-empty skeleton:
  - Create one glTF node per bone with:
    translation = bone.local_trs.wp
    rotation = bone.local_trs.q (as [x,y,z,w])
    scale = bone.local_trs.scale
  - Set parent-child relationships from nParentIndex
  - Create a glTF Skin with:
    joints = list of all bone node indices
    inverseBindMatrices = accessor of 4x4 float32 matrices,
      built from each bone's inv_bind_trs as:
      mat = compose_trs(inv_bind_trs.wp, inv_bind_trs.q, inv_bind_trs.scale)
      DO NOT invert again — transformSkinningInv is already inverted

  - Add JOINTS_0 accessor (uint8x4 if max ≤ 255, uint16x4 if > 255)
  - Add WEIGHTS_0 accessor (float32x4)

CRITICAL — bone_palette remapping:
  The JOINTS_0 byte values from the parser are indices into the
  per-segment bone_palette, NOT global bone indices. Before
  writing JOINTS_0 to glTF, remap each byte:

    global_index = submesh.bone_palette[local_joint_byte]

  The parser stores the palette on each Submesh as `bone_palette`.

  If bone_palette is empty, treat the byte as already a global index.

Zero-weight joint masking:
  After remapping, mask out unused JOINTS_0 slots: when
  weights[i] == 0, set joints[i] = 0. Source bytes often hold
  stale palette entries (e.g. weights=(1,0,0,0), joints=(52,8,74,99)).
  Khronos gltf_validator flags every such pair as "Joints accessor
  element at index N is used with zero weight but has non-zero value"
  — tens of thousands of warnings on a typical character mesh.

BONE EXPORT STRATEGY — full skeleton default, optional pruning:
  Each .app file embeds the FULL character skeleton (190-298 bones)
  even if the mesh only uses a handful. A helmet weighted to 1 bone
  still carries 298 bones. This is intentional — all equipment pieces
  for the same character class share the same skeleton template
  (identified by BoneData.unk_a3acec8), so bone indices are globally
  consistent and pieces can be combined under one armature.

  DEFAULT: Export the full skeleton. This is critical for:
  - Multi-piece outfit assembly (helmet + body + boots under one rig)
  - Attachment points (weapon/shield hardpoint bones)
  - Animation playback (clips reference full skeleton indices)
  - Cloth bone constraint chains

  OPTIONAL: --prune-bones flag for lightweight single-piece export.
  When enabled:
  1. Collect all unique bone indices from bone_palette across all
     LOD0 segments — these are the WEIGHTED bones
  2. For each weighted bone, walk nParentIndex up to root (0xFFFF),
     adding every parent — these are the CHAIN bones needed for
     correct transforms
  3. active_set = union of weighted + chain bones
  4. Build a compact bone list from active_set (sorted by original
     index to preserve hierarchy order)
  5. Create a remap table: old_global_index → new_compact_index
  6. When writing JOINTS_0, apply BOTH remaps:
     raw_byte → bone_palette[raw_byte] → remap[global_index]

  In BOTH modes, the JOINTS_0 values still need palette remapping
  via bone_palette — pruning just adds a second remap on top.

  Store the skeleton template ID (BoneData.unk_a3acec8) on MeshData
  so the frontend can identify compatible models for assembly.

Coordinate system:
  Apply the same Z-up-LH → Y-up-RH conversion to bone transforms
  as is applied to vertex positions: (x, y, z) → (x, z, -y) for
  translations, (qx, qy, qz, qw) → (qx, qz, -qy, qw) for quaternions.
  Normalize quaternions after conversion (float32 round-trip can
  push magnitude > 1.0, causing NODE_ROTATION_NON_UNIT validator error).

STEP 4: Validate.

For each skinned test model:
  - Print: total bones, weighted bones, root bone count, max depth
  - Print first 5 bones: name/hash, parent, local translation
  - Export to .glb (full skeleton, default)
  - Confirm Khronos glTF validator passes (skinned mesh mode)
  - Confirm JOINTS_0 count == vertex count
  - Confirm WEIGHTS_0 count == vertex count
  - Confirm inverseBindMatrices count matches bone count
  - Test --prune-bones: confirm reduced active bone count

For static test models (0 bones):
  - Confirm no Skin node is created
  - Export still works as static mesh
```

**After Claude Code finishes:** Import each exported .glb into Blender.
Check that the armature appears with a sensible bone hierarchy. Select
the mesh, enter Weight Paint mode, and verify bone weights paint onto
expected body regions. Try posing a bone to confirm deformation works.

### Acceptance criteria

- [x] Skeleton location identified — embedded in .app, not separate SNO
- [x] Bone array located in payload binary (dataOffset verified)
- [x] skeleton parser reads BoneStructure records (232 bytes each)
- [x] Bone hierarchy reconstructed from nParentIndex values
- [x] Full skeleton exported by default (all bones, original indices)
- [x] --prune-bones option exports only weighted + parent chain bones
- [x] glTF Skin node created with correct joint hierarchy
- [x] inverseBindMatrices from transformSkinningInv (not double-inverted)
- [x] JOINTS_0 remapped through bone_palette (segment-local → global)
- [x] WEIGHTS_0 exported as float32x4
- [x] Bone names resolved from dwHash where possible
- [x] Skeleton template ID (unk_a3acec8) stored on MeshData
- [x] Coordinate system conversion applied to bone transforms
- [x] Skinned test models export and pass glTF validator
- [x] Static test models export as static mesh (no skin node)

---

## Phase 5 — Materials + textures ✅

**Status: Complete**

**Goal:** Export models with PBR material references and optionally
embed/extract texture files.

### How material slots work

The material chain in `.app` files:

```
AppearanceDefinition
  └─ ptAppearanceMaterials[] ← array of SubObjectAppearance
       └─ snoMaterial ← SNO reference → Material definition file
            └─ texture references (diffuse, normal, metallic, roughness, AO)

SubObject
  └─ nMaterialIndex ← index into ptAppearanceMaterials[]
```

Each SubObject (submesh) has `nMaterialIndex` which indexes into the
`ptAppearanceMaterials` array. Each entry in that array is a
`SubObjectAppearance` with `snoMaterial` — a SNO reference pointing to
the actual Material definition file, which in turn references Texture SNOs.

D4Analyzer walks this entire chain when exporting to glTF: it resolves
the material SNOs, extracts textures from CASC, converts `.tex` → PNG,
and embeds them. This is why D4Analyzer exports have correct materials
while our exports currently have empty material slots.

### 5A. Research — material and texture format discovery ✅

**Status: Complete.** Material reference chain fully documented in
`docs/material_format_spec.md`. Key findings: slot enum mapped
(1=base color creature, 19=base color character, 3=normal, 62=roughness,
63=metallic, 81=AO, 86=emissive), no metallic/roughness scalars exist
(always texture-driven), `rgbavalAvgColor` on TextureDefinition provides
fallback base color, texture pixels require CASC payload extraction.

### 5B. Implementation — material names + PBR parameters ✅

**Status: Complete.**

Run in a fresh Claude Code session:

```
ultrathink. Read docs/material_format_spec.md, docs/app_format_spec.md,
and d4extract_roadmap.md for context.

The material format spec (section 5.1) defines exactly what to build.
The parser already extracts submesh.material_index per submesh. This
phase resolves those indices to named, tinted materials with texture
inventories.

STEP 1: Build src/d4extract/formats/material_parser.py.

This module reads material data from d4data JSON files (not binary
.mat files). It needs access to:
- The model's .app.json (for ptAppearanceMaterials)
- The referenced .mat.json files (for tUberMaterial.ptMatTexList)
- The referenced .tex.json files (for rgbavalAvgColor, resolution)

Dataclasses (from material_format_spec.md section 5.1):

  @dataclass
  class TextureRef:
      slot: int              # raw eShaderTex value
      role: str              # "BASE_COLOR", "NORMAL", "ROUGHNESS", etc.
      sno_id: int
      path: str              # base/meta/Texture/<name>.tex
      width: int
      height: int
      format: int            # eTexFormat (9=BC4, 10=BC1, 41=BC4, 42=BC5, 46=BC1, 47=BC1)
      avg_rgba: tuple[float, float, float, float]

  @dataclass
  class Material:
      name: str              # MaterialDefinition file stem
      sno_id: int
      shader_map: str        # e.g. "scene_def_gbuff_basic"
      textures: list[TextureRef]
      base_color_factor: tuple[float, float, float, float]
      emissive_factor: tuple[float, float, float]
      metallic_factor: float = 1.0   # always 1.0 (texture-driven)
      roughness_factor: float = 1.0  # always 1.0 (texture-driven)

Slot to role mapping (from material_format_spec.md section 3.2):
  1=BASE_COLOR(creature), 3=NORMAL, 19=BASE_COLOR(character),
  54=DYE_MASK, 56=DYE_RAMP, 62=ROUGHNESS, 63=METALLIC, 81=AO,
  86=EMISSIVE, 96=MASK_PRIMARY, 97=NOISE_PROCEDURAL,
  104=TRANSLUCENCY, 108=DYE_MASK_2, 145=SKIN_MASK,
  212-214=DETAIL_NORMAL, 218-220=DETAIL_ROUGHNESS

IMPORTANT: Two different albedo slots exist.
  Slot 1 = creatures/world objects. Slot 19 = player character armor.
  Check BOTH and use whichever is non-null as baseColorTexture source.
  The base_color_factor comes from that texture's rgbavalAvgColor.

For emissive_factor: look in ptRunTimeMaterialValues for
"emissive color" (vec4) and "emissive multiplier" (float).
Compute: rgb = emissive_color.rgb * emissive_multiplier.
Clamp to [0, 1] for glTF. Default to (0, 0, 0) if absent.

STEP 2: Wire material data into the parser.

Add a --d4data-path CLI option pointing to d4data/json/ directory.
When provided, material_parser resolves the full chain:
  .app.json -> ptAppearanceMaterials -> .mat.json -> .tex.json

When --d4data-path is absent, materials fall back to "Material_<n>"
with no PBR data (current behavior preserved).

Add to MeshData: materials: list[Material] | None

STEP 3: Update gltf_export.py.

When MeshData.materials is populated:
- Name each glTF material from mat.name
- Set pbrMetallicRoughness.baseColorFactor = mat.base_color_factor
- Set pbrMetallicRoughness.metallicFactor = 1.0
- Set pbrMetallicRoughness.roughnessFactor = 1.0
- Set emissiveFactor = mat.emissive_factor
- Add glTF extras with full texture inventory:
  extras = {"d4_textures": [
    {"role": t.role, "slot": t.slot, "sno_id": t.sno_id,
     "path": t.path, "width": t.width, "height": t.height,
     "format": t.format} for t in mat.textures
  ]}
- Optionally write <model>.materials.json sidecar with same data

When MeshData.materials is None: current "Material_0" behavior.

STEP 4: Validate.
For each test model with --d4data-path=d4data/json/:
- Print table: submesh index, material name, texture count
- Verify material names match d4data ground truth
- Verify baseColorFactor is non-white for materials with avg_rgba
- Verify submesh-to-material assignments are correct
```

### 5C. Texture extraction and embedding (requires CASC access) ✅

**Status: Complete.**

This step depends on being able to extract Texture SNO files from
CASC. Run after Phase 6 confirms base game file extraction works,
OR if D4Analyzer can export the textures separately.

```
ultrathink. Read docs/material_format_spec.md and d4extract_roadmap.md
for context.

Phase 5B already populated MeshData.materials with Material objects.
Each Material has a textures list of TextureRef with:
  .slot    — eShaderTex enum (1, 3, 19, 62, 63, 81, 86, etc.)
  .role    — mapped name ("BASE_COLOR", "NORMAL", "ROUGHNESS", etc.)
  .path    — CASC path like "base/meta/Texture/<name>.tex"
  .format  — eTexFormat (9=BC4, 10=BC1, 41=BC4, 42=BC5, 46=BC1, 47=BC1)
  .avg_rgba — fallback color (already used by 5B for baseColorFactor)

The glTF extras already contain the full texture inventory per
material. This phase adds actual pixel data.

Build src/d4extract/formats/texture_parser.py:
- Read .tex payload files extracted from CASC (at --texture-dir path)
- Wrap raw pixel data in a DDS header based on eTexFormat, then
  decode with Pillow's DDS plugin. This avoids writing manual BC
  block decoders — Pillow handles BC1/BC4/BC5 natively via DDS.
- eTexFormat mapping to DDS fourCC:
  - 9:  BC4 (ATI1) — single channel masks
  - 10: BC1 (DXT1) — dye/luminance maps
  - 41: BC4 (ATI1) — roughness, metallic, AO
  - 42: BC5 (ATI2) — tangent-space normals (two-channel)
  - 46: BC1 (DXT1) — albedo textures
  - 47: BC1 (DXT1) — albedo with punch-through alpha
- For normal maps (eTexFormat 42):
  - D4 uses DirectX convention (Y+ down)
  - Apply DX→GL flip: invert the green channel (G = 255 - G)
  - Reconstruct Z from X and Y: Z = sqrt(1 - X² - Y²)
- For metallic + roughness: glTF expects a single texture with
  R=255(unused), G=roughness, B=metallic. Combine slots 62 and 63
  into one image.
- Convert to PNG using Pillow for embedding in glTF.

Update gltf_export.py:
- When --with-textures is specified and texture files are available:
  - For each material, look up its TextureRef list
  - Convert referenced .tex files to PNG via DDS header wrapping
  - Embed as glTF texture images in the .glb
  - Wire to correct material slot based on TextureRef.role:
    BASE_COLOR → baseColorTexture
    NORMAL → normalTexture (with DX→GL flip applied)
    ROUGHNESS + METALLIC → metallicRoughnessTexture (combined)
    AO → occlusionTexture
    EMISSIVE → emissiveTexture
  - Suppress COLOR_0 on all primitives when textures are embedded
    (D4 vertex colors are blend/AO masks, not PBR tints — they
    multiply incorrectly with base color textures)
- Without --with-textures: current 5B behavior (names + factors only)

Add CLI options:
  --with-textures / --no-textures
  --texture-dir <path> — directory containing extracted .tex files

Validate: export test models with --with-textures and verify:
- PNG images are generated from .tex files without errors
- The .glb contains embedded texture images (check buffer size increase)
- Each material's baseColorTexture, normalTexture, and
  metallicRoughnessTexture point to the correct image indices
- Normal maps have correct DX→GL green channel flip
- Print a summary: material name, texture count, total embedded bytes
```

**After Claude Code finishes 5C:** Open the textured .glb in Blender.
The model should display correctly textured without manual material
assignment.

### Acceptance criteria

- [x] MaterialDefinition struct documented in material_format_spec.md
- [x] Material SNO IDs extracted from .app meta per submesh
- [x] Material slots have descriptive names (not "Material_0")
- [x] PBR parameters set on glTF materials when available
- [x] Texture SNO references documented per material slot
- [x] (5C) Textures extracted from .tex container and converted to PNG
- [x] (5C) Textures embedded in .glb with correct slot assignments
- [x] (5C) Models display correctly textured in Blender


## Phase 6 — TACT key resolution (low priority, parallel track) ✅

**Status: Complete (base game confirmed unencrypted)**

**Goal:** Obtain TACT keys for completeness — enabling extraction of
unreleased cosmetics and store content. Base game models do NOT require
TACT keys and should already be extractable.

**Priority downgraded:** Community sources confirm TACT keys only encrypt
pre-release content and cosmetics (skins, store items). All base game
models — monsters, NPCs, weapons, released armor, bosses, environments —
are unencrypted. This means:
- The full d4extract pipeline should work for most content without keys
- rustydemon may already extract base game `.app` files directly
- D4Analyzer's 4/183 decrypted keys are for cosmetic content only
- **Action item:** Test rustydemon extraction on base game Appearance
  files to confirm they extract without TACT keys

### Investigation paths (if keys are still needed)

1. **Test rustydemon first** — try extracting `.app` files for our test
   models directly. If they extract clean, TACT is not blocking us.
2. **Use existing keys** — ~110 TACT keys are already in `tact_keys` in
   the project. Feed these to rustydemon or CASCLib to decrypt content.
3. **Extract more keys from running game** — rustydemon's
   [tact_scan.py](https://github.com/HoldMyBeer-gg/rustydemon/blob/main/research/d4/tact_scan.py)
   scans a running D4 process's memory for decrypted TACT keys. Run the
   game, run the script with `python tact_scan.py <PID>`, get all keys
   the game has in memory. This is how the community extracts keys.
4. **wowdev wiki** — [TACT/Keys](https://wowdev.wiki/TACT) documents
   D4 product names as `fenrisdev`, `fenris_dev`, `fenrise`, `fenris_event`
5. **Community sources** — the "Not Finding a Cow Level" Discord has
   D4Analyzer developers who maintain key lists. Dakota628 (d4parse /
   diablo.farm) reportedly has a comprehensive set.
6. **wow.tools API** — CascLib downloads keys from
   `https://wow.tools/api.php?type=tactkeys` which may include D4/fenris keys
7. **CASCLib integration** — CASCLib has built-in TACT decryption and
   D4 support. Could replace rustydemon as the extraction layer.

### Success criteria

- [x] Confirmed: base game .app files extract without TACT keys
- [x] Can extract raw .app meta + payload files from CASC for base content
- [ ] Optional: TACT keys obtained for cosmetic/unreleased content
- [x] Documented process for updating keys when D4 patches

---

## Phase 7 — Blender importer addon

**Target: Blender 5.1** (EEVEE Next — Blend Mode / Alpha Blend settings
were removed in 4.0+; use Material.surface_render_method instead).

**Goal:** Build a Blender addon that imports d4extract .glb files with
D4-specific fixes that can't be handled in the glTF itself.

### Background

The glTF exporter produces mathematically correct skeletons — bone
transforms compose correctly, deformation works, and the quaternion
axis conversion is verified. However, Blender displays bone orientations
using its own bone-roll convention, which causes visual issues:

- Some bones appear to point backwards (e.g. right arm toward shoulder
  instead of toward hand) even though they deform correctly
- Blender's bone visualization doesn't match what artists expect from
  a humanoid rig
- glTF has no mechanism to control Blender-specific bone display

A Blender addon can fix this at import time, plus add other D4-specific
quality-of-life features.

### 7A. Core addon — bone orientation fix

```
ultrathink. Read docs/skeleton_format_spec.md and d4extract_roadmap.md
for context.

Build a Blender addon: blender/d4extract_importer.py (or a proper
addon package at blender/d4extract_importer/)

The addon should:

1. Register as a Blender addon with bl_info metadata
   (name="D4Extract Importer", category="Import-Export")

2. Add a menu item: File → Import → Diablo IV Model (.glb)

3. On import:
   a. Use Blender's built-in glTF importer to load the .glb
   b. Post-process the armature to fix bone orientations:
      - For each bone in edit mode, recalculate the bone roll
        so the bone's local Y axis points toward its first child
        (or along the parent's direction for leaf bones)
      - This is Blender's standard "Recalculate Roll → Active Bone"
        operation, automated across the whole skeleton
   c. Optionally rename bones from "bone_<hash>" to readable names
      if a bone name dictionary (data/bone_names.json) is available

4. Add an import options panel with:
   - "Fix bone orientations" checkbox (default: on)
   - "Apply bone names" checkbox (default: on, greyed out if no dict)
   - "Import textures" checkbox (default: on, for Phase 5 materials)

Validate: import skinned test models through the addon. Confirm bone
orientations display correctly (arms pointing toward hands, spine
pointing upward, fingers toward tips). Print a before/after comparison
of bone rolls for the first 10 bones.
```

### 7B. Additional import features (after core works)

```
ultrathink. Read d4extract_roadmap.md for context.

Extend the Blender addon with:

1. Automatic material slot setup:
   - If the .glb has material slots named "Material_0" etc.,
     and a companion .json sidecar exists with texture paths,
     create Blender materials with the correct texture assignments
   - This bridges Phase 5 (materials) with the Blender workflow

2. Skeleton template grouping:
   - If importing multiple .glb files with the same skeleton
     template ID (stored in glTF extras or a sidecar), offer
     to merge them under a single armature
   - This enables importing helmet + body + boots as one character

3. LOD selection:
   - If the .glb contains multiple mesh objects at different LODs,
     add an option to select which LOD to display

4. Batch import:
   - Import all .glb files in a directory with progress bar
   - Optionally place each model at grid positions for browsing

Register the addon properly so it survives Blender restart
(install from zip workflow).
```

**After Claude Code finishes:** Install the addon in Blender via
Edit → Preferences → Add-ons → Install. Test importing each of
the five sample models. Verify bone orientations look natural and
deformation still works correctly when posing.

### Acceptance criteria

- [ ] Blender addon installs and appears in File → Import menu
- [ ] Bone orientations display correctly for humanoid skeletons
- [ ] Deformation still works correctly after bone roll fix
- [ ] Bone name dictionary applied when available
- [ ] Import options panel shows checkboxes for optional features
- [ ] Multiple models can be merged under one armature (same template)
- [ ] Addon survives Blender restart (proper bl_info registration)

---

## Phase 8 — Bone name resolution + animation (research)

**Goal:** Resolve bone name hashes to readable strings, and lay the
groundwork for animation import.

### 8A. Identify the hash function

The bone `dwHash` values in BoneStructure are 32-bit hashes of bone name
strings. The hash function is NOT FNV-1a, FNV-1, or CRC32 — tested against
known field name → hash pairs from definitions.json and none matched.

```
ultrathink. Read docs/skeleton_format_spec.md and d4extract_roadmap.md
for context. Bones in the exported .glb files are named "bone_<hash>"
because we don't know the hash function that maps bone names to the
dwHash values stored in BoneStructure.

GOAL: Identify the hash function so we can brute-force bone names.

We have ground truth pairs from definitions.json — field names and
their corresponding hash values:

  "transform"         → 24597212  (0x017752dc)
  "wp"                → 4039      (0x00000fc7)
  "dwHash"            → 30829055  (0x01d669ff)
  "nParentIndex"      → 175282672 (0x0a7299f0)
  "eVBFormat"         → 119266950 (0x071bde86)
  "dwFlags"           → 210056392 (0x0c8534c8)
  "ptShapes"          → 123594024 (0x075de528)
  "nLOD"              → 4038509   (0x003d9f6d)

These are NOT standard FNV-1a, FNV-1, CRC32, DJB2, SDBM, or Jenkins
one-at-a-time (already tested, none match).

APPROACH:
1. Try every common game hash variant:
   - DJB2 and DJB2a (xor variant)
   - Murmur2 and Murmur3 (32-bit)
   - SuperFastHash (Paul Hsieh)
   - Jenkins one-at-a-time and lookup3
   - xxHash32
   - Blizzard-specific: SStrHash from WoW/D3 source leaks
   - Try each with: as-is, lowercase input, uppercase input
   - Try each with seeds 0, 1, and common Blizzard seeds

2. If no standard function matches, analyze the hash properties:
   - All observed values are positive and < 0x10000000 (~28 bits)
   - Check if this is a hash modulo some prime
   - Check if it's a truncated 32-bit hash (top bits zeroed)
   - Look for the hash function in the Diablo IV executable
     (search for the offset basis constant near XOR/multiply ops)

3. Search community resources:
   - Check d4parse source for any hash implementation we missed
   - Check CASCLib for Blizzard hash implementations
   - Search WoW modding wikis for SNO field hash functions
   - The "Not Finding a Cow Level" Discord may have this info

4. If the hash function is found, verify it produces correct values
   for ALL 8 test pairs above. Then write a Python implementation
   and save it to src/d4extract/utils/hash.py.

5. If the hash function cannot be identified, document what was
   tried and move to the dictionary attack approach in 9B.
```

### 8B. Bone name dictionary attack

Run after 8A identifies the hash function (or skip if hash is unknown):

```
ultrathink. Read docs/skeleton_format_spec.md for context.

Using the hash function in src/d4extract/utils/hash.py (from Phase 8A),
build a bone name dictionary by hashing candidate names and comparing
against known bone hashes.

STEP 1: Collect all unique bone hashes.
Parse all skinned test models. Collect every unique dwHash value across
all models. There will be significant overlap between models sharing
the same character class skeleton base.

STEP 2: Generate candidate bone names.
Blizzard uses predictable naming conventions. Generate candidates:
- Standard bone names: spine, pelvis, hips, chest, neck, head,
  shoulder, arm, forearm, hand, finger, thumb, thigh, calf, foot,
  toe, clavicle, jaw, eye, weapon, shield, back, cape, tail, wing
- With prefixes: l_, r_, left_, right_, bone_, b_, jnt_
- With suffixes: _01 through _10, _l, _r, _tip, _end, _root,
  _twist, _roll, _helper, _cloth, _attach, _fx, _hardpoint
- CamelCase and snake_case variants
- Diablo-specific: horn, tentacle, claw, fang, spike, chain,
  trophy, weapon_r, weapon_l, shield, offhand
- Use d4data/dict.txt (32K words) as additional building blocks
- Common separators: _, none (camelCase)
- Generate 2-3 word combinations of the above

STEP 3: Hash all candidates and match.
For each candidate string, compute its hash and check against the
set of known bone hashes. Record all matches.

STEP 4: Save results.
Write the hash→name dictionary to data/bone_names.json.
Update app_parser.py to load this dictionary and resolve bone
names during skeleton parsing. Fall back to "bone_<hash>" for
any unresolved hashes.

Print: total bones, resolved count, unresolved count, and a
sample of resolved names to verify they make sense.
```

### 8C. Animation research (groundwork only)

```
ultrathink. Read docs/skeleton_format_spec.md, docs/app_format_spec.md,
and d4extract_roadmap.md for context.

This is RESEARCH ONLY — do not build a parser yet. The goal is to
document the animation chain so it can be implemented in a future phase.

KEY CONTEXT: The animation reference chain was described by the D4
modding community but has NOT been verified against definitions.json.
The claimed ActorDefinition layout is:

  ActorDefinition (UNVERIFIED — verify in Step 1)
    ├── snoAppearance              → DT_SNO<Appearance> (our .app file)
    ├── arCustomizationAppearances → DT_VARIABLEARRAY<DT_SNO<Appearance>>
    ├── arAppearanceSets           → DT_VARIABLEARRAY<DT_SNO<AppearanceSet>>
    ├── arAnimSets                 → DT_VARIABLEARRAY<DT_SNO<AnimSet>>
    ├── arStoreAnimSets            → DT_VARIABLEARRAY<DT_SNO<AnimSet>>
    ├── snoAnimTree                → DT_SNO<AnimTree>
    └── tAnimTreeOverride          → AnimTreeOverride

VERIFY THIS FIRST by finding ActorDefinition in definitions.json and
comparing the actual field list. Report any differences.

Also: TACT keys only encrypt unreleased cosmetics, NOT base game
content. Animation files for base game actors should be extractable.

STEP 1: Find animation-related structs in definitions.json.
Clone https://github.com/DiabloTools/d4data if not present. Load
definitions.json and find:
- ActorDefinition — the hub connecting appearance + animations
- AnimSetDefinition — collection of animation clips
- AnimationDefinition — individual animation clip
- AnimTreeDefinition — state machine / blend tree
Document their struct layouts, focusing on fields that reference
other SNO types or contain bone hashes.

STEP 2: Trace the full animation chain.
Starting from ActorDefinition, trace:
  Actor.arAnimSets[] → AnimSet → individual AnimationDefinitions
  Actor.snoAnimTree → AnimTree (how animations are selected/blended)

For our test models, find their ActorDefinitions in d4data/json/:
- Search d4data/json/base/meta/Actor/ for files referencing
  test models by appearance SNO ID
- Extract the arAnimSets references to identify animation files

STEP 3: Examine AnimationDefinition's keyframe format.
From definitions.json, identify:
- How bones are referenced (by dwHash? by index?)
- Keyframe data format (separate P/R/S curves? packed quaternions?)
- Duration, framerate, loop mode fields
- Compression scheme (if any — Blizzard often uses quantized curves)
- File size: is keyframe data in meta, payload, or both?

STEP 4: Assess feasibility.
- Try extracting an Actor JSON from d4data for a test model
- Can the referenced AnimSet SNO files be found in d4data/json/?
- Are animation payload files extractable from CASC without TACT keys?
- Estimate: how many animations per character class?

Write all findings to docs/animation_format_spec.md.
Do NOT implement a parser — this is research for a future phase.
```

**After Claude Code finishes 8C:** Review animation_format_spec.md to
decide whether animation import is feasible and plan implementation.

### Acceptance criteria

- [ ] Hash function identified and implemented (or documented as unknown)
- [ ] Bone name dictionary built with best-effort name resolution
- [ ] Resolved bone names appear in exported .glb files
- [ ] Animation reference chain documented (Actor → AnimSet → Animation)
- [ ] AnimationDefinition struct layout documented
- [ ] Feasibility assessment for animation import written
- [ ] docs/animation_format_spec.md created


## Phase 9 — Desktop GUI (D4.Export)

**Status: Not started**

**Goal:** Build **D4.Export**, a PySide6 + PyVistaQt desktop application
that lets users browse, preview, and export 3D models from their Diablo IV
installation. The GUI is a thin presentation layer over the existing
production Python pipeline — it calls `app_parser`, `material_parser`,
`texture_parser`, and `GltfExporter` directly, with no reimplementation.

**UX reference:** wow.export — browse → preview → export flow adapted
to D4's CASC archive and .app model format.

**Tech stack:** PySide6 (Qt 6) for the app shell, PyVistaQt + VTK for
the 3D viewport, existing Python parsers for all data work.

**Full implementation architecture:** `docs/d4export_gui_implementation.md`
contains self-contained prompts for each sub-phase.

### 9A. Game directory setup

Application shell with dark theme (#1e1e2e), `QStackedWidget` layout,
game directory picker with auto-detection (Steam/Battle.net), and
`QSettings` persistence. Validates D4 install via `Data/` or `.build.info`.

### 9B. Model list browser

Full-window scrollable list of ~13K model entries from CASC. Uses
`QListView` + `QStringListModel` + `QSortFilterProxyModel` for instant
filtering. Quick filter category buttons (MON, PLR, NPC, ITM, ENV, WPN).
`CatalogWorker` (QThread) scans CASC and caches results to disk.

### 9C. 3D viewport preview

Two-panel split: model list (30%) + PyVistaQt `BackgroundPlotter` (70%).
`LoadModelWorker` extracts from CASC and parses on a background thread;
rendering happens on the main thread only. Per-submesh coloring,
properties overlay, orbit camera, wireframe toggle.

### 9D. GLB export

`ExportButton` with dropdown options menu wired to `GltfExporter`.
Configurable: textures, skeleton, normals, tangents, prune bones,
cloth meshes, coordinate system. `ExportWorker` runs on QThread.
D4Data path required for texture embedding.

### Acceptance criteria

- [ ] App launches with dark theme, game directory setup works
- [ ] CASC catalog scanned and cached, ~13K models displayed
- [ ] Filter bar and category buttons narrow the list instantly
- [ ] Clicking a model renders it in the 3D viewport
- [ ] Properties overlay shows vertex/tri/bone counts
- [ ] Export produces valid .glb that opens in Blender 5.1
- [ ] All blocking work runs on QThread workers
- [ ] Export options persist across sessions via QSettings

---


## Known issues tracker

| Issue | Phase | Status | Notes |
|-------|-------|--------|-------|
| Normals garbled (packed uint8x4 eFormat 8, not float32) | 2 | ✅ Resolved | Decode: `(byte / 127.5) - 1.0` |
| UVs incorrect (half-float eFormat 7 not converted) | 2 | ✅ Resolved | Convert float16 → float32 |
| Rotation/orientation wrong | 2-3 | ✅ Resolved | Coordinate system conversion: (x,y,z) → (x,z,-y) |
| 3/5 test models still garbled | 2 | ✅ Resolved | Fixed via SubObjectSegment offset handling |
| Attribute order differs between Format 4 and 6 | 2 | ✅ Resolved | Parser reads VertexElem array dynamically |
| gltf_export.py drops most attributes | 3 | ✅ Resolved | All 9 attributes now exported |
| Spec file Sections 2-3 conflict with Section 9 | 2 | Documented | Section 9 is authoritative; Sections 2-3 from early hex analysis have errors |
| eSemantic enum values unknown | 2 | ✅ Resolved | 0=POS, 1=UV0, 2=UV1, 7=COLOR0, 8=COLOR1, 9=NORM, 10=TAN, 11=JOINTS, 12=WEIGHTS |
| eFormat enum values unknown | 2 | ✅ Resolved | 1=float3, 2=snorm16x2, 4=uint8x4, 5=unorm4, 7=half2, 8=packed4 |
| Stride 52 hypothesis | 2 | ✅ Resolved | Does not exist — was multiple buffers misidentified as one stream |
| TACT encryption (cosmetics only) | 6 | ✅ Resolved | Only encrypts unreleased content/cosmetics; base game models unencrypted |
| D4Analyzer crashes on expansion content | — | External | Missing TACT keys for unreleased cosmetics in new expansion |

---
