---
name: d4-animation-extraction
description: "Diablo IV animation binary format, payload structure, and reverse engineering plan for extracting per-model animations. Covers the full reference chain from ActorDefinition through AnimSet to AnimationDefinition to AnimPayloadData, the AnimPermutation struct layout (45 fields), curve storage format (ptTranslationCurves/ptRotationCurves/ptScaleCurves as raw byte blobs), flCompression modes (0-6 all decode through one short-form curve layout — flCompression does not control curve encoding; the real format fork is u8 vs u16 keyframe-timestamp width), payload size analysis, TACT encryption status for animations, and the five-phase reverse engineering strategy. Use this skill whenever working on animation extraction from CASC, parsing .ani payload files, decoding compressed animation curves, implementing glTF animation export, linking AnimSets to ActorDefinitions, understanding AnimPayloadData binary layout, or planning animation-related pipeline work. Also trigger when the user asks about D4 animation formats, keyframe data, animation compression, root motion, or how animations connect to models."
---

# D4 Animation Extraction

This skill documents what we know about Diablo IV's animation binary format based on a deep dive into the d4data repository. It answers the Phase 9C research questions from the d4-skeleton-animation-assembly skill and provides the blueprint for extracting individual model animations.

**Status summary:** The entire metadata chain from actor to animation is fully mapped and accessible in d4data. The blocker is decoding the compressed keyframe data inside .ani payload files, which requires extracting payloads from CASC and reverse engineering the curve compression format.

## The animation reference chain

Every animation in D4 is reachable through a well-defined SNO reference chain:

```
ActorDefinition (.acr)          — 59,659 actors in d4data
  ├── snoAppearance             → Appearance (.app) — geometry + skeleton
  ├── arAnimSets[]              → AnimSetDefinition (.ans) — 11,840 sets
  ├── arStoreAnimSets[]         → store/MTX animation overrides
  ├── snoAnimTree               → AnimTreeDefinition (.ant) — 87 state machines
  └── tAnimTreeOverride         → per-actor node overrides

AnimSetDefinition (.ans)
  └── ptPowerEntryList[]        → AnimSetPowerEntry
        ├── snoPower            → Power SNO (action name: "AnimKey_Run", "Death", etc.)
        ├── snoAnim             → AnimationDefinition (.ani)
        └── snoFemaleOverrideAnim → optional female variant

AnimationDefinition (.ani)      — 45,331 animations (121 TACT-encrypted)
  ├── snoAppearance             → target skeleton/appearance
  ├── ptPermutations[]          → AnimPermutation (1-3 randomized variants)
  └── arPermutationIndices[]    → playback order

AnimPermutation
  └── ptPayloadData             → AnimPayloadData (EXTERNAL — in payload file)
        ├── ptBoneNames[]       → uint32 bone hashes (maps curves to skeleton)
        ├── ptTranslationCurves[] → per-bone position curves (raw bytes)
        ├── ptRotationCurves[]  → per-bone rotation curves (raw bytes)
        ├── ptScaleCurves[]     → per-bone scale curves (raw bytes)
        └── (root motion, facing, depth of field, etc.)
```

This chain has been **verified against definitions.json** — the ActorDefinition struct (hash 3085005858) confirms all fields including `arAnimSets`, `snoAnimTree`, and `tAnimTreeOverride`.

## Concrete example: Barbarian

The male Barbarian actor (`barbarianM.acr`) demonstrates the full chain:

- **Appearance:** `barM_base00.app` (the skeleton with 190 bones)
- **AnimTree:** `Player_AnimTree.ant` (shared across all player classes)
- **AnimSets:** 52 sets covering gameplay, mounts, emotes, UI, fishing, store cosmetics
  - `barb_base.ans` — 62 entries: idle, walk, run, evade, death, knockback, flinch, traversals
  - `bar_skills_DW.ans` — dual-wield skill animations
  - `bar_mount.ans` — horseback animations
  - etc.

Each AnimSetPowerEntry maps a game action to a specific `.ani` file, with optional male/female overrides:

```
AnimKey_Neutral  → barM_HTH_nav_idle   (female: barF_HTH_nav_idle)
AnimKey_Run      → barM_HTH_nav_run    (female: barF_HTH_nav_run)
Death            → barM_HTH_reac_death (female: barF_2HS_reac_death)
Evade            → barM_HTH_nav_evade  (female: barF_HTH_nav_evade)
```

