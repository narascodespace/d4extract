# Research: flCompression=1 (and a Check on Modes 2, 4, 6)

**Date:** 2026-05-17
**Status:** Conclusive — comp=1/2/4/6 are byte-identical to comp=3 for
short-form (≤255-frame) animations. A separate, mode-independent
**long-form variant** (u16 timestamps, >255 frames) was uncovered.
**Author:** decoder research session (no production code changed)

---

## Executive summary

`flCompression=1`, `2`, `4` and `6` all use **the same curve byte format
as `flCompression=3`** — for short-form animations (≤255 frames, which is
all 10 comp=1 player-content failures). Same header, same `[count][fmt]`
curve headers, same int16-quaternion encoding, same u8 timestamps. Just
like comp=5, `flCompression` does **not** affect curve encoding at all.

Proof — same trick as comp=5 — **cross-mode blob sharing**: in
`spiF_gla_nav_evade`, `druM_2HS_attk_StoneBurst`,
`palM_1hsShd_attk_impale`, `druM_HTH_trav_ladder_climb_down`,
`warlock_tailStrike_attk_basic` and `bandit_sword_nav_run`, curve byte
blobs are **physically aliased** (same payload offset+size) across the
comp=1↔comp=3, comp=5↔comp=1, comp=6↔comp=3 and comp=2↔comp=3
boundaries. A single sequence of bytes is consumed by two
different-`flCompression` permutations — so the encoding cannot depend
on `flCompression`.

**But the research uncovered a second, orthogonal finding.** The curve
format has a **long-form variant**: animations with `nKeyframeCount > 255`
store timestamps as **u16** instead of u8 (a u8 cannot index frame 256+).
This is **mode-independent** — observed in a comp=3 animation
(`Amalgam_reac_Death`, 271 frames) exactly as in a comp=1 IGC clip
(`IGC_CBH_t3barbarianm_1010`, 2601 frames). The shipping decoder
hard-codes u8 timestamps and therefore **already silently mis-decodes
~1,820 long comp=3 animations today** — a pre-existing latent bug
unrelated to comp=1.

**Recommendation: collapse the comp=1/2/4/6 fix into one combined
implementation prompt** — same workflow as comp=5 — but it must do *two*
things, not one: (a) widen `_SUPPORTED_COMPRESSION` to `(0,1,2,3,4,5,6)`,
and (b) add a `nKeyframeCount > 255` guard that raises a clean error.
Without (b), widening the comp=1 guard would turn long comp=1 clips (IGC)
from a clean failure into silent garbage. Implementing the u16 long-form
decoder itself is a **separate, larger follow-up** (well-characterised
here, ~4,138 permutations across all modes would be recovered).

---

## 1. Test corpus

Extracted via `RustyDemonCLI.extract_anim_pair_batch` (one CASC open;
game install `C:\Program Files (x86)\Diablo IV`, rustydemon-cli 0.3.3)
into `d4extract/research/flcompression1/extracted/`. Meta JSONs read from
d4data `base/meta/Anim/`.

### Primary — comp=1 player content (the 10 Character Builder failures)

| Animation | comp | Bones | Frames (per perm) | Payload |
|-----------|-----:|------:|-------------------|--------:|
| `barM_mount_hth_event_dismount_damage` | 1 | 188 | 31 | 46,816 B |
| `warM_mount_horse_reac_dismount` | 1 | 190 | 32 | 61,368 B |
| `spiF_mount_hth_event_dismount_damage` | 1 | 190 | 31 | 48,676 B |
| `spiF_gla_attk_centipede_core` | 1 | 190 | 56 | 44,768 B |
| `spiF_gla_attk_gorillaDefensive` | 1 | 190 | 54 | 37,116 B |
| `spiF_gla_attk_sky_basic_new` | 1 | 190 | 51/53/65 | 137,172 B |
| `spiF_gla_attk_Plains_Offense_temp` | 1 | 190 | 11/62 | 62,416 B |
| `spiF_gla_attk_guardianImpact_repeat` | 1 | 190 | 86/86 | 117,820 B |
| `spiF_gla_nav_evade` | **1+3** | 190 | 6/60/6/55 | 123,068 B |
| `spiF_hth_emote_taunt` | 1 | 190 | 101 | 81,008 B |

