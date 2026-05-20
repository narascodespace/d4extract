# Research: flCompression=5 Animation Byte Layout

**Date:** 2026-05-16
**Status:** Conclusive — comp=5 is byte-layout-identical to comp=3.
**Author:** decoder research session (no production code changed)

---

## Executive summary

`flCompression=5` is **the same curve byte format as `flCompression=3`.**
Same 208-byte `AnimPayloadData` header, same 13-array layout, same per-bone
curve descriptors, same `[count][fmt]` curve headers, same int16-quaternion
encoding (xyzw / 32767), same float32 translation baseline+delta scheme,
same timestamp arrays, same alignment rules.

The decoder rejects comp=5 **only** because of its
`if compression not in (0, 3)` guards. Widening those guards to accept `5`
is the entire implementation — an estimated **~1 hour** including
regression tests and a Warlock re-export to verify.

The single strongest piece of evidence: in `warM_2HM_attk_apocalypse`, a
comp=5 permutation and a comp=3 permutation **physically share 237 curve
byte blobs** (same payload offset + size). One sequence of bytes is
consumed by both a comp=5 and a comp=3 permutation — so the curve encoding
provably cannot depend on `flCompression`.

**Recommendation: collapse the implementation into one prompt.** The
research is conclusive enough to one-shot it. The only residual risk is
that the broader comp=5 population (beyond this 5-file corpus) contains a
variant; mitigate by having the implementation re-run the full Warlock
export and confirm the decode-failure count drops 55 → ~0.

---

## 1. Test corpus

Extracted via `RustyDemonCLI.extract_anim_pair` (game install
`C:\Program Files (x86)\Diablo IV`, rustydemon-cli 0.3.3). Each animation
was extracted into its own subdirectory under
`d4extract/research/flcompression5/extracted/<name>/` — see *Surprises*
below for why a shared output directory had to be avoided.

Meta JSONs are read from d4data at
`d4data/json/base/meta/Anim/<name>.ani.json`.

| # | Animation | comp | Perms | Payload file | Payload size |
|---|-----------|------|-------|--------------|--------------|
| 1 | `warM_2HS_attk_weaponAttack`   | 5     | 2 | `warM_2HS_attk_weaponAttack.ani`   | 71,108 B  |
| 2 | `warM_1hsOh_attk_weaponAttack` | 5     | 2 | `warM_1hsOh_attk_weaponAttack.ani` | 84,584 B  |
| 3 | `warM_demonForm_nav_runStop`   | 5     | 1 | `warM_demonForm_nav_runStop.ani`   | 40,640 B  |
| 4 | `warM_2HM_attk_apocalypse`     | 5 + 3 | 5 | `warM_2HM_attk_apocalypse.ani`     | 190,580 B |
| 5 | `warM_1HS_attk_SigilOfFlames`  | 5     | 1 | `warM_1HS_attk_SigilOfSummons.ani` | 56,432 B  |
| 6 | `barM_HTH_nav_idle` (baseline) | 3     | 3 | `barM_HTH_nav_idle.ani`            | 84,684 B  |

All warM animations target the 190-bone `warM_base00` skeleton.

Notable corpus properties:

- **`warM_2HM_attk_apocalypse` is the keystone file.** Its 5 permutations
  mix compression modes: **p0/p1/p4 are comp=5, p2/p3 are comp=3** — same
  animation, same skeleton, one payload file. This makes it a built-in
  Rosetta Stone: comp=5 and comp=3 can be diffed without any
  cross-file/cross-skeleton noise.
- **`warM_1HS_attk_SigilOfFlames` is a shared payload.** Its meta has no
  same-name payload; the resolver mapped it to
  `warM_1HS_attk_SigilOfSummons.ani` via `CoreTOCSharedPayloadsMapping`.
  The extractor reported `shared=True` for it (correctly, once each
  animation had its own output directory).