## AnimationDefinition struct (hash 880817737)

The .ani meta file contains everything except the actual keyframe bytes:

| Field                    | Type        | Purpose                                    |
|--------------------------|-------------|--------------------------------------------|
| snoAppearance            | DT_SNO      | Target skeleton/appearance                 |
| ePlaybackMode            | enum        | Looping mode                               |
| eSelectionOrder           | enum        | Permutation selection strategy             |
| bAlternatesAnims         | bool        | Whether permutations alternate             |
| ptPermutations[]         | AnimPermutation[] | The actual animation data containers  |
| arPermutationIndices[]   | int[]       | Playback order for permutations            |
| ptRepeatablePermutations[] | int[]     | Which permutations can repeat              |
| bHasPermutationGroups    | bool        | Grouping flag                              |

## AnimPermutation struct (hash 1358693725, 45 fields)

Each permutation is a complete animation variant. Key fields:

| Field              | Type    | Offset | Purpose                                       |
|--------------------|---------|--------|-----------------------------------------------|
| flFrameRate        | float   | 8      | Almost always 30.0 fps                        |
| flCompression      | float   | 12     | Modes 0-6; does not affect curve encoding     |
| dwBlendTime        | float   | 16     | Transition blend duration (seconds)           |
| nBoneCount         | int16   | 40     | Number of animated bones                      |
| ptPayloadData      | ext.    | 48     | → AnimPayloadData (in payload file)           |
| nKeyframeCount     | int     | 252    | Total keyframes in the animation              |
| nCycleCount        | uint    | 256    | Loop count (1 = plays once)                   |
| nPermutationGroup  | byte    | 248    | Group index for grouped permutations          |
| wvAvgVel           | vec3    | 312    | Average velocity (root motion hint)           |
| transInitialOffset | PRT     | 324    | Initial root offset (quat + position)         |
| pnAnimToStructure  | ext.    | 368    | Maps anim bone indices → skeleton bone indices|

### Permutation examples

**barM_HTH_nav_idle** (barbarian idle): 3 permutations, each with 190 bones:
- Perm 0: 61 frames, payload offset 32
- Perm 1: 121 frames, payload offset 27712
- Perm 2: 121 frames, payload offset 63104

**AMB_Camel_idle_01** (ambient camel): 3 permutations, each with 41 bones:
- Perm 0: 151 frames, Perm 1-2: 51 frames each

## AnimPayloadData struct (hash 2313381993, 208 bytes)

This is the critical struct. It lives in the **payload** file (not meta) and consists of 13 DT_VARIABLEARRAY headers, each 16 bytes: `[pad4][pad4][dataOffset:u32][dataSize:u32]`.

| Offset | Field                | Element Type       | Purpose                            |
|--------|----------------------|--------------------|------------------------------------|
| 0      | ptBoneNames          | BoneName (uint32)  | Bone hash per curve index          |
| 16     | unk_a9ead38          | float              | Unknown per-bone floats            |
| 32     | unk_8c9e18f          | float              | Unknown per-bone floats            |
| 48     | pwvNonlinearOffset   | vec3               | Per-frame nonlinear root offset    |
| 64     | unk_5cd81c8          | vec3               | Unknown per-frame vectors          |
| 80     | unk_fa7ecfb          | vec3               | Unknown per-frame vectors          |
| 96     | pflFacingYaw         | float              | Per-frame facing yaw (root motion) |
| 112    | pflRootScale         | float              | Per-frame root scale               |
| 128    | unk_7c60205          | float              | Unknown per-frame floats           |
| 144    | ptDepthOfField       | AnimDepthOfField   | fStop + focalDistance per frame     |
| 160    | ptTranslationCurves  | TranslationCurve   | Per-bone position keyframes        |
| 176    | ptRotationCurves     | RotationCurve      | Per-bone rotation keyframes        |
| 192    | ptScaleCurves        | ScaleCurve         | Per-bone scale keyframes           |

### Curve storage

TranslationCurve, RotationCurve, and ScaleCurve all share the same struct layout (16 bytes each):

```
TranslationCurve / RotationCurve / ScaleCurve  (size: 16 bytes)
  └── ptKeysComp: DT_VARIABLEARRAY of DT_BYTE  (raw byte blob)
```

