# Diagnostic — Spiritborn M & Lilith import at an offset from world origin

**Date:** 2026-05-17
**Status:** Conclusive. Root cause identified with direct evidence.
**Scope:** Diagnosis only — no production code was modified.

---

## TL;DR

The offset is **not** a rig / inverse-bind-matrix / scene-graph bug. All
three skeletons are structurally identical and correct: root bone at the
world origin, inverse bind matrices rest-consistent, every mesh node and
the bone-hierarchy root at identity.

The offset is the **"wrong default action"** bug — the same class as the
already-documented `npc_crow` issue (`project_d4extract_gltf_anim_verified.md`:
*"npc_crow 'mirrored' was wrong-default-action in Blender (cutscene at
index 0)"*).

On glTF import, Blender applies the animation at **index 0** as the
armature's active Action. Both broken exports have a **cutscene/cinematic
clip at index 0** whose **root-bone translation channel holds a constant,
non-zero displacement** (the root motion that places the actor on its
mark in the cinematic). Blender applies it → the whole character is
displaced. The working export simply has **no cinematic clips at all**,
so its index-0 clip leaves the root at the origin.

| Export | Path | anim[0] | anim[0] root translation | Result |
|---|---|---|---|---|
| Spiritborn M | Character Builder (per-piece) | `Conv_Hell_CBE_spim` | **(−0.2326, 0, −1.5699)** const | **offset** |
| Lilith | Model Browser (single-mesh) | `IGC_T3_hds_lilith_scaled_1050` | **(−1.2315, +0.3191, −1.9593)** const | **offset** |
| Warlock F | Character Builder (per-piece) | `warF_STF_attk_basiccast_temp` | *no root channel* → (0,0,0) | correct |

---

## 1. Per-piece export landed status

**Yes — the per-piece Character Builder export landed.**
`CharacterBuilderPage.get_export_pieces()` exists in `builder_page.py`,
and `main_window._export_builder()` routes to `_export_builder_pieces()`
which calls it. Confirmed in the exported files:

- `spiritbornm.glb` / `warlockf.glb`: 10 meshes, 11 scene-root nodes
  (8 skinned piece nodes + 1 bone-hierarchy root + 2 weapon nodes) →
  the per-piece topology.
- `lilith_scaled.glb`: 1 mesh, 2 scene-root nodes → the legacy
  single-mesh Model Browser path.

**Important correction to the task premise:** there is **no
`armature_root` node** in either export path (`grep armature
gltf_export.py` → no matches). The bone-hierarchy root (`bone_bf59f7af`)
is itself a scene-root node. Steps 4 and 6 of the brief — "read the
`armature_root` emission" and "force `armature_root` to identity" —
therefore have no target; that hypothesis is structurally inapplicable.
The substitute hypothesis test (animation reorder) was run instead — see
§8.

**The per-piece export is exonerated.** Warlock F works *on the
per-piece path*; Lilith is broken *on the untouched single-mesh path*.
The bug is path-independent and lives in shared animation handling that
pre-dates the per-piece work.

---

## 2. Test corpus

All three are real GUI exports already on disk (`pythongui_exports/`,
timestamped 2026-05-17 21:1x — current builds). Analyzed directly; no
CASC re-extraction needed because the glb carries the full skeleton
(bone node TRS) and inverse bind matrices.

| File | Size | Path | nodes | meshes | skin joints | animations |
|---|---|---|---|---|---|---|
| `pythongui_exports/spiritbornm.glb` | 116 MB | Character Builder per-piece | 335 | 10 | 325 | 184 |
| `pythongui_exports/warlockf.glb` | 201 MB | Character Builder per-piece | 365 | 10 | 355 | 427 |
| `pythongui_exports/lilith_scaled.glb` | 47 MB | Model Browser single-mesh | 315 | 1 | 314 | 40 |

Base body pieces: `spiM_P00`, `warF_P01`, `lilith_scaled`. All three
skeletons share the same root bone hash **`bone_bf59f7af`**.

---

## 3. Root + first bones TRS (glTF space, post `z_up_to_y_up`)

Bone 0 = `bone_bf59f7af` (root). Bone 1 = `bone_98c6875e` (hips/pelvis).

| Skeleton | bone 0 local T | bone 1 (hips) local T | bone 1 world T |
|---|---|---|---|
| Spiritborn M | (0, 0, 0) | (+0.0167, +1.2014, 0) | (+0.0167, +1.2014, 0) |
| Warlock F | (0, 0, 0) | (−0.0882, +1.0549, 0) | (−0.0882, +1.0549, 0) |
| Lilith | (0, 0, 0) | (−0.0145, +1.4525, 0) | (−0.0145, +1.4525, 0) |

The root bone is at the **origin** in every skeleton. The hips sit
~1.0–1.45 up the Y (Y-up) axis and within ±0.09 of origin in X/Z — i.e.
a normal pelvis height. **No skeleton has an offset root or offset
hips.**

---

## 4. World-space rest pose of the hips/pelvis

Computed by walking the parent chain (root → … → hips) and accumulating
rest TRS. Result is identical to §3's "bone 1 world T":