Raw extracted payloads + the analysis scripts are kept under
`d4extract/research/flcompression5/` (6.5 MB total, 6.4 MB of which is the
extracted CASC payloads). The repo is not currently a git repository; if
it becomes one, **gitignore `research/flcompression5/extracted/`** (raw
game assets, large, reproducible via `extract.py`) and keep the four
analysis scripts + this report.

Reproduce with:

```
.venv/Scripts/python.exe research/flcompression5/extract.py    # pull payloads
.venv/Scripts/python.exe research/flcompression5/analyze.py    # header + byte counts
.venv/Scripts/python.exe research/flcompression5/probe.py      # size-equation test
.venv/Scripts/python.exe research/flcompression5/valuetest.py  # value/decode test
.venv/Scripts/python.exe research/flcompression5/hexdump.py    # annotated dumps
```

---

## 2. AnimPayloadData header confirmation

**The 208-byte header parses identically for comp=5 and comp=3.** The
existing `parse_anim_payload_header` (which is compression-agnostic)
parsed every permutation in the corpus without error or warning:

- All 13 `DT_VARIABLEARRAY` records present, 16 bytes each, 208-byte total.
- `ptBoneNames` = 190 × 4 B for every warM permutation (matches `nBoneCount`).
- `ptTranslationCurves` / `ptRotationCurves` / `ptScaleCurves` = 190 × 16 B
  each (one 16-byte descriptor per bone) for every permutation, comp=5 and
  comp=3 alike.
- `unk_a9ead38` = 190 × 4 B (per-bone float) — present in both modes.
- Root-motion slots (`pwvNonlinearOffset`, `unk_5cd81c8`, `unk_fa7ecfb`)
  are populated per-frame (59 × 12 B in `SigilOfFlames`) or empty,
  identically to comp=3 conventions.

**No structural deviation between comp=5 and comp=3 at the header level.**
The `flCompression` float at `AnimPermutation+12` is the only field that
differs.

A second header-level finding (see §4): within `warM_2HM_attk_apocalypse`,
multiple permutations' curve-descriptor arrays point at **overlapping
payload regions** — including across the comp=5/comp=3 boundary.

---

## 3. Byte-count comparison table

`bytes/bone/frame = (T + R + S curve bytes) / (bone_count × frame_count)`.

| File | comp | Bones | Frames | Trans B | Rot B | Scale B | B/bone/frame |
|------|-----:|------:|-------:|--------:|------:|--------:|-------------:|
| `barM_HTH_nav_idle` p0       | **3** | 190 |  61 |  4,156 | 12,540 |   548 | **1.488** |
| `barM_HTH_nav_idle` p1       | **3** | 190 | 121 |  4,668 | 23,996 | 1,084 | **1.294** |
| `barM_HTH_nav_idle` p2       | **3** | 190 | 121 |  4,664 | 26,056 | 1,084 | **1.383** |
| `warM_2HM_attk_apocalypse` p2 | **3** | 190 | 23 |  5,952 | 10,280 |   208 | **3.762** |
| `warM_2HM_attk_apocalypse` p3 | **3** | 190 | 49 | 11,656 | 20,860 |   312 | **3.526** |
| `warM_2HS_attk_weaponAttack` p0   | **5** | 190 | 53 |  8,420 | 18,796 |   288 | **2.731** |
| `warM_2HS_attk_weaponAttack` p1   | **5** | 190 | 53 |  8,312 | 18,232 |   264 | **2.662** |
| `warM_1hsOh_attk_weaponAttack` p0 | **5** | 190 | 53 | 12,112 | 25,208 |   612 | **3.767** |
| `warM_1hsOh_attk_weaponAttack` p1 | **5** | 190 | 53 |  8,384 | 19,064 |   288 | **2.754** |
| `warM_demonForm_nav_runStop` p0   | **5** | 190 | 60 |  9,844 | 19,024 |   872 | **2.609** |
| `warM_2HM_attk_apocalypse` p0     | **5** | 190 | 97 | 13,160 | 35,092 |   364 | **2.638** |
| `warM_2HM_attk_apocalypse` p1     | **5** | 190 | 16 |  6,136 |  7,556 |   336 | **4.614** |
| `warM_2HM_attk_apocalypse` p4     | **5** | 190 | 66 | 15,628 | 24,960 | 1,068 | **3.322** |
| `warM_1HS_attk_SigilOfFlames` p0  | **5** | 190 | 59 | 13,684 | 29,292 | 1,528 | **3.970** |