The actual keyframe data is a raw byte array (`ptKeysComp`). Its encoding is documented under **Compression analysis** below — notably it does *not* vary with `flCompression`; the real format fork is keyframe-timestamp width (see the "All modes decode" subsection).

### Root motion fields

Several AnimPayloadData fields store per-frame root motion data separately from bone curves:
- `pwvNonlinearOffset` — vec3 per frame, nonlinear position offset for the root
- `pflFacingYaw` — float per frame, character facing angle
- `pflRootScale` — float per frame, root scale factor

These are likely stored as plain float arrays even when bone curves are compressed, since they're small (one value per frame, not per bone per frame).

## Compression analysis

### flCompression = 3 (vast majority of pre-expansion animations; expansion content uses the structurally-identical flCompression=5)

Observed payload sizes show heavy compression:

| Animation              | Bones | Frames | Payload chunk | Bytes/bone/frame |
|------------------------|-------|--------|---------------|------------------|
| barM_HTH_nav_idle p0   | 190   | 61     | 27,680 bytes  | ~2.4             |
| barM_HTH_nav_idle p1   | 190   | 121    | 35,392 bytes  | ~1.5             |

Uncompressed TRS would be ~40 bytes/bone/frame (quat16 + pos12 + scale12). The actual ~2 bytes/bone/frame implies:
- Constant curve elimination (static bones stored once, not per-frame)
- Delta encoding between frames
- Variable bit-width quantized values
- Possibly smallest-3 quaternion encoding (drop largest component)

### flCompression = 5 (byte-for-byte identical to comp=3)

