# Diablo IV `.app` Skeleton Format Specification

**Status:** IMPLEMENTED — Structural layout derived from `d4data/definitions.json` (3,066 RTTI structs) and verified against five sample `.app.json` dumps; binary offsets are now confirmed by `parse_skeleton()` in `src/d4extract/formats/app_parser.py`, which decodes 190/298/244-bone skeletons cleanly from the binary payloads of barF_H07 / necF_stor229_HLM / Azmodan and produces zero false positives on the static Goatman / offHandsSorc samples. Thirteen dedicated tests in `tests/test_skeleton.py` cover the full chain (parse → palette propagation → glTF skin emission → bone pruning) including a regression test that pins Azmodan's 9 LOD0 submeshes to their d4data-JSON ground truth values for material indices, vC/iC counts, palette sizes, and palette content.

**Headline finding:** the bone hierarchy is **not** stored in a separate SNO group. It lives **inside the same `.app` Appearance file** as the geometry, embedded in the `Structure → BoneData → BoneStructure` chain that the existing parser already walks for chunks/LODs/sub-objects. There is no skeleton-typed cross-reference field on `AppearanceDefinition`.

---

## 1. Where the skeleton lives

### 1.1 Containment chain

```
AppearanceDefinition  (RTTI hash 2214406937, snoGroup 9)
  └─ tStructure : Structure                           (hash  835243889, +0x08)
        ├─ ptChunks      : array<GeoChunk>            (+0x20)   ← geometry (already parsed)
        └─ ptBoneData    : array<BoneData>            (+0x40)   ← skeleton root
              └─ ptBoneStructure : array<BoneStructure> (+0x08) ← per-bone records
```

`ptBoneData` is a `DT_VARIABLEARRAY` but in every sample we have looked at it
contains exactly **one** `BoneData` element — there is one skeleton per
appearance file. The interesting array is the inner one: `ptBoneStructure` is
the flat list of every bone in the skeleton (base + cloth + helper bones).

### 1.2 No external skeleton SNO

A transitive scan of every `DT_SNO` field reachable from `AppearanceDefinition`
yields only these external references — none of them point to a skeleton or
animation file:

| Field path                                                                       | Target SNO group |
|-----------------------------------------------------------------------------------|------------------|
| `AppearanceDefinition.snoFusedPrefab`                                             | 24 (FusedPrefab) |
| `Structure.ptChunks[].ptLODs[].ptSubObjects[].snoCampaignVisibilityCondition`     | 51               |
| `Structure.ptBoneData[].ptBoneStructure[].snoParticleSystem`                      | 27 (Particle)    |
| `Structure.ptBoneData[].ptBoneStructure[].ptConstraint[].snoParticleSystemSno`    | 27               |
| `Structure.ptBoneData[].ptNPCWeaponHardpointOverrides[].snoItemType`              | 98               |
| `ptAppearanceMaterials[].ptSOAs[].snoMaterial / snoOverrideMaterial`              | 57               |
| `ptAppearanceMaterials[].ptSOAs[].snoCloth / snoHighQualityClothOverride`         | 11 (Cloth)       |
| `ptAppearanceMaterials[].ptSOAs[].snoEffectGroup`                                 | 14               |
| `ptAppearanceMaterials[].ptSOAs[].arAnimClothOverrides[].snoClothOverride`        | 11               |
| `tFoliageSettings.snoSoundDisturbed / snoSoundImpact / snoAmbientSound`           | 40 / 40 / 5      |
| `arBaseAppearanceTextureOverrides[].snoTexture`                                   | 44               |

`AnimationDefinition` (file-type hash `0x2088d14a`) and `AnimSetDefinition`
(`0x8f0f1a87`) exist as separate SNO groups, but `.app` files do not link to
them directly. Animation playback presumably joins an `.app` skeleton with a
separate clip at a higher engine layer (e.g. through `ActorDefinition` or
`AnimTreeDefinition`), but **for export to glTF the skeleton is fully
self-contained inside the `.app`**.