All 10 are **short-form** (≤101 frames).

### Secondary — IGC sample, cross-mode candidates, comp=2/4/6 samples

| Animation | comp (per perm) | Bones | Note |
|-----------|-----------------|------:|------|
| `IGC_CBH_t3barbarianm_1010` | 1 | 190 | IGC cinematic, **2601 frames** |
| `druM_2HS_attk_StoneBurst` | 1,1,1,3 | 191 | cross-mode (Druid player) |
| `palM_1hsShd_attk_impale` | 5,1 | 193 | cross-mode (comp=5 + comp=1) |
| `druM_HTH_trav_ladder_climb_down` | 3,1,3 | 191 | cross-mode |
| `bandit_sword_nav_run` | 2,3,3 | 83 | comp=2 sample, cross-mode |
| `bandit_sword_nav_walk` | 2 | 83 | comp=2 sample, pure |
| `treasuregoblin_nav_idle_unalert_outro_still` | 4 | 67 | the **only** comp=4 file |
| `warlock_tailStrike_attk_basic` | 6,6,3,3,3 | 17 | comp=6 sample, cross-mode |
| `Amalgam_reac_Death` | 3 | 133 | **long comp=3** (271 frames) verification |

Baselines reused from the comp=5 research corpus: `barM_HTH_nav_idle`
(comp=3 reference). Skeletons `barM_base00`, `warM_base00`, `spiF_base00`
extracted for the rest-pose test.

Raw payloads + analysis scripts under `d4extract/research/flcompression1/`
(~9 MB). Same gitignore-or-commit decision as the comp=5 corpus —
recommend gitignoring `extracted*/` and keeping the four scripts +
`results.txt` + this report. Reproduce with:

```
python research/flcompression1/extract.py     # pull payloads
python research/flcompression1/analyze.py     # header, byte counts, cross-mode
python research/flcompression1/valuetest.py   # decode + rest-pose
python research/flcompression1/hexdump.py     # annotated dumps
```

---

## 2. AnimPayloadData header confirmation

**The 208-byte header parses identically for comp=1 as for comp=3/5.**
The compression-agnostic `parse_anim_payload_header` parsed every
permutation in the corpus without error. Detailed dump,
`spiF_gla_attk_centipede_core` p0 (comp=1, 190 bones, 56 frames):

```
  [ 0] ptBoneNames          off=      240 size=    760 (190x4B)
  [ 1] unk_a9ead38          off=     1000 size=    760 (190x4B)
  [ 2] unk_8c9e18f          off=        0 size=      0 (0x4B)
  [ 3] pwvNonlinearOffset   off=     1760 size=    672 (56x12B)
  [ 4] unk_5cd81c8          off=     2432 size=    672 (56x12B)
  [ 5] unk_fa7ecfb          off=     2432 size=    672 (56x12B)
  ...
  [10] ptTranslationCurves  off=     3776 size=   3040 (190x16B)
  [11] ptRotationCurves     off=    20520 size=   3040 (190x16B)
  [12] ptScaleCurves        off=    52776 size=   3040 (190x16B)
```

13 `DT_VARIABLEARRAY` records, 190×16 B curve-descriptor arrays — same
shape as the comp=3/5 baselines. No structural deviation.

---

## 3. Shared-payload analysis

**Hypothesis (from the task): the three classes' `mount_*_dismount`
anims physically alias one payload. Result: DISPROVEN.**

`payload_resolver.resolve_payload_path(..., sno_group="Anim")` for each:

```
  barM_mount_hth_event_dismount_damage   -> None
  warM_mount_horse_reac_dismount          -> None
  spiF_mount_hth_event_dismount_damage    -> None
```

`None` = each has its own same-name payload, not aliased to another
animation. `extract_anim_pair_batch` independently reported
`shared=False` for all three. The dismount anims were authored
per-class; the "every class fails its dismount" pattern is just because
mount/dismount reactions are short, late-authored clips that happened to
be tagged comp=1 — not a shared asset.