Used in Vessel of Hatred / Lord of Hatred expansion content (Warlock,
likely Spiritborn — verify with that class's corpus). Reverse engineered
in May 2026 via cross-mode blob sharing: `warM_2HM_attk_apocalypse`
ships permutations declared as both comp=5 and comp=3 in the same .ani
payload, with 237 curve byte blobs physically aliased across the
boundary. Same bytes decode successfully under both flags — the
encoding cannot depend on flCompression. Confirmed across 5 sampled
files / 9 permutations / ~5,100 curves with 100% unit-length
quaternions.

**Decoder treatment:** route comp=5 through the existing comp=3 path.
No separate logic needed.

See `d4extract/docs/research-flcompression5.md` for the full evidence
and `d4extract/research/flcompression5/` for the analysis scripts and
extracted corpus.

### flCompression = 0 ("uncompressed")

Found in dozens of animation files (Chimera, Andariel, crowd, conversation anims). Even comp=0 uses ~12 bytes/bone/frame — NOT full 40-byte float TRS. This means comp=0 is still a compact representation, likely:
- int16 quantized values with per-bone range headers (6 bytes pos + 8 bytes quat = 14, close to observed ~12), or
- float16 half-precision values, or
- Quantized with identity-scale assumption (no per-frame scale data for most bones)

**The comp=0 files are the Rosetta Stone** — decode these first, then tackle comp=3.

### Known comp=0 files (good test cases)

| File                                    | Bones | Frames | Appearance          |
|-----------------------------------------|-------|--------|---------------------|
| CMP_dogLarge_ui_loadingScreen_pose_01   | 58    | 30     | cmp_base000_dogLarge|
| Chimera_lionsnake_attk_basic (perm 0)   | 134   | 43     | chimera_lionsnake   |
| Chimera_lionsnake_attk_basic (perm 1)   | 134   | 45     | chimera_lionsnake   |
| Andariel_attack_chain_line_1arm         | 279   | 321    | Andariel            |

### All modes decode — the real fork is timestamp width

Modes 1, 2, 4 and 6 are **implemented**, not uncharted. Cross-mode
curve-blob aliasing research (`d4extract/docs/research-flcompression1.md`,
extending the comp=5 work above) proved every mode 0-6 is byte-identical
for short-form animations: `flCompression` does **not** control curve
encoding at all. The decoder routes the whole range through one
short-form path — `_SUPPORTED_COMPRESSION = (0, 1, 2, 3, 4, 5, 6)` in
`anim_parser.py`, with no per-mode branching.

The genuine binary fork is **keyframe-timestamp width**, decided by
frame count rather than by `flCompression`:

- **Short-form (≤255 frames)** — timestamps are u8. Fully decoded.
- **Long-form (>255 frames)** — timestamps are u16 (a u8 cannot index
  frame 256+). The u16 layout is **not yet decoded**. `decode_permutation`
  rejects long-form permutations cleanly via the `LONG_FORM_FRAME_LIMIT`
  (255) guard rather than silently mis-decoding them — roughly 1,800
  long-form permutations ship in base data, and the guard surfaces them
  in the export failure dump as their own category. Implementing the u16
  layout is the remaining animation-decode work; see
  `d4extract/docs/research-flcompression1.md` for the analysis.

## TACT encryption status

Only **121 of 45,331** animation files are TACT-encrypted (0.27%). These are exclusively unreleased store cosmetics with blank names. All base-game animations (monsters, NPCs, bosses, player classes) are fully extractable.

Similarly, only 14 AnimSet files are encrypted. The AnimTree files (87 total) appear entirely unencrypted.

## Shared payload mappings

7,491 animation entries in `CoreTOCSharedPayloadsMapping.dat.json` point one .ani payload to another. This means many animations share the same physical payload file. When extracting, resolve through this mapping to get the actual payload path. Example:
```
barF_2HM_attk_primalAxe.ani → barF_2HP_attk_primalAxe.ani  (female barb shares payload)
morlu_swarmer_death_holy.ani → spider_adult_reac_death_crushed.ani  (reused death anim)
```

## AnimSetDefinition struct (hash 4188868250)

| Field              | Type                  | Purpose                              |
|--------------------|-----------------------|--------------------------------------|
| ptWeaponClasses    | int[]                 | Weapon class filter (0=any)          |
| eAnimsetType       | enum                  | Set type                             |
| eActorMountType    | enum                  | Mount type filter (-1 = not mounted) |
| ptPowerEntryList[] | AnimSetPowerEntry[]   | The action→animation mappings        |

## AnimTreeDefinition struct (hash 2423935966)

The AnimTree is a state machine that controls animation blending and transitions at runtime. It contains:
- `ptLeaf[]` — AnimTreeLeaf nodes (animation slots with blend weights, sync groups, layers)
- `ptNode[]` / `ptNodeBase[]` — blend nodes with child connections and blend parameters
- `ptBlendTriangles[]` — triangular blend spaces for locomotion

Each leaf can reference either a specific animation (`snoAnim`) or an animation key (`snoPowerAnimKey`) that gets resolved through the AnimSet at runtime. The tree is shared across many actors — `Player_AnimTree.ant` is used by all player classes.

For animation extraction, the AnimTree is useful context but not strictly required — the AnimSet provides the direct action→animation mapping.

## Reverse engineering plan

### Phase 1: Extract .ani payload files from CASC

Add SNO group 6 (Animation) to the rustydemon extraction pipeline. Pull a curated test set:

**Simplest cases** (2-3 bones, 2 frames):
- `Amazon_Prop_Chest_Common_Dyn_Neutral.ani` — chest idle, 2 bones, 2 frames, comp=3
- `Amazon_Prop_Chest_Common_Dyn_Open.ani` — chest opening, 2 bones, 2 frames, comp=3

**Uncompressed references** (comp=0):
- `CMP_dogLarge_ui_loadingScreen_pose_01.ani` — 58 bones, 30 frames
- `Chimera_lionsnake_attk_basic.ani` — 134 bones, 43+45 frames (2 permutations)

**Real-world complexity** (comp=3):
- `barM_HTH_nav_idle.ani` — 190 bones, 61+121+121 frames (3 permutations)
- `AMB_Camel_idle_01.ani` — 41 bones, 151+51+51 frames

### Phase 2: Decode flCompression=0 (the Rosetta Stone)

1. Parse the 208-byte AnimPayloadData header — 13 DT_VARIABLEARRAY entries, same format as other SNO payload headers
2. Follow each `[dataOffset, dataSize]` to locate the nested arrays in the payload
3. Read `ptBoneNames` to map curve indices to skeleton bone hashes
4. Read `ptTranslationCurves` / `ptRotationCurves` / `ptScaleCurves` — each bone gets one curve struct (16 bytes) with a `ptKeysComp` byte blob
5. Interpret the raw bytes. For comp=0, likely candidates:
   - int16 quantized with per-bone min/max range header
   - float16 half-precision TRS values
   - Fixed-point with implicit scale
6. **Validate:** reconstruct frame-0 bone transforms and compare to skeleton rest pose (`transformParentRel` from the .app file). An idle animation's first frame should be very close to the rest pose.

### Phase 3: Known-plaintext attack for flCompression=3

Use "Neutral" idle animations where frame 0 ≈ skeleton rest pose. The rest pose is known from the .app payload's `transformParentRel`. Compare compressed byte streams against expected float values to identify:
- Per-bone or per-curve quantization headers (min/max/scale/bias)
- Bit-packing layout for quaternions (smallest-3 is common in Blizzard engines)
- Delta encoding between consecutive frames
- Constant curve flags (bones that don't move)
- Curve type discriminators (const / linear / hermite / bezier)

The ~2 bytes/bone/frame for comp=3 vs ~12 bytes/bone/frame for comp=0 implies roughly 4-6x additional compression, consistent with constant curve elimination + delta encoding.

### Phase 4: Cross-validate with Blizzard engine precedent

D3's M2 `.anim` format and WoW's animation system are documented on wowdev.wiki. D4's architecture already mirrors D3's separation of translation/rotation/scale into distinct curve arrays. Check:
- WoW M2 animation compression specs (quantized quaternions with range headers)
- D3 animation format documentation from modding community
- Blizzard's known use of variable bit-width delta encoding
- SStrHash / animation naming conventions

### Phase 5: Progressive validation

Validate decoded animations by writing glTF animation channels and importing into Blender on already-exported skeletons:

1. **2-bone chest opening** — simplest possible, verify basic curve decode
2. **58-bone dog pose** — verify multi-bone hierarchy
3. **134-bone chimera attack** — verify combat animation with comp=0
4. **190-bone barbarian idle** — verify player skeleton with comp=3

For each: check bones don't explode, motion is smooth, loops match `nCycleCount`, timing matches `flFrameRate`.

Use `ptBoneNames` hashes to map animation curves to skeleton bones. Use `pnAnimToStructure` for any index remapping between animation bone order and skeleton bone order.

## Key d4data file locations

| Data                      | Path in d4data                                    |
|---------------------------|---------------------------------------------------|
| Animation meta JSONs      | `json/base/meta/Anim/*.ani.json`                  |
| AnimSet meta JSONs        | `json/base/meta/AnimSet/*.ans.json`               |
| AnimTree meta JSONs       | `json/base/meta/AnimTree/*.ant.json`              |
| Actor definitions         | `json/base/meta/Actor/*.acr.json`                 |
| PlayerClass definitions   | `json/base/meta/PlayerClass/*.pcl.json`           |
| Shared payload mapping    | `json/base/CoreTOCSharedPayloadsMapping.dat.json` |
| Encrypted SNO list        | `json/base/EncryptedSNOs.dat.json`                |
| SNO group IDs             | `json/snoGroups.json` (Animation=6, AnimSet=8, AnimTree=67) |
| Struct definitions        | `definitions.json` (keyed by type hash)           |
| Bone name dictionary      | `dict.txt` (32K words for hash brute-force)       |

## Key type hashes (definitions.json)

| Hash       | Name                  | Purpose                       |
|------------|-----------------------|-------------------------------|
| 880817737  | AnimationDefinition   | .ani meta file root struct    |
| 1358693725 | AnimPermutation       | Per-permutation anim data     |
| 2313381993 | AnimPayloadData       | Payload struct (curves + root)|
| 592804084  | TranslationCurve      | Position curve (ptKeysComp)   |
| 2977136533 | RotationCurve         | Rotation curve (ptKeysComp)   |
| 1252497901 | ScaleCurve            | Scale curve (ptKeysComp)      |
| 4188868250 | AnimSetDefinition     | .ans root struct              |
| 977570672  | AnimSetPowerEntry     | Action→animation mapping      |
| 2423935966 | AnimTreeDefinition    | .ant root struct              |
| 3085005858 | ActorDefinition       | .acr root struct              |
| 1365297765 | BoneName              | 4-byte uint32 bone hash       |
| 894232435  | AnimDepthOfField      | fStop + focalDistance          |
| 3763372188 | AnimContactFrame      | Foot contact frame marker     |