### 1.3 Sample bone counts

`dataOffset` / `dataSize` are reported by the d4data JSON for the
`__external__` `ptBoneStructure` array; dividing by `sizeof(BoneStructure) = 232`
gives the bone count, which matches `BoneData.nBaseBoneCount + nClothBoneCount`:

| Model                      | dataOffset | dataSize | Bones | nBaseBoneCount | nClothBoneCount |
|----------------------------|------------|----------|-------|----------------|------------------|
| `Goatman_BossTrophy`       | —          | —        | 0     | —              | — (static)       |
| `offHandsSorc_stor029`     | 0          | 0        | 0     | 0              | 0  (static)      |
| `barF_H07` (body armor)    | 224        | 44,080   | 190   | 190            | 0                |
| `necF_stor229_HLM` (helm)  | 224        | 69,136   | 298   | 192            | 106              |
| `Azmodan` (boss)           | 58,792     | 56,608   | 244   | 95             | 149              |

So the rule is `total_bones = nBaseBoneCount + nClothBoneCount`, and skinned
character pieces (`barF_H07`, `necF_stor229_HLM`) embed the **full character
skeleton** even when the rendered geometry is just a helmet — they need it so
their per-segment bone palettes can address the same global indices the
underlying body uses.

Where these `dataOffset` values resolve in the actual binary (meta vs. payload
file, and which header section) still needs verification by the parser. The
existing meta files (e.g. barF_H07 = 3,304 bytes) are too small to hold the
44 KB bone array, so the storage must be in the payload file or an additional
streamed blob; the structural layout described below is the same regardless.

---

## 2. `BoneStructure` — per-bone record (size 232 bytes, hash 3352290229)

This is the unit of the bone array. Field offsets are taken verbatim from
`definitions.json`.

| Offset | Size | Type / element                | Field                  | Meaning                                             |
|--------|------|-------------------------------|------------------------|-----------------------------------------------------|
| 0x00   | 16   | `array<CollisionShape>`       | `ptShapes`             | Per-bone collision primitives (ragdoll, hit shapes) |
| 0x10   | 16   | `array<ConstraintParameters>` | `ptConstraint`         | Joint constraints (attached particle, limits)       |
| 0x20   | 4    | uint32                        | `dwHash`               | Bone-name hash (FNV-style; resolve via d4data names)|
| 0x24   | 4    | uint32                        | `dwFlags`              | Bone flags (root / cloth / hidden / etc.)           |
| 0x28   | 2    | int16                         | **`nParentIndex`**     | Index into the same `ptBoneStructure` array; root bone uses `0xFFFF` (−1) |
| 0x2A   | 2    | int16                         | `nLOD`                 | LOD level at which this bone first appears          |
| 0x2C   | 2    | int16                         | `nTrueBoneLOD`         | "True" LOD (independent of asset LOD selection)     |
| 0x2E   | 2    | uint16                        | `unk_b03b557`          | Padding/unknown                                     |
| 0x30   | 4    | DT_SNO (group 27)             | `snoParticleSystem`    | Optional particle attached to this bone             |
| 0x34   | 16   | `Sphere`                      | `wsBounds`             | World-space-relative bounding sphere (centre + radius) |
| 0x44   | 40   | `PRSTransform`                | **`transform`**        | World-space rest pose (model-space bind pose)       |
| 0x6C   | 40   | `PRSTransform`                | **`transformInv`**     | Inverse of `transform` (cached)                     |
| 0x94   | 40   | `PRSTransform`                | **`transformParentRel`** | Local pose relative to parent (the "rig" pose)    |
| 0xBC   | 40   | `PRSTransform`                | **`transformSkinningInv`** | **Inverse bind matrix** — the `IBM` glTF needs   |
| 0xE4   | 4    | —                             | (tail padding)         |                                                     |