- Spiritborn M hips world: **(+0.017, +1.201, 0)**
- Warlock F hips world: **(−0.088, +1.055, 0)**
- Lilith hips world: **(−0.015, +1.453, 0)**

**Step 2 hypothesis (hips world-pos offset distinguishes broken vs
working) → REJECTED.** All three hips are within 0.09 of the X/Z origin.
The ~0.4-unit Y spread is just height variance (Lilith is a taller
model) and is a Y-up axis offset, not a ground-plane displacement.

---

## 5. Inverse bind matrix translations

`world(joint) @ IBM` was computed for the first 6 joints of each skin —
at a self-consistent rest pose this product must be identity.

| Skeleton | root IBM T | hips IBM T | `world@IBM` translation (all probed joints) |
|---|---|---|---|
| Spiritborn M | (0, 0, 0) | (−0.0167, −1.2014, 0) | (0, 0, 0) ✓ |
| Warlock F | (0, 0, 0) | (+0.0882, −1.0549, 0) | (0, 0, 0) ✓ |
| Lilith | (0, 0, 0) | (+0.0145, −1.4525, 0) | (0, 0, 0) ✓ |

Each hips IBM translation is the exact negation of the hips world
translation, and `world(joint) @ IBM` is the identity (translation 0)
for every probed joint in all three files.

**Step 3 hypothesis (IBM / bone double-counting) → REJECTED.** The
inverse bind matrices are rest-consistent. At rest, skinning reproduces
the authored vertex positions exactly — no IBM-induced offset.

---

## 6. `armature_root` / scene-graph TRS in the emitted glTF

There is no `armature_root` node. The relevant nodes:

| Node | Spiritborn M | Warlock F | Lilith |
|---|---|---|---|
| skinned mesh node(s) | scene-root, TRS = identity (unset) | same | same |
| bone-hierarchy root `bone_bf59f7af` | scene-root, T=[0,0,−0] R=[0,0,−0,1] S=[1,1,1] | same | same |
| `skin.skeleton` field | node 8 (= bone root) | node 8 | node 1 |

Every mesh node and the bone root carry **identity** transforms in all
three exports. There is nothing non-identity anywhere in the scene
graph that could displace geometry.

---

## 7. Scene graph diff — broken vs working

The Spiritborn M (broken) and Warlock F (working) scene graphs are
**structurally identical**: same per-piece layout (8 skinned mesh nodes
+ bone root + 2 weapon nodes as scene roots), same identity TRS
everywhere, same skin wiring (`skin.skeleton` → bone root, joints[0] =
root bone). Lilith differs only in being single-mesh (Model Browser
path), but its skin/bone-root wiring is the same shape.

**There is no scene-graph difference that explains the offset.** The
only differentiating factor between broken and working is **animation
content at index 0** (§9).

Geometry note (secondary): the POSITION bounding boxes of the Spiritborn
M body pieces are origin-centred and ground-anchored, the same as
Warlock F's (feet at Y≈0, body X∈[−0.22,+0.19]). Lilith's single mesh,
however, has a POSITION bounding box centred at **X≈−0.95** (X∈[−2.09,
+0.20]). This is a *bind-pose geometric* offset, separate from the
animation offset; its origin cannot be determined from the glb alone
(it may be Lilith's authored asymmetric boss silhouette, or an asset
quirk). See "Open items".

---

## 8. Hypothesis test (Step 6 substitute)

Step 6 as written (force `armature_root` to identity) is inapplicable —
no such node, and all roots are already identity. The hypothesis
actually supported by the data is *"the animation at index 0 carries
root motion that displaces the character."* Test artifact produced:

**`research/origin-offset/spiritbornm_PATCHED_restpose_first.glb`** —
the `animations` array of `spiritbornm.glb` reordered so the synthetic
in-place `rest_pose` Action (root translation = (0,0,0)) is index 0 and
`Conv_Hell_CBE_spim` moves to index 1. Verified: structurally identical
to the original (same node/mesh/skin/accessor counts, same binary blob;
each Animation is self-contained so reordering the list is index-safe).

**Predicted result:** importing the patched glb shows Spiritborn M at
the world origin (Blender applies index-0 `rest_pose` → root at (0,0,0)).
**Final confirmation requires the user to import it in Blender** — this
session cannot run Blender. The prediction rests on the documented
`npc_crow` precedent (this project already established that Blender
applies index 0 and that a cutscene there produces the wrong default
pose).

---

## 9. Final diagnosis

**Root cause:** Both broken exports have a **cutscene / cinematic
animation at index 0** whose **root-bone (`bone_bf59f7af`) translation
channel holds a constant, non-zero value** — the baked-in root motion
that positions the actor in the cinematic scene:

