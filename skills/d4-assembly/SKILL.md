---
name: d4-assembly
description: "Diablo IV multi-piece character assembly system for combining equipment pieces under a shared skeleton. Covers the assembly concept (why it works), skeleton template ID matching, the merge algorithm for combining multiple MeshData objects, geometry concatenation with index rebasing, material/texture deduplication, cloth bone handling during merge, the GUI character builder workflow (class selector → slot panel → piece browser → viewport assembly → export), known template IDs per class, slot-to-SNO-path heuristics for categorizing pieces, and assembled glTF export. Use this skill whenever implementing the character builder, merging multiple models under one armature, filtering pieces by skeleton compatibility, building the assembly viewport, exporting assembled characters, or debugging assembly issues like mismatched skeletons or broken bone indices after merge."
---

# D4 Multi-Piece Character Assembly

This skill covers the assembly system that lets users combine multiple Diablo IV equipment pieces (helmet, chest, gloves, pants, boots) into a single character under one skeleton. It documents the Phase 10D content that was truncated from the d4-skeleton-animation-assembly skill.

## Why assembly works

D4 characters are composed of multiple equipment pieces that share a skeleton template. Every `.app` file for the same character class embeds the **full skeleton** — a helmet with only 1 weighted bone still carries the complete 190-298 bone hierarchy. This is intentional: `BoneData.unk_a3acec8` (the skeleton template ID) is identical across all equipment for the same class, so bone indices are globally consistent. A vertex weighted to bone 47 in the chest piece refers to the same physical bone as bone 47 in the boots piece.

This means assembly is conceptually simple: load N pieces, verify they share a template ID, concatenate their geometry, keep one copy of the skeleton, export as a single glTF with all submeshes under one `Skin` node.

## Known skeleton template IDs

From `docs/d4extract_roadmap.md` and observed game data:

| Template ID (dec) | Template ID (hex) | Class | Base bones | Cloth bones | Total |
|---|---|---|---|---|---|
| 188232014 | 0x0B38B14E | Barbarian | 190 | 0 | 190 |
| 3340713596 | 0xC71F2E7C | Necromancer | 192 | 106 | 298 |

The remaining classes (Druid, Rogue, Sorcerer, Spiritborn) have template IDs that haven't been documented yet. They can be discovered by loading any skinned player piece for that class and reading `mesh_data.skeleton.template_id`.

### Runtime template discovery

The GUI character builder doesn't hardcode template IDs. Instead, it discovers them at runtime:

1. When the catalog loads, the builder can scan a few known player pieces per class (e.g. `player_barb_*.app`, `player_sorc_*.app`)
2. Parse the skeleton from each piece and read `skeleton.template_id`
3. Cache the `{class_name: template_id}` mapping for the session
4. The piece browser then filters the catalog to only show models whose template ID matches the selected class

Alternatively, a simpler initial approach: filter purely by SNO path prefix (`player_barb_`, `player_sorc_`, etc.) and validate template compatibility when the user equips a piece. This avoids a batch scan at startup.

## SNO path conventions for player equipment

Player equipment follows naming patterns in the CASC catalog:

```
base/meta/Appearance/player_barb_<piece_descriptor>.app
base/meta/Appearance/player_sorc_<piece_descriptor>.app
base/meta/Appearance/player_druid_<piece_descriptor>.app
base/meta/Appearance/player_rogue_<piece_descriptor>.app
base/meta/Appearance/player_necro_<piece_descriptor>.app
base/meta/Appearance/player_spirit_<piece_descriptor>.app   (Spiritborn — expansion)
```

The class prefix maps to the category pill filter already in the GUI (`PLR` pill matches `player_`).

### Slot detection from submesh data

There is no guaranteed slot field in the SNO path name. Slot assignment comes from the submesh-level `slot_hash` field (a DJB2 hash resolved via `data/hash_names.json`):

| Hash (dec) | Slot abbreviation | Display name |
|---|---|---|
| 110143 | bdy | Chest / Body |
| 110665 | bts | Boots |
| 115849 | glv | Gloves |
| 116929 | hlm | Helm |
| 121048 | leg | Legs / Pants |
| 130201 | trs | Torso |

A single `.app` file's submeshes may carry different slot hashes (e.g. a chest piece with attached shoulder geometry might have both `bdy` and `trs` slots). For the character builder's piece browser, the **dominant slot** is the most common `slot_hash` across the piece's submeshes, or the first non-zero slot hash encountered.

Some pieces have a slot hash of 0 (unknown/unresolved). These are typically environment models, monsters, or base body meshes — the piece browser should filter them out or show them in an "Other" category.

## The merge algorithm