`PRSTransform` (40 bytes, hash 4111826321):

| Offset | Size | Type      | Field    |
|--------|------|-----------|----------|
| 0x00   | 16   | `bcQuat`  | `q`      | Rotation, layout `(x, y, z, w)` float32 |
| 0x10   | 12   | float[3]  | `wp`     | Translation                              |
| 0x1C   | 12   | float[3]  | `vScale` | Scale (per-axis)                         |

Reconstruction notes:
- glTF `nodes[i].translation/rotation/scale` should come from `transformParentRel` (it is the only transform that is genuinely **local**; the other three are world-space caches).
- glTF `inverseBindMatrices[i]` is the 4×4 expansion of `transformSkinningInv`. Build it as `IBM = T(wp) · R(q) · S(vScale)` (standard TRS) — note this is already the inverse, so do not invert it again.
- `transform` and `transformInv` can be ignored for export; the runtime caches them to skip the recursive multiply.
- `dwHash` is the only name we have. The bones are anonymous in the binary; resolving to a readable string requires a hash dictionary (`d4data/dict.txt` / `dict.json`). Falling back to `bone_<dwHash:08x>` is acceptable.
- `nParentIndex == 0xFFFF` (treated as `-1`) marks roots. In multi-root rigs every root forms its own subtree under the model node.

### 2.1 Hierarchy reconstruction

The tree is implicit: walk the array in storage order, and `nParentIndex` of
every entry refers to an earlier entry. To emit glTF nodes:

```
nodes[i] = {
    name:        f"bone_{bone[i].dwHash:08x}",
    translation: bone[i].transformParentRel.wp,
    rotation:    bone[i].transformParentRel.q,
    scale:       bone[i].transformParentRel.vScale,
}
for i, b in enumerate(bones):
    if b.nParentIndex == 0xFFFF:
        roots.append(i)
    else:
        nodes[b.nParentIndex].children.append(i)
skin.joints = list(range(len(bones)))
skin.inverseBindMatrices = [TRS_matrix(b.transformSkinningInv) for b in bones]
```

---

## 3. How the Appearance file references the skeleton

It does **not** — the skeleton is a child of `Structure` rather than a
reference. The same `Structure` object also owns the geometry chunks, so the
parser already has both pieces in hand once it has loaded the meta:

```
Structure (+0x20) ptChunks      → geometry
Structure (+0x40) ptBoneData[0] → skeleton (BoneData)
```

Implications for the exporter:
1. `gltf_export.py` should add a `Skin` whenever `tStructure.ptBoneData[0].ptBoneStructure` is non-empty.
2. `BoneData.nMaxAnimLOD` and `BoneStructure.nLOD` define a per-LOD bone subset. For a first pass we can emit the full skeleton on every LOD — glTF lets unreferenced joints sit unused.
3. `BoneData.tLookAtData`, `tIKData`, `tLockedRotationBoneData`, `ptHardpoints`, `ptGlobalComponentClothCapsules` describe runtime-only behavior (look-at chains, IK, attachment slots, cloth colliders). None of it is needed to display the rest pose; we can ignore it for the initial skinning export.
4. There is no separate animation file referenced from the `.app`. To play
   an animation you would still need to load an `AnimationDefinition`
   (snoGroup ≈ `0x2088d14a` mapping) keyed by bone-name hashes — that work
   is out of scope for skinning support.

---

## 4. `SubObjectSegment.pBoneIDs` — the per-segment bone palette

### 4.1 Struct definition (already in `app_format_spec.md` §9.1)

```
SubObjectSegment (hash 1370930836, size 32)
  +0x00 array<DT_BYTE> pBoneIDs       // local-bone-palette → global-bone-index map (uint8 indices when total ≤ 256)
  +0x10 uint32         nVertCount     // vertices in this segment
  +0x14 uint32         nVertOffset    // vertex offset within the chunk's vertex buffer
  +0x18 uint32         nIndexCount    // indices in this segment
  +0x1C uint32         nIndexOffset   // index offset within the chunk's index buffer
```