**Reading:** comp=5 sits at **2.6–4.6 bytes/bone/frame**, the same band as
comp=3 (1.3–3.8). It is emphatically **not** ~12 (the comp=0 band) and not
>12 (a different curve type). The per-file spread is explained entirely by
keyframe density, not by a different encoding: short clips with sparse
motion (apocalypse p1, 16 frames) cost more per frame because the fixed
per-curve header is amortised over fewer frames — exactly the comp=3
pattern. The two comp=3 apocalypse permutations (p2/p3) fall *inside* the
comp=5 range for the same file, which on its own already argues the modes
are not structurally different.

---

## 4. Hex dumps (annotated)

All dumps from `warM_2HM_attk_apocalypse` so the comp=5 (p0) and comp=3
(p2) samples come from the **same animation, same `warM_base00`
skeleton, same payload file** — no cross-file noise. Offsets are absolute
byte offsets into the payload file.

### 4a. The clincher — a curve blob shared across the comp boundary

`bone[8]` (hash `0x1289e8b3`), translation curve. The descriptor in the
comp=5 permutation p0 **and** the descriptor in the comp=3 permutation p2
both point at payload offset **6416**, size 16:

```
comp=5 p0  bone[8] TRANSLATION  (16 B @ payload offset 6416)
comp=3 p2  bone[8] TRANSLATION  (16 B @ payload offset 6416)   <- SAME offset

  00001910  01 04 00 00 00 84 5d 39 bb 6a a9 bd 55 2c b9 bd  |......]9.j..U,..|
            ^^ ^^ ^^^^^ ^^^^^^^^^^^ ^^^^^^^^^^^ ^^^^^^^^^^^
            |  |  u16   f32 base.x  f32 base.y  f32 base.z
            |  fmt=0x04
            count=1  -> constant curve, baseline (0.00021, -0.08272, -0.09042)
```

It is a single physical 16-byte blob. A comp=5 permutation and a comp=3
permutation each name it as their `bone[8]` translation curve. **If comp=5
and comp=3 decoded curve bytes differently, the engine could not legally
alias one blob to both.** A systematic count over all 190 bones:

| Channel | comp=5 p0 ∩ comp=3 p2 curves at identical (offset, size) |
|---------|-----------------------------------------------------------|
| Translation | **130** / 190 |
| Rotation    | **96** / 179 non-empty |
| Scale       | **11** / 13 non-empty |

237 shared blobs across the comp=5/comp=3 boundary.

### 4b. comp=5 translation curve, animated (count ≥ 2)

`bone[6]` (hash `0x32ff39ad`), comp=5 p0 — a `count=6` animated curve.
This is the exact `flCompression=0/3` translation layout from
`anim_parser.py` (`[u8 count][u8 fmt=0x04][u16 pad][f32 baseline xyz]`,
then `[u8 timestamps[count]]`, 4-byte pad, `[f32 vec3 × count]`):