---

## 4. Cross-mode blob sharing — the smoking gun

Within a single payload, this counts curve descriptors whose
`(dataOffset, dataSize)` are **identical** across permutations of
*different* `flCompression`. An identical pointer means both permutations
read the **same physical bytes**.

```
spiF_gla_nav_evade            modes [1,3,3,3]
  p0(c1) vs p1(c3): T=139/190  R=102/177  S=0/1     <== ALIASED
  p0(c1) vs p2(c3): T=148/190  R=133/177  S=1/1     <== ALIASED
  p0(c1) vs p3(c3): T=137/190  R=102/177  S=0/1     <== ALIASED

druM_2HS_attk_StoneBurst      modes [1,1,1,3]
  p0(c1) vs p3(c3): T=137/191  R=99/175   S=0/1     <== ALIASED
  p1(c1) vs p3(c3): T=140/191  R=99/175   S=0/1     <== ALIASED

palM_1hsShd_attk_impale       modes [5,1]
  p0(c5) vs p1(c1): T=141/193  R=87/170   S=0/0     <== ALIASED

druM_HTH_trav_ladder_climb_down  modes [3,1,3]
  p0(c3) vs p1(c1): T=7/191    R=17/179   S=0/0     <== ALIASED

warlock_tailStrike_attk_basic modes [6,6,3,3,3]
  p0(c6) vs p2(c3): T=5/17     R=4/17     S=0/0     <== ALIASED

bandit_sword_nav_run          modes [2,3,3]
  p0(c2) vs p1(c3): T=45/83    R=24/79    S=0/0     <== ALIASED
```

A comp=1 permutation and a comp=3 permutation each name the **same byte
range** as their curve data. The same holds for comp=5↔comp=1,
comp=6↔comp=3 and comp=2↔comp=3. The engine could not legally alias one
blob to two permutations if those permutations decoded curve bytes
differently. **comp=1, 2 and 6 use the comp=3 curve encoding.** (comp=4
has only one file in all of d4data, with no sibling comp=3 permutation to
alias against — covered by §5–§7 instead.)

---

## 5. Byte-count comparison table

`bytes/bone/frame = (T + R + S curve bytes) / (bone_count × frame_count)`.

| File | comp | Bones | Frames | Trans B | Rot B | Scale B | B/bone/frame |
|------|-----:|------:|-------:|--------:|------:|--------:|-------------:|
| `barM_HTH_nav_idle` p0 (ref) | **3** | 190 | 61 | 4,156 | 12,540 | 548 | **1.49** |
| `barM_mount_hth_event_dismount_damage` p0 | **1** | 188 | 31 | 13,264 | 20,592 | 1,720 | **6.10** |
| `warM_mount_horse_reac_dismount` p0 | **1** | 190 | 32 | 19,060 | 27,256 | 3,744 | **8.23** |
| `spiF_mount_hth_event_dismount_damage` p0 | **1** | 190 | 31 | 11,964 | 22,812 | 2,380 | **6.31** |
| `spiF_gla_attk_centipede_core` p0 | **1** | 190 | 56 | 7,088 | 29,700 | 0 | **3.46** |
| `spiF_gla_attk_guardianImpact_repeat` p0 | **1** | 190 | 86 | 10,852 | 43,456 | 0 | **3.32** |
| `spiF_hth_emote_taunt` p0 | **1** | 190 | 101 | 19,824 | 48,188 | 2,956 | **3.70** |
| `spiF_gla_nav_evade` p0 | **1** | 190 | 6 | 4,624 | 4,060 | 16 | **7.63** |
| `spiF_gla_nav_evade` p1 | **3** | 190 | 60 | 9,648 | 28,208 | 276 | **3.35** |
| `IGC_CBH_t3barbarianm_1010` p0 | **1** | 190 | 2601 | 325,252 | 830,664 | 0 | **2.34** |
| `bandit_sword_nav_run` p0 | **2** | 83 | 25 | 5,088 | 9,232 | 0 | **6.90** |
| `treasuregoblin_..._outro_still` p0 | **4** | 67 | 33 | 5,192 | 14,208 | 816 | **9.14** |
| `warlock_tailStrike_attk_basic` p0 | **6** | 17 | 75 | 3,948 | 4,640 | 0 | **6.74** |