### 4.2 The mapping

`pBoneIDs` is a **per-draw-call bone palette**. The `JOINTS_0` byte values
already extracted by `app_parser.py` are **indices into this palette** (0..N−1),
**not** indices into the global `ptBoneStructure` array. To get the global
index that glTF wants:

```
global_bone_index = pBoneIDs[ joints_byte ]
```

This is the standard "skinning palette" optimization — it lets each draw
call ship a small constant buffer of bone matrices instead of the entire
skeleton.

### 4.3 Observed values

**barF_H07 (body armor, 190 global bones, 1 chunk · 1 sub-object · 1 segment per LOD):**

| LOD | nVertOffset | nVertCount | nIndexOffset | nIndexCount | len(pBoneIDs) | First few palette entries |
|-----|-------------|------------|--------------|-------------|----------------|----------------------------|
| 0   | 0           | 13,240     | 0            | 61,356      | 21             | `[103, 52, 8, 74, 99, 55, 35, 84, 93, 7, 10, …]` |
| 1   | 0           | 10,427     | 0            | 46,014      | 21             | `[103, 8, 52, 74, 99, 55, 35, 84, 44, 93, 7, 10, …]` |

Both LODs reach the same 21 global bones, but in slightly reordered palettes.
Each `JOINTS_0.x/y/z/w` byte therefore lands in `0..20` and is then remapped
through `pBoneIDs` to the appropriate global index.

**necF_stor229_HLM (helmet, 298 global bones, 5 sub-objects per LOD):**

| LOD | SO range | pBoneIDs |
|-----|----------|----------|
| 0/1 | every segment | `[62]` — single rigid bone, head only |

The helmet ships as 5 sub-objects (probably split by material) but every one
is rigidly bound to global bone 62. JOINTS_0 in the vertex stream will be
`0,0,0,0` for every vertex.

### 4.4 Exporter integration sketch

```
# Per chunk-vertex-buffer, build a global JOINTS_0 stream:
for so in lod.ptSubObjects:
    palette = so.ptSegments[seg].pBoneIDs
    for v in vertices[seg.nVertOffset : seg.nVertOffset + seg.nVertCount]:
        v.JOINTS_0 = tuple(palette[b] for b in v.JOINTS_0_byte)
```

Then point `primitive.attributes.JOINTS_0` at the remapped uint16 stream and
attach the single `Skin` built from `BoneStructure[]`. `WEIGHTS_0` flows
through unchanged.

If two segments inside the same vertex buffer overlap (they do not in the
samples — `nVertOffset + nVertCount` of one equals the start of the next),
each vertex still belongs to exactly one segment, so the remap is unambiguous.

`SubObject.ptBaseBoneInfluences` (`+0xB0`, `array<DT_BYTE>`) appears alongside
the segment table and likely stores a fallback uniform-bone palette for the
sub-object as a whole; we do not need it once `pBoneIDs` is wired up, but it
is worth keeping in the parser output for future verification.

---

## 5. Confidence summary

| Claim                                                                          | Confidence |
|--------------------------------------------------------------------------------|------------|
| Skeleton is embedded inside `Structure.ptBoneData[0].ptBoneStructure`          | HIGH       |
| Field layout of `BoneStructure` (parent + 4 PRSTransforms + flags + hash)      | HIGH (RTTI verbatim) |
| `transformSkinningInv` is the per-bone inverse bind matrix                     | HIGH (name + position match standard skinning convention) |
| `transformParentRel` is the local rest pose                                    | HIGH       |
| `nParentIndex == 0xFFFF` flags root bones                                      | MEDIUM (typical convention; worth verifying on first parse) |
| `pBoneIDs` is a local→global palette indexed by `JOINTS_0` byte values         | HIGH       |
| No external SNO file is required for skinning                                  | HIGH       |
| Exact byte location of `ptBoneStructure` within meta+payload                   | LOW (still needs binary verification) |