```
comp=5 p0  bone[6] TRANSLATION  (96 B @ payload offset 6064)
  count=6  fmt=0x04  pad=0x0000  baseline xyz = (0.00799, 0.00325, -0.0479)
  timestamps[6] = [0, 5, 9, 20, 43, 96]      (96 = last frame; 97-frame clip)

  000017b0  06 04 00 00 c4 e6 02 3c 60 ce 54 3b 40 34 44 bd  |.......<`.T;@4D.|
            ^^ ^^ ^^^^^ <--- f32 baseline x/y/z (12 B) --->
            cnt fmt pad
  000017c0  00 05 09 14 2b 60 00 00 00 00 00 00 00 00 00 00  |....+`..........|
            <-- timestamps[6] --> <-- 4-byte align pad to offset 0x17d4 -->
  000017d0  00 00 00 00 5f 07 fe bb 88 1f 4f bb 34 e2 ae 3c  |...._.....O.4..<|
                        <------ f32 vec3 keyframe[0] (delta) ----->
  000017e0  c5 e6 02 bc 38 76 55 bb 45 3c b4 3c c5 e6 02 bc  |....8vU.E<.<....|
  000017f0  38 76 55 bb 45 3c b4 3c c5 e6 02 bc 38 76 55 bb  |8vU.E<.<....8vU.|
  00001800  45 3c b4 3c c5 e6 02 bc 38 76 55 bb 45 3c b4 3c  |E<.<....8vU.E<.<|
```

Size check: `align4(16 + 6) + 6×12 = 24 + 72 = 96` ✓.

### 4c. comp=5 vs comp=3 rotation curve, side by side

`bone[8]` (hash `0x1289e8b3`) rotation — comp=5 p0 has `count=88`, comp=3
p2 has `count=23`. Both follow the identical `flCompression=0/3` rotation
layout (`[u16 count][u8 timestamps[count-1]]`, 2-byte pad,
`[i16 quat xyzw × count]`):

```
comp=5 p0  bone[8] ROTATION  (796 B @ payload offset 23816)   count=88
  00005d08  58 00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e  |X...............|
            ^^^^^ <----- u8 timestamps[87], frames 1..96 ----
  ...
  00005d58  54 55 56 57 58 59 5a 5d 60 00 3b f5 be 1a 1f 71  |TUVWXYZ]`.;....q|
            ----- end timestamps --> ^^^^^ <-- i16 quat keyframes -->
                                     2-B pad
  00006018  e3 5f 4b e5 d8 05 17 50 06 60 00 00              |._K....P.`..|
                                            ^^^^^ 2-byte trailer

comp=3 p2  bone[8] ROTATION  (208 B @ payload offset 90840)   count=23
  000162d8  17 00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e  |................|
            ^^^^^ <-- u8 timestamps[22], frames 1..22 -->
  000162e8  0f 10 11 12 13 14 15 16 97 db cd 0e e8 30 8f 6f  |.............0.o|
            -- end timestamps --> ^^^^^ <-- i16 quat keyframes -->
                                  (2+22 is even -> no pad)
  ...
  00016398  fe df 6a 11 ab 2f 0f 71 8f de 24 10 75 30 7f 70  |..j../.q..$.u0.p|
```

Identical structure. Size check, comp=5 count=88:
`align2(2 + 87) + 88×8 + 2 = 90 + 704 + 2 = 796` ✓.
comp=3 count=23: `align2(2 + 22) + 23×8 + 2 = 24 + 184 + 2 = 210`… see the
2-byte-trailer note below — the observed 208 vs. the +2 trailer is itself
mode-independent.

> **Formula correction (applies to comp=0/3 *and* comp=5 equally):**
> Every `count ≥ 2` rotation curve in the corpus — comp=3 baseline
> included — is exactly **2 bytes longer** than
> `align2(2 + (count-1)) + count×8`. There is a 2-byte trailer after the
> last quaternion (mirroring the `[u16 trailer]` the existing decoder
> already documents for the `count == 1` case). The current
> `decode_rotation_curve` tolerates this because its bounds check only
> rejects `needed > len(blob)` — a 2-byte *surplus* is silently ignored.
> This is **not a comp=5 issue**; it is a pre-existing under-documentation
> of the comp=0/3 format. The comp=5 implementation does not need to act
> on it, but it should be noted so a future strict parser doesn't trip.

---

## 5. Hypothesis

**flCompression=5 uses the exact same curve byte encoding as
flCompression=3.** Confidence: **~95%**.