**Reading:** comp=1/2/4/6 sit at **2.3–9.1 bytes/bone/frame** — the same
band as comp=3 (the high values are short clips where the fixed
per-curve header amortises over few frames; `spiF_gla_nav_evade` p0 is 6
frames). Crucially, the comp=3 permutations of the *same files* land in
the same range (`spiF_gla_nav_evade` p1 = 3.35). Note the IGC clip's
density (2.34) is perfectly normal — **byte-count alone does not flag the
long-form variant**; only the structural probe (§6) and decode test (§7)
do.

A structural size-equation probe (does each blob satisfy the comp=3
`count`-driven size formulas?) found comp=1 player files behave
**identically to the comp=3 baseline** — same OK / off-by-trailer
distribution. The comp=3 reference itself "mismatches" the simplified
probe formula at the same rate, confirming the probe imperfection is
mode-independent. The IGC clip is the sole outlier — see §6C.

---

## 6. Hex dumps

### 6A. A curve blob aliased across the comp boundary

`spiF_gla_nav_evade` bone[0] translation curve. The descriptor in the
comp=1 permutation (p0) **and** in a comp=3 permutation (p1) both point
at payload offset **4800**, size 16:

```
  comp=1 p0 descriptor: offset=4800 size=16
  comp=3 p1 descriptor: offset=4800 size=16   <-- SAME physical bytes

  000012c0  01 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00  |................|
            ^^ ^^ ^^^^^ <----- f32 baseline (0,0,0) ----->
            |  fmt=0x04
            count=1 -> constant curve
```

One physical 16-byte blob, named by both a comp=1 and a comp=3
permutation. This is the proof in §4 at the byte level.

### 6B. comp=1 vs comp=3 — identical curve structure

A `count≥2` rotation curve from each. Both follow the comp=0/3 layout
`[u16 count][u8 timestamps[count-1]][int16 quat xyzw × count][u16 trailer]`:

```
comp=1  spiF_gla_attk_centipede_core p0  bone[1] rotation (452 B, count=50)
  00003ab8  32 00 01 02 03 04 05 06 07 08 09 0a 0b 0c 13 14  |2...............|
            ^^^^^ <-------- u8 timestamps (single bytes) -----
  00003ad8  25 26 27 28 29 2a 2b 2c 2d 2e 2f 30 31 32 33 34  |%&'()*+,-./01234|
  00003ae8  35 36 37 00 d9 f9 10 e8 35 70 67 38 ...          (quat data)

comp=3  barM_HTH_nav_idle p0           bone[7] rotation (148 B, count=16)
  00002f10  10 00 02 03 04 09 15 1a 1b 1c 1f 21 27 33 38 39  |...........!'389|
            ^^^^^ <----- u8 timestamps ----->
  00002f20  3c 00 97 f5 e3 01 29 7f 0a 0a ...                (quat data)
```

`count` is a u16, timestamps are **single bytes (u8)**, quaternions are
int16 xyzw. Identical layout, different file.

### 6C. The IGC anomaly — `IGC_CBH_t3barbarianm_1010` (2601 frames)

This comp=1 clip does **not** match the short-form layout:

```
first rotation curves — count(u16) vs comp3-u8-size-prediction:
  bone[0]: size= 11284  count= 1128  u8-predict= 10156  mismatch
  bone[2]: size= 15264  count= 1526  u8-predict= 13738  mismatch
  bone[5]: size= 15904  count= 1590  u8-predict= 14314  mismatch

bone[0] rotation curve head:
  0003fb40  68 04 00 00 02 00 04 00 06 00 07 00 08 00 0a 00  |h...............|
            ^^^^^ ^^^^^ ^^^^^ ^^^^^ ^^^^^ <-- u16 timestamps: 0,2,4,6,7,8,10
            count  ts[0] ts[1] ts[2] ...
            =1128
```