---

## 6. Implementation status

All four steps below are wired up in `src/d4extract/formats/app_parser.py`
and `src/d4extract/export/gltf_export.py`.

### 6.1 Bone-array binary discovery

The bone array lives at a payload offset reachable from the meta. Scanning
the meta for a 16-byte `DT_VARIABLEARRAY` header (8 zero bytes, then
`dataOffset` and `dataSize`) where:

- `dataSize % 232 == 0` (`sizeof(BoneStructure) == 232`),
- `dataOffset + dataSize ≤ payload_size`,
- `int16(payload[dataOffset + 0x28]) == -1` (root-bone marker), and
- the `transformParentRel` quaternion at `payload[dataOffset + 0x94]` has
  magnitude near 1,

uniquely identifies the bone array in every skinned sample (one match) and
returns zero matches in static samples. Implemented as `_scan_bone_array()`.

The owning `BoneData` struct sits 8 bytes earlier in the meta — the
`ptBoneStructure` field is at offset `+0x08` in `BoneData`. From that we
read `unk_a3acec8` (skeleton template id), `nBaseBoneCount`, and
`nClothBoneCount`. Implemented as `_read_bonedata_header()`.

### 6.2 Public types

```
class BoneTransform:        # 40 B PRSTransform
    q:     (x, y, z, w) float32
    wp:    (x, y, z) float32
    scale: (x, y, z) float32

class Bone:                 # one BoneStructure record
    index, parent_index, name_hash, flags, lod
    local_trs:    BoneTransform   # transformParentRel  -> glTF node TRS
    inv_bind_trs: BoneTransform   # transformSkinningInv -> glTF IBM (no further inversion)

class Skeleton:
    bones, base_bone_count, cloth_bone_count, template_id, bone_array_offset

class Submesh:
    ..., bone_palette: tuple[int, ...]   # per-segment pBoneIDs
```

`MeshData.skeleton: Skeleton | None` is populated by `parse_app()` for both
the primary path (`_build_lod0_mesh`) and the legacy path. Per-segment
`bone_palette` is read from `pBoneIDs` and propagated through
`Submesh.bone_palette`.

### 6.3 glTF export

`GltfExporter.__init__` gained two flags:

- `export_skin=True` — emit a `Skin` and `JOINTS_0` / `WEIGHTS_0` whenever
  `MeshData.skeleton` is non-empty and the vertex stream carries blend
  data.
- `prune_bones=False` — when True, trim the exported skeleton to the union
  of every weighted bone and its ancestor chain, then remap `JOINTS_0`
  values into the compact index space.

CLI flags `--no-skin` / `--prune-bones` mirror these. The companion
`QUATERNION_TRANSFORMS` and `SCALE_TRANSFORMS` tables in `gltf_export.py`
keep bone TRS consistent with the position transform: for the
`z_up_to_y_up` preset the quaternion conversion is `(qx, qy, qz, qw) →
(qx, qz, -qy, qw)` and the scale conversion is `(sx, sy, sz) → (sx, sz,
sy)` — the conjugation of the rotation under the same axis swap that
moves vertex positions.

Inverse bind matrices are composed from `transformSkinningInv` as a TRS
4×4 (after the same axis swap) and written **without** further inversion —
the on-disk transform is already inverted.

### 6.4 Index-buffer false-positive filter

The first cut of the parser successfully decoded barF / necF (skinned)
and Goatman / offHandsSorc (static), but `Azmodan` fell through
`_build_lod0_mesh` to the legacy heuristic path because
`_scan_geo_chunk_index_buffers` returned 5 candidates instead of 2.
Three were false positives that share the IB struct's leading
8-zero signature:

- two stray DT_VARIABLEARRAY headers from earlier in the meta (likely
  pieces of `MarkerSet` / `ClothData`), and