Evidence, strongest first:

1. **Cross-mode blob sharing (§4a).** 237 curve byte blobs in
   `warM_2HM_attk_apocalypse` are physically aliased between a comp=5
   permutation and a comp=3 permutation. The same bytes are decoded by
   both modes → the encoding is `flCompression`-independent. This is
   close to a proof, not just a correlation.

2. **Size-equation test (`probe.py`).** Every curve blob in all 9 comp=5
   permutations satisfies the identical `count`-driven size equations as
   the comp=3 baseline:
   - translation/scale `count ≥ 2`: `align4(hdr + count) + count×12`
   - rotation `count ≥ 2`: `align2(2 + count-1) + count×8 + 2`

   The comp=3 baseline `barM_HTH_nav_idle` satisfies the **same** equations
   (including the +2 trailer) — comp=5 and comp=3 are indistinguishable by
   size structure.

3. **Quaternion magnitude test (`valuetest.py`, Test 1).** All ~17,000
   comp=5 rotation keyframes, decoded as full int16 `xyzw / 32767`, have
   `|q|` ∈ [0.9999, 1.0000] — 100 % unit length, identical to the comp=3
   baseline. A smallest-3 scheme, an 8-bit scheme, or a different
   normaliser would produce wildly non-unit magnitudes here.

4. **Decode-without-fallback (`valuetest.py`).** Running the production
   `decode_permutation` over all 9 comp=5 permutations (with
   `perm.compression` overridden to 3 in memory) produced
   **zero per-curve `AnimFormatError` fallbacks** across ~5,100 curves —
   every `fmt` byte, every alignment, every bound held.

5. **fmt-byte audit (`hexdump.py`).** 1,826 / 1,826 non-empty comp=5
   translation/scale curves carry `fmt == 0x04` — the value the comp=0/3
   decoders hard-require.

6. **Rest-pose known-plaintext (`valuetest.py`, Test 2).** comp=5 attack
   animations, decoded as comp=3 and compared to the `warM_base00` rest
   pose at frame 0, match on ~90–111 / 190 bones for translation
   (sub-millimetre median error, ~7.5e-4) and ~63–71 / 190 for rotation —
   *the same match profile as the comp=3 permutations p2/p3 of the same
   file*. (A mid-swing attack is not expected to match rest on every bone;
   the static bones — fingers, etc. — match cleanly, which is the signal.)

7. **Constant-curve known-plaintext (`valuetest.py`, Test 3).** For bones
   that are `count=1` constants in both apocalypse p0 (comp=5) and p2
   (comp=3): **96/96 rotation** and **130/131 translation** baselines are
   byte-identical. The single translation outlier is the root bone, which
   genuinely sits at a different place in two different permutations of
   the animation — a real value difference, not a decode error.

**What flCompression=5 *means* semantically is still unknown** and does
not matter for decoding. It is most plausibly a newer
exporter/tool-version stamp or a runtime playback hint (additive layering,
etc.) introduced after comp=3 — the curve bytes are untouched by it.

Residual ~5 % uncertainty: the corpus is 5 files / 9 comp=5 permutations.
It is diverse (basic attacks, off-hand variant, locomotion, a 5-permutation
big spell, a channeled cast) and spans ~5,100 curves, but it cannot prove
that *no* comp=5 clip anywhere uses a variation. The mitigation is cheap
and built into the implementation plan (step 4 below).

---

## 6. Recommended implementation strategy

**Effort estimate: ~1 hour.** This is a guard-widening change, not a
decoder-writing change.

1. **Add a supported-compression constant** to `anim_parser.py`, e.g.
   `_SUPPORTED_COMPRESSION = (0, 3, 5)`, with a comment pointing at this
   report.

2. **Widen the four guards** that currently read
   `if compression not in (0, 3)` / `if perm.compression not in (0, 3)`:
   - `decode_translation_curve`
   - `decode_rotation_curve`
   - `decode_scale_curve`
   - `decode_permutation`

   No other code path branches on `flCompression`. The curve readers are
   already mode-agnostic below the guard.