- Spiritborn M — `animation[0] = Conv_Hell_CBE_spim` (a "Conversation,
  Hell" cutscene): root translation **(−0.2326, 0, −1.5699)**, constant
  across all 231 frames.
- Lilith — `animation[0] = IGC_T3_hds_lilith_scaled_1050` (an In-Game
  Cinematic): root translation **(−1.2315, +0.3191, −1.9593)**, constant
  across all 182 frames.

Blender applies the index-0 Action on import, so the entire character —
every bone, and the skinned mesh that follows it — is displaced by that
root translation.

Warlock F is correct only by luck of its content: its appearance has
**0 cinematic clips** among 427 animations. Its `animation[0]
= warF_STF_attk_basiccast_temp` is a gameplay attack with **no root
translation channel at all**, so the root stays at the glTF rest pose
(0,0,0). The synthetic `rest_pose` Action (root = (0,0,0)) is correct in
all three files but is emitted **last**, not first.

**The exporter's per-channel logic is correct.** The root translation
channel is emitted because `_channel_differs_from_rest`
(`gltf_export.py:1636`, used at `gltf_export.py:1435`) correctly detects
a constant baseline that overrides the rest pose — the deliberate
"count=1 override" behaviour. Stripping that channel would be wrong for
genuine cutscene playback. The bug is purely **ordering**: a root-motion
cutscene must not be the default (index-0) Action.

**Animation order = discovery order.** `export_worker.py:_decode_anim_infos`
(line 491) appends decoded clips in `self._anim_infos` order
(line 677); `_append_rest_pose_animation` (line 726) appends the
synthetic `rest_pose` **last** (line 748). `_anim_infos` order comes
from `builder_page.get_export_anim_infos()` (line 1616) →
`_anim_discovery_cache` order. Whatever clip discovery happens to list
first becomes `animation[0]`; for `spiM` and `lilith` that is a
cinematic with root motion.

This is the same defect class as the documented `npc_crow` "wrong
default action" issue — now manifesting as a *positional* offset
(root translation) rather than just a wrong pose.

**Counts (confirming the pattern):**

| Export | total anims | cinematic-named anims | at index 0? |
|---|---|---|---|
| Spiritborn M | 184 | 2 (`Conv_*`, `_CBE_`) | **yes** |
| Lilith | 40 | 1 (`IGC_*`) | **yes** |
| Warlock F | 427 | 0 | n/a |

---

## 10. Recommended fix scope

**Small — single fix point, shared by both export paths.** The fix
belongs in `export_worker.py`, which both the Character Builder and the
Model Browser route through.

**Recommended (option 1): emit `rest_pose` as `animation[0]`.**
In `_append_rest_pose_animation` (`export_worker.py:748`), change
`self._animations.append(rest_anim)` to `self._animations.insert(0,
rest_anim)`. The synthetic `rest_pose` has `force_static_channels=True`,
so it emits a root translation channel pinned to (0,0,0) — making it
*actively* anchor the character at the origin as the default Blender
import pose. One-line change, lowest risk, fixes both broken cases at
once. (Guard already ensures `rest_pose` is only added when real
animations exist and the model is skinned.)

Trade-off: the user-facing animation list then shows `rest_pose` first.
If that is undesirable, a slightly smarter variant is to pick a known
in-place idle (`*paperdoll_idle`, `*nav_idle`) for index 0 — but that is
name-heuristic and more fragile; `rest_pose`-first is robust.

Options **not** recommended: stripping/zeroing root translation channels
globally (breaks genuine cutscene playback); name-sorting cinematics
last (fragile string matching).

**This diagnosis is conclusive enough that the fix is obvious.** The
next prompt can be a single small implementation handoff: "move the
synthetic `rest_pose` animation to index 0 in `ExportWorker`, add a
regression test asserting `animations[0].name == 'rest_pose'` when real
animations are present."

---

## Open items (not blocking the fix)

1. **Lilith bind-pose X offset (~−0.95).** Lilith's POSITION bounding
   box is centred at X≈−0.95 independent of any animation (§7). After
   the index-0 fix, Lilith will import at her bind pose, which is still
   ~0.95 off-origin in X. Whether that is correct (authored asymmetric
   boss silhouette) or a separate asset/parse artifact cannot be
   resolved from the glb — it needs the raw `lilith_scaled.app`
   geometry compared against the skeleton. Recommend a follow-up check
   *only if* the residual offset matters after the animation fix. It is
   unrelated to the Spiritborn M offset (whose body geometry is
   origin-centred).

2. **Blender default-action confirmation.** The diagnosis rests on the
   documented `npc_crow` precedent that Blender applies `animation[0]`.
   The patched glb (§8) lets the user confirm directly. If the patched
   Spiritborn M still imports offset, Blender's default-action selection
   differs from the assumption — but the root-translation data in §9 is
   itself direct, Blender-independent evidence of where the displacement
   comes from.

---

## Reproduction

Scripts in `research/origin-offset/` (read-only analysis):

- `inspect_glb.py <glb>` — scene graph, skin wiring, bone TRS, IBM
  consistency.
- `mesh_bounds.py <glb>` — per-mesh POSITION bounding boxes.
- `inspect_anims.py <glb>` — animation list + root/hips translation
  channels per animation.
- `patch_anim_order.py <in.glb> <out.glb>` — hypothesis-test patch
  (reorders `rest_pose` to index 0). Output:
  `spiritbornm_PATCHED_restpose_first.glb`.