- the **bone-array DT_VARIABLEARRAY** at meta offset `0x3788` —
  ``BoneData.ptBoneStructure``'s header, with `dataOffset=58792` and
  `dataSize=56608`, which uniquely passes the IB scan's payload-size
  and even-size checks because Azmodan's bone array sits in the same
  payload region as its index buffers.

The shifted ``array_index`` then broke the SubObject pairing
(SubObjects' `ibi=0` no longer matched the LOD0 IB at `array_index=2`).

Fix in `_filter_ib_cluster()`:

1. **Position check** — real IBs always sit immediately after the VBs
   in the meta (gap 104–184 bytes in every sampled appearance).
   Candidates with negative or pathologically large gaps are rejected.
2. **24-byte-strided cluster** — `GeoChunkIndexBuffer` records form a
   contiguous array, so real IBs are at exact 24-byte file-offset
   stride from each other. Among the post-VB candidates, the longest
   24-stride run wins; singleton outliers (the bone-array header at
   `prev + 32`) are dropped.

This passes the raw `_scan_geo_chunk_index_buffers` result through the
filter only when called with `vbs` (the new optional argument) — the
stand-alone scan stays unchanged so the older legacy callers that
exercise it under synthetic fixtures remain unaffected.

### 6.5 Verified outputs

| Model | Skeleton | LOD0 weighted | Pruned active | Submeshes | JOINTS_0 max | Skin emit |
|---|---|---|---|---|---|---|
| `barF_H07` | 190 bones | 21 | 26 | 1 | 25 | ✓ |
| `necF_stor229_HLM` | 298 bones (192 base + 106 cloth) | 1 | 8 | 5 | 7 | ✓ |
| `Azmodan` | 244 bones (95 base + 149 cloth) | 196 | 229 | 9 | 243 | ✓ |
| `Goatman_BossTrophy` | none | — | — | 3 | — | static, no Skin emitted |
| `offHandsSorc_stor029` | none | — | — | 3 | — | static, no Skin emitted |

Azmodan output cross-checked against `d4data/json/base/meta/Appearance/Azmodan.app.json`:

- vertices = 49,912 ✓, indices = 249,936 ✓
- 9 LOD0 SubObjects with materials 0–8 ✓
- per-submesh `(vC, iC)`: `(32, 48), (127, 480), (30, 96), (58, 228),
  (786, 3198), (6602, 35244), (25378, 129294), (11778, 57942), (5121, 23406)`
  — every value matches.
- per-submesh palette sizes: `(6, 7, 8, 6, 95, 30, 60, 88, 11)` — every
  size matches d4data; first 5 entries of every palette match too.
- skeleton: 244 bones (95 base + 149 cloth), `template_id = 0xb34ad8a3`
  (decimal 3,008,026,787) ✓.
- glTF: 245 nodes (1 mesh + 244 bones), 9 primitives, 244 IBM matrices,
  `JOINTS_0` max = 243 (= bone-count − 1, the last cloth bone — exactly
  what `palette[i] = 243` resolves to).

All exports pass internal structural checks (bufferView/accessor bounds,
`Skin.joints` ⊂ nodes, IBM count == joint count, single-parent invariant
on the node tree). Khronos `gltf-validator` was not available in this
environment; `tests/test_skeleton.py` performs the equivalent checks.

### 6.6 Known limitations

- Bone names live as 32-bit hashes only; `dict.json` does not contain the
  bone-name corpus. Names fall back to `bone_<hash:08x>`. Resolving the
  corpus would require either a brute-force dictionary attack on the
  hash function or extracting the names list from a future game patch.
- Animation playback is out of scope. `AnimationDefinition` (snoGroup
  hash `0x2088d14a`) is a separate SNO file type that is not referenced
  from `AppearanceDefinition`; mapping clips onto an exported skeleton
  is a future phase.