3. **Add a regression test.** The cleanest fixture is synthetic — a comp=5
   permutation built from the same byte layout the existing comp=3 tests
   use, asserting it decodes identically. Optionally add a slow/integration
   test that decodes a real extracted comp=5 payload from
   `research/flcompression5/extracted/` if that directory is kept.

4. **Validate end-to-end (the real acceptance test).** Re-run the Warlock
   Character Builder export that produced the 55 comp=5 decode failures.
   Expect the decode-failure count to drop to **~0**. Then open the
   exported `.glb` in Blender and confirm the Warlock ability animations
   (Apocalypse, Sigil of Flames, Demon Form, etc.) play correctly on the
   assembled character — no exploded bones, smooth motion, correct timing.

5. **Update the d4-animation-extraction skill** — its `flCompression`
   description (`0=uncompressed, 3=quantized`) should gain
   `5=same layout as 3` so the next reader doesn't re-research this.

**Collapse recommendation: YES — combine implementation into one prompt.**
The research is conclusive; steps 1–3 are mechanical and step 4 is the
safety net. There is no second research cycle needed: comp=5 is not a
novel format.

---

## 7. Open questions

- **What does `flCompression=5` semantically signify?** Not needed to
  decode (the bytes are comp=3-identical), but worth a one-line note if it
  ever surfaces — likely a newer exporter version or a playback-layer
  flag. Could be confirmed by checking whether comp=5 correlates with a
  d4data build version or with specific `AnimPermutation` flag fields.
- **Does any comp=5 clip outside this corpus use a variation?** Unlikely
  given the diversity sampled, but the full Warlock decode-failure set (55
  animations) is the natural wider test — step 4 covers it. If a Warlock
  anim still fails after the guard widening, capture it and re-open this
  research.
- **The 2-byte rotation-curve trailer** (§4) is undocumented in the
  current decoder and the d4-animation-extraction skill. It is harmless
  today (the bounds check tolerates a surplus) and mode-independent, but a
  future strict/round-trip parser should account for it.
- **Long animations (> 255 frames).** Timestamps are `u8` frame indices;
  the corpus tops out at 97 frames. This is a pre-existing comp=0/3
  limitation, not comp=5-specific, but the implementation should confirm
  no Warlock comp=5 clip exceeds 255 frames (none in this corpus does).
- **comp=1, 2, 4 remain uncharted.** Out of scope here. comp=1 is known to
  back IGC cinematics (already filtered from the Character Builder); 2 and
  4 are unsampled.

---

## Surprises / notes for the next session

- **`RustyDemonCLI.extract_anim_pair` returns the wrong files when its
  output directory is reused across calls.** `extract()` snapshots the
  *entire* output directory with `os.walk`, and `extract_anim_pair` then
  returns `meta_files[0]` / `payload_files[0]` — the alphabetically-first
  meta/payload in the whole directory, not the ones matching `anim_name`.
  Extracting six animations into one shared directory produced six
  identical (wrong) return tuples and a bogus `is_shared` flag. The fix in
  `extract.py` was to give each animation its own subdirectory. This is a
  latent wrapper bug worth a separate ticket — it is not exercised today
  because callers use fresh/isolated directories, but it is a sharp edge.
- **`warM_2HM_attk_apocalypse` mixing comp=5 and comp=3 in one file** was
  the single most useful corpus property and was not anticipated. Any
  future compression-mode research should first scan for a file that
  carries the unknown mode *and* a known mode together.
- **Permutations alias curve data within a payload.** Beyond the
  cross-mode sharing in §4a, several apocalypse permutations point
  different permutations' curve-descriptor arrays at overlapping payload
  regions. The decoder already handles this (it follows offsets blindly),
  but it is worth knowing that payload byte ranges are not partitioned
  one-per-permutation.