### Input

A list of `MeshData` objects, all with the same `skeleton.template_id`.

### Steps

1. **Validate template compatibility**: Assert all pieces share the same `skeleton.template_id`. Abort with a clear error if any piece has a different template or is unskinned (static mesh).

2. **Pick the canonical skeleton**: Use the skeleton from the first piece (or the piece with the most bones — they should all be identical, but cloth bone counts can vary). The canonical skeleton becomes the single `Skin` node in the output glTF.

3. **Concatenate geometry with index rebasing**: For each piece, the vertex data (positions, normals, tangents, UVs, joints, weights, colors) is appended to a running buffer. Index values are offset by the cumulative vertex count of all previous pieces:

   ```python
   vertex_offset = 0
   index_offset = 0
   material_offset = 0
   merged_positions = []
   merged_indices = []
   merged_submeshes = []

   for piece in pieces:
       merged_positions.extend(piece.positions)
       # Rebase indices
       merged_indices.extend(i + vertex_offset for i in piece.indices)
       # Rebase submesh offsets
       for sm in piece.submeshes:
           merged_submeshes.append(Submesh(
               vertex_offset=sm.vertex_offset + vertex_offset,
               vertex_count=sm.vertex_count,
               index_offset=sm.index_offset + index_offset,
               index_count=sm.index_count,
               material_index=sm.material_index + material_offset,
               bone_palette=sm.bone_palette,
               sub_object_hash=sm.sub_object_hash,
               slot_hash=sm.slot_hash,
               name=sm.name,
           ))
       vertex_offset += len(piece.positions)
       index_offset += len(piece.indices)
       material_offset += len(piece.materials or [])
   ```

4. **JOINTS_0 values pass through unchanged**: Because all pieces share the same skeleton template, the pBoneIDs-remapped global bone indices are already consistent. A `JOINTS_0` value of 47 in any piece refers to the same bone. No re-remapping is needed during merge.

5. **Material roster concatenation**: Each piece's materials are appended to a combined list. Submesh `material_index` values are offset by the cumulative material count from previous pieces. This preserves per-piece material assignments even when pieces use different textures.

6. **Optional material deduplication**: If two pieces reference the same `MaterialDefinition` SNO ID, they can share a single glTF material. This reduces file size but requires tracking which materials are identical by SNO ID rather than by list index.