The timestamps are **u16, not u8** — `00 00`, `02 00`, `04 00`… The size
checks out exactly as `2 + count×2 (u16 ts) + count×8 (quats) + 2
(trailer)` = `2 + 2256 + 9024 + 2 = 11284`. The IGC clip is **2601
frames** — a u8 timestamp tops out at 255 and physically cannot index
frame 256+, so a long animation *must* widen its timestamps.

### 6D. The long-form variant is NOT comp=1-specific

`Amalgam_reac_Death` — a **comp=3** animation, 271 frames:

```
  bone[1] rotation: size=1984 count=198
   first 24 bytes: c6 00 00 00 01 00 02 00 03 00 04 00 05 00 06 00 ...
                   count  ts0   ts1   ts2   ts3   ts4   ts5  <- u16 timestamps
```

`size 1984 = 2 + 198×2 + 198×8 + 2`. A **comp=3** animation using **u16
timestamps**, identical long-form layout to the comp=1 IGC clip. The
long-form variant is selected by **frame count, not compression mode**.

---

## 7. Known-plaintext attack (rest-pose validation)

Each comp=1 player anim was decoded through the production
`decode_permutation` (with `compression` overridden to 3 in memory) and
frame 0 compared against the matching skeleton's rest pose:

| comp=1 anim | T match-rest (<1e-3) | T median err | R match-rest (<1°) |
|-------------|---------------------:|-------------:|-------------------:|
| `barM_mount_hth_event_dismount_damage` | 176/188 | 1.2e-06 | 125/188 |
| `warM_mount_horse_reac_dismount` | 177/190 | 2.0e-06 | 124/190 |
| `spiF_mount_hth_event_dismount_damage` | 179/190 | 9.4e-07 | 105/190 |
| `spiF_gla_attk_centipede_core` | 185/190 | 1.9e-06 | 119/190 |
| `spiF_gla_attk_guardianImpact_repeat` | 184/190 | 1.9e-06 | 135/190 |
| `spiF_hth_emote_taunt` | 184/190 | 1.9e-06 | 119/190 |

~180/190 bones match the rest pose at **micron precision** (median error
~1e-6). These are attacks/dismounts/emotes, so frame 0 is not fully at
rest — the static bones match exactly, the animated ones don't, exactly
the profile seen for comp=3/comp=5 attack anims in prior research.

**Decode test** — every comp=1/2/4/6 permutation of player content
decoded through the comp=3 decoder with **0 non-unit quaternions and 0
per-curve fallbacks** (13 comp=1 player perms + 9 comp=2/4/6 perms +
cross-mode files). **Cross-permutation constants:** in
`spiF_gla_nav_evade`, **139/139 translation and 102/102 rotation**
`count=1` curve baselines are byte-identical between the comp=1 and
comp=3 permutations.

The lone exception, again, is the IGC clip: decoded as comp=3 it yields
**54,637 / 494,190 non-unit quaternions (11%)** — the decoder reading u16
timestamps as u8, landing the quaternion reads at wrong offsets.

---

## 8. Compression-mode prevalence (modes 2, 4, 6) + long-form scan

Scan of all **45,333** `.ani.json` files in d4data:

| Mode | Permutations | Files | Verdict |
|-----:|-------------:|------:|---------|
| 0 | 8,272 | 8,167 | uncompressed (documented) |
| 1 | 489 | 476 | **= comp=3** (this research) |
| 2 | 50 | 34 | **= comp=3** (cross-mode aliasing, §4) |
| 3 | 46,674 | 36,266 | quantized (documented) |
| 4 | **1** | **1** | only `treasuregoblin_nav_idle_unalert_outro_still` |
| 5 | 712 | 587 | = comp=3 (prior research) |
| 6 | **2** | **1** | only `warlock_tailStrike_attk_basic` — **not in the original task scope; surfaced by the scan** |

- **comp=2** (50 perms): sampled `bandit_sword_nav_run` (cross-mode
  aliased with comp=3) and `bandit_sword_nav_walk` — both decode cleanly
  as comp=3. = comp=3.