7. **Cloth bone handling**: Cloth bones (indices >= `base_bone_count`) are physics-driven at runtime and have identity transforms at rest. During assembly:
   - Include cloth bones from all pieces in the skeleton (they're already part of the template)
   - Cloth bone submeshes from each piece retain their original joint indices
   - The `base_bone_count` / `cloth_bone_count` split stays the same since it's a property of the template, not of individual pieces

8. **Build the merged MeshData**:

   ```python
   assembled = MeshData(
       name=f"assembled_{class_name}",
       positions=merged_positions,
       normals=merged_normals,
       tangents=merged_tangents,
       uvs=merged_uvs,
       joints=merged_joints,
       weights=merged_weights,
       colors=merged_colors,
       indices=merged_indices,
       submeshes=merged_submeshes,
       skeleton=canonical_skeleton,
       materials=merged_materials,
       vertex_count=total_verts,
       index_count=total_indices,
       submesh_count=len(merged_submeshes),
   )
   ```

### Output

A single `MeshData` that can be passed directly to `GltfExporter.export()`. The exporter handles coordinate conversion, buffer layout, and skin node construction identically to a single-piece export — no special assembly mode needed in the exporter.

## GUI character builder workflow

### Panels

The character builder tab has three panels in a horizontal splitter:

| Panel | Position | Widget | Purpose |
|---|---|---|---|
| Slot panel | Left (30%) | `SlotPanel` | Class selector pills + equipment slot grid |
| Viewport | Center (60%) | `ViewportWidget` (dedicated instance) | 3D preview of assembled character |
| Piece browser | Right (10%) | `PieceBrowser` | Filtered model list for the selected slot |

### User flow

1. **Select a class** via the pill buttons (Barbarian, Druid, etc.). This clears all equipped slots, filters the piece browser to show only models matching that class's template ID (or SNO path prefix as a fallback), and clears the viewport.

2. **Click an equipment slot** (Helm, Chest, Gloves, Pants, Boots). This highlights the selected slot in the slot panel and filters the piece browser further — ideally by slot type, but initially can show all pieces for the class and let the user pick.

3. **Select a piece** from the piece browser. This loads the piece from CASC (via `LoadModelWorker`), equips it in the selected slot (updates `SlotState.equipped_sno`), and triggers a viewport refresh.

4. **Viewport updates incrementally**: Each time a slot changes, the viewport runs the merge algorithm on all currently-equipped pieces and re-renders. For responsiveness, cache each piece's parsed `MeshData` (reuse `_mesh_cache`) and only re-parse when a new piece is equipped. The merge itself is fast (just list concatenation).

5. **Export the assembly**: The export button produces a single `.glb` containing all equipped pieces merged under one skeleton. Uses the same `ExportWorker` and `GltfExporter` as the model browser, just with the merged `MeshData` as input.

### Signal flow

```
SlotPanel.class_changed(str)
  -> PieceBrowser.set_class_filter(class_name)
  -> BuilderPage.clear_viewport()

SlotPanel.slot_selected(str)
  -> PieceBrowser.set_slot_filter(slot_key)

PieceBrowser.piece_selected(str)  # SNO path
  -> BuilderPage.equip_piece(slot_key, sno_path)
  -> LoadModelWorker -> MeshData cached
  -> merge all equipped pieces
  -> ViewportWidget.load_mesh(assembled_mesh)
```

### Viewport rendering for assemblies

The builder's `ViewportWidget` is a separate instance from the browser's. It displays the assembled character:

- Call `load_mesh(assembled_mesh_data)` with the merged MeshData
- Per-submesh coloring distinguishes pieces visually (different hue per source slot)
- Textures are loaded per-piece via `TextureWorker` and applied to the corresponding submesh range
- When a single slot changes, for the initial implementation clearing and re-rendering the full assembly is acceptable; incremental updates can come later

## Assembled glTF export

The merged `MeshData` passes through `GltfExporter` unchanged. The exporter:

1. Writes one `buffer` with all vertex + index data
2. Creates one `accessor` per attribute per submesh (via the existing per-submesh primitive construction)
3. Creates one `Skin` node with the canonical skeleton's full joint list and `inverseBindMatrices`
4. Creates one `material` per submesh (or per deduplicated material)
5. Creates one `mesh` with N primitives (one per submesh across all pieces)

The assembled .glb imports into Blender as a single object with one armature and multiple material slots — ready for posing, rendering, or further editing. The Blender addon (d4-blender-addon skill) can post-process the import with material fixes and bone name resolution.

### Export options specific to assembly

| Option | Default | Notes |
|---|---|---|
| Coordinate transform | z_up_to_y_up | Same as single-piece export |
| Full skeleton | always | Pruning is disabled for assemblies — indices must stay global |
| Embed textures | user choice | Per-piece textures embedded independently |
| Materials sidecar | user choice | Combined material list from all pieces |

Bone pruning (`--prune-bones`) must be **disabled** for assembled exports. Pruning remaps bone indices to a compact subset, which would break the global index consistency that makes assembly possible. The GUI should gray out or hide the prune option when exporting from the character builder.

## Edge cases and error handling

### Template mismatch
If a user somehow equips a piece with a different template ID (e.g. due to a catalog filtering bug), the merge step should detect the mismatch and show a clear error: "This piece uses a different skeleton template and cannot be combined with the current build."

### Missing skeleton
Static (unskinned) models have `skeleton = None`. These cannot participate in assembly. The piece browser should filter them out, but the merge algorithm should also guard against them.

### Overlapping geometry
Equipment pieces may have overlapping geometry at slot boundaries (e.g. chest armor shoulder pads overlapping with glove arm pieces). This is normal — D4 handles it at runtime with render order and stencil masking. In the exported assembly, both sets of triangles exist and may z-fight slightly. This is cosmetic and expected.

### Cloth bone count variation
Some pieces may include cloth bones that others don't weight to. The canonical skeleton includes all bones from the template, so cloth bones are always present even if unused. No special handling needed — zero-weighted cloth bone entries are valid in glTF.

### Empty slots
An assembly with only some slots filled (e.g. just chest + boots) is perfectly valid. The merge operates on whatever pieces are currently equipped. An assembly with zero pieces is a no-op — the viewport stays empty.

## Future enhancements

- **Weapon slots**: Weapons attach to hardpoint bones rather than being skinned to the skeleton. Supporting weapon assembly requires identifying hardpoint bone names and computing attachment transforms. Deferred until bone name resolution (Phase 9A/9B) is complete.
- **Save/load builds**: Persist the equipped slot configuration (`{slot_key: sno_path}`) to a JSON file or `QSettings` so the user can reload a character build across sessions.
- **Thumbnails**: Pre-render small viewport captures of each piece for the piece browser, making visual browsing faster than reading SNO path names.
- **Animation preview**: Once animation extraction (Phase 8/9C) is implemented, play idle or walk animations on the assembled character in the viewport.