- **comp=4** (1 file): `treasuregoblin_nav_idle_unalert_outro_still` —
  decodes cleanly as comp=3 (0 non-unit quats). = comp=3.
- **comp=6** (1 file): `warlock_tailStrike_attk_basic` — comp=6
  permutations are cross-mode aliased with this file's own comp=3
  permutations (§4). = comp=3. *Modes beyond 5 were not requested; comp=6
  is reported because the scan found it.*

**Long-form scan** — permutations with `nKeyframeCount > 255`:

```
total >255-frame permutations: 4,138   (IGC-prefixed: 2,304; non-IGC: 1,834)
  by compression mode: {0: 1943, 1: 373, 2: 1, 3: 1820, 5: 1}
max nKeyframeCount per mode: {0:5400, 1:3906, 2:300, 3:4211, 4:33, 5:521, 6:75}
```

**1,820 comp=3 permutations exceed 255 frames** — the shipping decoder
accepts them (comp=3 is supported) and decodes them with the u8-timestamp
assumption, i.e. **silently mis-decodes all 1,820 today**. Sample
non-IGC long animations: `Amalgam_reac_Death` (271 f), `Amalgam_event_
Sleeping_Awaken` (310 f), `Amazon_Prop_Ballista_Loop_*_Dyn_Idle`
(301 f), `ambient_dog_nav_emote` (261 f) — props, monsters, ambient.

---

## 9. Hypothesis

The curve byte format is governed by **two axes, and `flCompression` is
neither of them**:

1. **`flCompression` (0/1/2/3/4/5/6) does not affect curve encoding.**
   comp=1, 2, 4, 6 are byte-identical to comp=3 — proven by cross-mode
   blob aliasing (§4), clean decode (§7), and identical constant-curve
   baselines (§7). What `flCompression` actually signifies (exporter
   version, a playback/blend hint) remains unknown and irrelevant to
   decoding — same conclusion as the comp=5 research.

2. **Timestamp width depends on `nKeyframeCount`:**
   - **Short-form (≤255 frames):** u8 timestamps. `[u16 count]
     [u8 ts[count-1]][int16 quat×count][u16 trailer]` — the format the
     decoder already implements.
   - **Long-form (>255 frames):** u16 timestamps. `[u16 count]
     [u16 ts[count]][int16 quat×count][u16 trailer]` — the u16 variant
     stores `count` timestamps (frame 0 explicit) vs the u8 variant's
     `count-1` (frame 0 implicit). Otherwise identical. This is
     **mode-independent** (§6C/§6D: observed in both comp=1 IGC and
     comp=3 `Amalgam_reac_Death`).

**Confidence: ~97%** for "comp=1/2/4/6 short-form = comp=3 short-form" —
the cross-mode aliasing is a near-proof and every short-form decode test
passed. The 3% residual is the usual "an unsampled file does something
odd"; closed the same way comp=5's was — by a verification re-decode of
the full player comp=1 surface in the implementation step.

**All 10 comp=1 player-content failures are short-form** (≤101 frames).
They fail today purely because of the `_SUPPORTED_COMPRESSION` guard.

The u16 long-form layout is characterised but **not 100% nailed down** —
the exact timestamp count (`count` vs `count-1`) and translation/scale
long-form headers want one more verification pass before *implementing*
that decoder. It is not needed for the player-content fix.

---

## 10. Recommended implementation strategy

**Collapse the comp=1/2/4/6 fix into ONE combined implementation prompt**
(same as comp=5). Estimated effort **~1–1.5 hours**. It must do *two*
things:

1. **Widen the guard:** `_SUPPORTED_COMPRESSION = (0, 1, 2, 3, 4, 5, 6)`
   in `anim_parser.py`. comp=1/2/4/6 route through the unchanged comp=3
   path. This fixes all 10 player failures (Barb 1, Warlock 1,
   Spiritborn 8).

2. **Add a `nKeyframeCount > 255` guard** in `decode_permutation` that
   raises a clear error (e.g. `"long-form (u16-timestamp) animation not
   yet supported: <name> has N frames"`). This is **not optional**:
   - Without it, widening the comp=1 guard turns long comp=1 clips (the
     373 long comp=1 perms, mostly IGC) from a clean "unsupported
     flCompression" failure into **silent garbage** if loaded via the
     Model Browser — a regression.
   - It also converts the **pre-existing** comp=3 long-form silent
     garbage (1,820 perms) into honest, visible failures.
   - It is mode-independent and correct: a u8-timestamp decoder genuinely
     cannot read a >255-frame animation.

3. **Tests:** mirror the comp=5 tests — synthetic comp=1/2/4/6
   permutations decode identically to comp=3; modes still unsupported
   stay rejected; a `nKeyframeCount > 255` permutation raises the new
   guard's error. A real-payload integration test against this research
   corpus.

4. **Verify:** decode-test the full comp=1 player surface (all `barM_`/
   `warM_`/`spiF_` short comp=1 anims) — expect 0 failures — and confirm
   the Character Builder export decode-failure count drops to ~0 for
   Barb/Warlock and ~0 for Spiritborn. The Character Builder already
   filters IGC via `is_player_anim`, so the long-form guard should not
   fire during a normal builder export.

**Separate, larger follow-up (do NOT fold into the above):** implement
the **u16 long-form decoder**. This research characterised it (§6C/§6D)
but it needs its own verification pass and is a real decode change, not a
guard widen. Payoff is large — ~4,138 permutations across all modes
(2,304 IGC + 1,834 non-IGC, including 1,820 comp=3 animations the decoder
mis-handles today). Medium effort; worth its own research-then-implement
cycle.

---

## 11. Open questions

- **The exact u16 long-form layout.** §6C/§6D show `[u16 count]
  [u16 ts×count][quat×count][u16 trailer]` for rotation, but the
  timestamp count (`count` vs `count-1`) and the translation/scale
  long-form headers need one more verification pass before the long-form
  decoder is implemented.
- **What `flCompression` actually means.** Still unknown (carried over
  from the comp=5 research). Not needed to decode; worth a line if it
  ever surfaces — likely an exporter-version or playback-layer tag.
- **comp=6** was not in the task scope; the scan found exactly one file
  (`warlock_tailStrike_attk_basic`). It is comp=3-identical. Folding `6`
  into `_SUPPORTED_COMPRESSION` costs nothing and avoids a future
  one-file failure, so the recommendation includes it — but it is a
  single data point.
- **The pre-existing comp=3 long-form bug.** 1,820 comp=3 animations
  >255 frames are mis-decoded today. The recommended frame-count guard
  makes them fail *cleanly*; actually decoding them needs the follow-up.
  Worth checking whether any are reachable from a normal Character
  Builder export (player skeletons) — the sampled long anims are props/
  monsters/ambient, so probably not, but confirm.
- **comp=1 IGC clips** remain undecodable after this fix (long-form).
  They are filtered from the Character Builder by `is_player_anim`, so
  this is acceptable — but a Model Browser user loading one directly will
  now get the new clean long-form error instead of the old
  unsupported-compression error. Equivalent UX, just a different message.

---

## Surprises / notes for the next session

- **The headline surprise: the format's real variable is frame count,
  not `flCompression`.** The task framed comp=1 as a compression mode to
  reverse-engineer; it turned out comp=1 short-form *is* comp=3, and the
  genuine format fork (u8 vs u16 timestamps) is orthogonal to
  `flCompression` entirely.
- **The shipping decoder already has a latent bug** on 1,820 long comp=3
  animations — found incidentally. Not caused by anything in this work;
  flagged for the follow-up.
- The three `mount_*_dismount` anims do **not** share payloads (§3) — the
  task's aliasing hypothesis was wrong; they were just authored per-class
  as short comp=1 clips.
- **comp=6 exists** (1 file) — outside the task's "check 2 and 4" scope.
- comp=2/4/6 are all comp=3-identical; comp=4 is a single file in all of
  d4data and comp=6 is a single file — these modes are vanishingly rare.
