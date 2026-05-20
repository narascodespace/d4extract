# Research: Mapping Equipment Mesh Assets to In-Game Display Names

**Date:** 2026-05-18
**Status:** Research complete — implementation not started.
**Goal:** Resolve a Character Builder equipment piece's `.app` filename
(e.g. `barM_crft25_HLM.app`) to its in-game display name
(e.g. *Wanderer's Helm*) so slot dropdowns and the piece browser show
readable, searchable names instead of raw SNO stems.

**One-line conclusion:** In-game names live in
`enUS_Text/meta/StringList/Item_<itemStem>.stl.json` → `arStrings[]`
entry with `szLabel == "Name"` → `szText`. Getting from a `.app` to that
item stem takes a **2-hop name-convention join** (appearance → Actor →
Item) — not a single field, but a deterministic algorithm with a
**~95 % hit rate**. Recommend a **single implementation prompt**.

Research scripts and a sampled dump live in
`d4extract/research/equipment-names/` (`trace_helm.py`, `resolve.py`,
`sample_resolved.json` — scratch code, run with system Python, not
production).

---

## 1. d4data directory catalog

The d4data checkout roots SNO metadata at
`d4data/json/base/meta/<SnoType>/`. Localized text is **separate**, under
`d4data/json/<locale>_Text/meta/StringList/`. Candidate subdirectories
for item display data:

| Path | Count | Relevance |
|---|---|---|
| `base/meta/Item/*.itm.json` | 11,428 | **ItemDefinition** — one per item. References an Actor + ItemType. **Carries no name field.** |
| `base/meta/ItemType/*.itt.json` | 151 | Item *category* (Helm, Axe, Boots…). Used to know an item's slot. |
| `base/meta/Actor/*.acr.json` | ~? (group 1) | **ActorDefinition** — bridges Item → Appearance. Its `snoAppearance` and, crucially, its **name** carry the link. |
| `base/meta/Appearance/*.app.json` | 66,562 | **AppearanceDefinition** — the mesh assets the builder loads. Carry **no** item/name back-reference. |
| `enUS_Text/meta/StringList/Item_*.stl.json` | 11,319 | **StringListDefinition** — where the display name actually lives. |
| `base/meta/AppearanceSet/*.aps.json` | 20 | Per-class `<Class><Gender>_Armor.aps` — flat list of that class's armor appearances. No item linkage; not useful here. |
| `base/meta/Affix/*` | 6,127 | Affix name fragments ("of Frost"). Not needed for base display names. |
| `base/meta/Aspect/*` | 608 | Legendary aspect powers. Not the item name. |
| `base/meta/StringList/` | 1 | A stray "Axe Bad Data" file — the *real* StringLists are under `enUS_Text/`, not here. |
| `base/CoreTOC.dat.json` | 43 MB | Global SNO `id → (group, name)` table — 849,182 entries. Needed to resolve numeric SNO IDs to stems. |
| `incomingSnoReferences.json` | 50 MB | Reverse reference graph: `snoID → [snoIDs that reference it]`. The key to the reverse lookup. |

**Note:** there is **no** `ItemAppearance`, `String`, or `Localization`
SNO type. The brief's guessed names don't exist; the real layout is
`Item` + `Actor` + `Appearance` + `StringList` as above.

The `ItemDefinition` JSON has **no name field at all** — no `pszName`,
`pszDisplayName`, `gbidName`, etc. It is pure gameplay data
(`snoActor`, `snoItemType`, `eComponentStyleType`, `dwComponentStyle`,
affix lists…). The name is *only* in the matching StringList file.

---

## 2. Chain trace from a single item

Worked example, a weapon (the cleanest case):

**Step 1 — the appearance.** `base/meta/Appearance/axe_uniq01.app`
(`__snoID__` 557509, `__type__` `AppearanceDefinition`). It carries no
back-reference to anything.

**Step 2 — the appearance has a same-stem Actor.**
`base/meta/Actor/axe_uniq01.acr` (`__snoID__` 557511). Its
`snoAppearance` field points back at `axe_uniq01.app`. (Forward direction
— not what we need, but confirms the pairing.)

**Step 3 — reverse the Actor → Item edge via `incomingSnoReferences`.**
`incomingSnoReferences["557511"]` → `[ ...one id... ]`. Resolving that id
through `CoreTOC` → group `73` (`Item`), name
`1HAxe_Legendary_Generic_001`.

**Step 4 — the Item → name via StringList (filename convention).**
`enUS_Text/meta/StringList/Item_1HAxe_Legendary_Generic_001.stl.json`:

```json
{ "arStrings": [
  { "szLabel": "Name", "szText": "Bearded Axe", "hLabel": 4062401 } ] }
```

→ **"Bearded Axe"**.

The StringList filename is *always* `Item_<itemStem>.stl.json` — verified
across every sampled item. No ID lookup needed for this hop; just string
concatenation.

### The deviation: armor does NOT keep the appearance on its Actor

For **armor**, the Item's Actor exists but its `snoAppearance` points at
a **placeholder** mesh, not the player mesh:

```
Helm_Cosmetic_Barb_150_stor.itm  → snoActor → Helm_Cosmetic_Barb_150_stor.acr
                                            → snoAppearance → Helmet_flippy.app   ← placeholder
Helm_Unique_Barb_100.itm         → snoActor → HLM_sets51.acr
                                            → snoAppearance → Helmet_flippy.app   ← placeholder
```

The real player mesh `barM_sets51_HLM.app` is **never in the SNO
reference graph from any item** — `incomingSnoReferences` for an armor
`.app` only ever returns the base player Actor (`barbarianM`) and the
class `AppearanceSet` (`BarbM_Armor`). The game resolves the worn mesh
at runtime by **string convention**, combining the wearer's
class/gender with the item's component style.

The bridge that *does* survive is the **Actor's name**:

* Item `Helm_Unique_Barb_100` → Actor **`HLM_sets51`**
* Item `Helm_Normal_Generic_007` → Actor **`HLM_base07`**
* Item `Helm_Magic_Barb_025_Crafted_L9` → Actor **`HLM_crft25`**
* Item `Chest_Unique_Barb_100` → Actor **`TRS_sets52`**

i.e. non-cosmetic armor Actors are named `<SLOT>_<styleToken><number>`.
The player appearance is `<classGender>_<styleToken><number>_<SLOT>.app`.
So `barM_sets51_HLM.app` ⇄ Actor `HLM_sets51` ⇄
`incomingSnoReferences[HLM_sets51]` → the items. Slot tokens:
`HLM`/`TRS`/`GLV`/`LEG`/`BTS` (chest meshes use suffix `_TRS` *or*
`_BDY`; the Actor prefix is always `TRS`).

**Cosmetic armor** (style tokens `stor`, `dlux`/`dulx`, `pvpa`) has **no
`<SLOT>_<token><num>` Actor at all** — the Actor is named after the item
itself (`Helm_Cosmetic_Barb_150_stor`) and still points at the
placeholder. Those resolve only by matching the item's *name*
(see §4).

`dwComponentStyle` (an integer on the Item) is **not** a usable key:
145 of 163 helm component-style numbers are shared by 2–8 items, and the
number ranges overlap across tokens (e.g. `uniq` and `dlux` both occupy
~95–101). The discriminator is the **token**, which only exists as part
of the Actor/appearance *name* — there is no structured token field
(`eComponentStyleType` is `1` for essentially all armor). The resolver
therefore ignores `dwComponentStyle` entirely and works purely on
name-parsing + the reference graph.

---

## 3. Cross-slot sampling

Resolved by `resolve.py` (full output in `sample_resolved.json`). Every
row is a real `.app` → resolved in-game name:

| Slot | `.app` stem | Token | Method | Item stem | **Display name** |
|---|---|---|---|---|---|
| Helm | `barM_base02_HLM` | base | actor-ref | `Helm_Rare_Generic_002` | Highland Circlet |
| Helm | `barM_crft25_HLM` | crft | actor-ref † | `Helm_Magic_Barb_025_Crafted_L9` | Wanderer's Helm |
| Helm | `barM_sets50_HLM` | sets | actor-ref ‡ | `Helm_Legendary_Generic_050` | Boneweave Helm |
| Helm | `barM_uniq101_HLM` | uniq | actor-ref | `Helm_Unique_Rogue_002` | Deathmask of Nirmitruq |
| Chest | `barM_sets50_TRS` | sets | actor-ref ‡ | `Chest_Legendary_Generic_050` | Boneweave Armor |
| Chest | `BarM_stor211_TRS` | stor | cosmetic-name | `Chest_Cosmetic_Barbarian_211_stor` | Demonheart Carapace |
| Gloves | `barM_uniq101_GLV` | uniq | actor-ref | `Gloves_Unique_Barb_101` | Bane of Ahjad-Den |
| Pants | `barM_dlux100_LEG` | dlux | cosmetic-name | `Pants_Cosmetic_Barbarian_dlux100_stor` | Warm Regards |
| Boots | `barM_crft25_BTS` | crft | actor-ref † | `Boots_Magic_Barb_025_Crafted_L5` | Wanderer's Boots |
| Main Hand | `axe_uniq01` | — | actor-ref | `1HAxe_Legendary_Generic_001` | Bearded Axe |
| Main Hand | `Axe_stor008` | — | actor-ref | `Axe_stor008` | Protean Axe |
| Off-Hand | `shield_uniq06` | — | actor-ref | `1HShield_Unique_Necro_100` | Lidless Wall |
| Off-Hand | `shield_base02` | — | actor-ref | `1HShield_Rare_Generic_002` | Heavy Shield |

† **Class-preference tiebreak applied.** `HLM_crft25` is referenced by
7 items — one crafted helm per class plus a Generic. Since the mesh is
`barM_*` (Barbarian Male), the resolver should prefer the item whose
name contains the class word (`Barb`) → *Wanderer's Helm*, not
*Drifter's Hood* (the Rogue variant). Without the tiebreak the join is
non-deterministic.

‡ **Genuine same-mesh ambiguity.** `HLM_sets50` is referenced by *both*
`Helm_Legendary_Generic_050` ("Boneweave Helm") and
`Helm_Unique_Generic_003` ("Godslayer Crown") — one mesh, two different
items, both Generic so no class hint helps. See §4.

The base-game *base/Generic* items resolve to mundane names ("Heavy
Shield", "Hand Axe", "Helm"); store/unique items resolve to the
recognizable names a D4 player knows ("Lidless Wall", "Bane of
Ahjad-Den"). All three brief-requested categories verified:
common (`Helm` / `Buckler`), unique (`Lidless Wall`), class-specific
(`Wanderer's Helm`).

---

## 4. Fallback story

Measured over the **barM** appearance set (715 armor + 656
weapon/off-hand `.app` files):

| Outcome | Armor | Weapon/OH | Meaning |
|---|---:|---:|---|
| Resolved to a name | 678 (94.8 %) | 629 (95.9 %) | Clean hit. |
| `no-actor` | 27 | 5 | No `<SLOT>_<tok><num>` Actor exists. |
| `cosmetic-miss` | 10 | — | Cosmetic token, no matching cosmetic item. |
| `actor-no-item` | — | 17 | Actor exists but no Item references it. |
| Actor matched, item has no StringList | 37 total `unnamed` rows (incl. above) | | Item exists but `Item_<stem>.stl` missing/empty. |

**Overall ≈ 95 % resolve to a real in-game name.** The ~5 % that miss
are not a flaw in the algorithm — they are genuinely nameless assets:

* **VFX / sub-mesh fragments** — `shield_stor016_smoke_mesh`,
  `axe_stor061_swirlMesh`, `sword_stor077_SmokeSource`,
  `dagger_stor074_Shared_smokeCloth`. These are effect meshes parented
  to a real weapon, not equippable items. (`actor-no-item`.)
* **Data gaps** — `barM_gnrc125_HLM` has no `HLM_gnrc125` Actor even
  though `HLM_gnrc126/127/129` exist; `TRS_uniq101` is absent while
  `HLM/GLV/LEG_uniq101` exist. Unreleased / placeholder meshes.
* **Test assets** — `barM_test999_TRS`, `*_TestLook`.
* **Unreleased cosmetics** — `barM_pvpa75_*` (PvP armor with no shipped
  item).

**Fallback ladder** (in priority order):

1. Class-preference tiebreak when an Actor maps to multiple items.
2. Quality tiebreak for same-mesh collisions
   (`Unique > Set > Legendary > Rare > Magic > Normal`) — or expose both
   names. ("Godslayer Crown / Boneweave Helm".)
3. If no item resolves → **fall back to the current SNO stem** (today's
   behaviour), optionally suffixed with a small `[asset]` tag so the
   user knows it's unnamed. This is the right home for VFX fragments and
   test meshes — they *should* show their stem.

No "internal designer name" field exists to fall back to — the
ItemDefinition has no name of any kind — so the SNO stem is the only
floor.

---

## 5. Scale and performance estimate

| Quantity | Value |
|---|---|
| Equipment armor `.app` files (16 class-genders) | ~8,870 |
| Weapon + off-hand `.app` files (ungendered) | ~875 |
| **Total equipment appearances to index** | **~9,745** |
| `ItemDefinition` files | 11,428 |
| `Item_*.stl.json` name files | 11,319 — **5.8 MB** total |
| `CoreTOC.dat.json` | 43 MB (849,182 id→name entries) |
| `incomingSnoReferences.json` | 50 MB |

**Cold-build cost (measured):**

* Parse `CoreTOC` → `id→(group,name)` + `actorName→id`: **~1 s**.
* Parse `incomingSnoReferences`: **~1 s**.
* Resolve all ~9,745 appearances (in-memory dict/graph lookups): **< 1 s**.
* Read item-name StringLists — this is the bottleneck:
  * Reading **all 11,319** single-threaded: **38 s** (tiny ~500-byte
    files, syscall-bound).
  * With an 8-worker `ThreadPoolExecutor` (the pattern `anim_lookup.py`
    already uses): **≈ 5–8 s**.
  * Better: resolve first, then read only the **~3–5 K** StringLists for
    items actually referenced → **≈ 2–4 s** threaded.

**Total cold build ≈ 8–12 s. Warm (disk-cached) ≈ milliseconds.**

**Comparison to the animation index:** `anim_lookup.py` scans ~45 K
`.ani.json` files. The item index reads two big JSONs (~2 s) + ~5–11 K
small files — **same order of magnitude, marginally cheaper**. The
existing 3-layer cache architecture transfers directly.

**Resulting index size:** ~9,745 `stem → name` strings, or — since the
non-cosmetic resolution is shared across all 16 class-genders by Actor —
it can instead be keyed by `(token, number, slot)` / Actor name and be
even smaller. Either way **< 1 MB** as a JSON cache.

---

## 6. Localization notes

* Localized text is **wholly separate** from gameplay SNO data, under
  `d4data/json/<locale>_Text/meta/StringList/`.
* This d4data checkout ships **English only** — `enUS_Text` is the
  *only* `*_Text` directory present (there is also `enUS_Speech`, voice
  audio, irrelevant here).
* A full multi-locale extraction would add sibling dirs
  (`deDE_Text/`, `frFR_Text/`, …), each with a parallel
  `meta/StringList/Item_*.stl.json` tree. The item *stem* and SNO IDs
  are locale-independent, so the **only** locale-dependent input is the
  StringList directory path.
* **v1 ships English-only.** Broader locale support is a one-parameter
  change (the `<locale>_Text` dir) — *trivially extensible*, but there
  is no other-locale data on disk in this checkout to test against.

---

## 7. Recommended implementation strategy

### 7.1 Where the lookup lives
New module **`d4extract/gui/item_lookup.py`**, structured after
`anim_lookup.py`: a frozen dataclass (`ItemNameInfo` or just a flat
`dict[str, str]`), an on-disk JSON cache, an in-memory cache keyed by
resolved d4data path, and a public `get_or_build_index()` /
`resolve_display_name(app_stem)` API.

### 7.2 The resolution algorithm (the actual content of the prompt)
Build, from d4data:

1. From `CoreTOC.dat.json`: `idmap = id → (group, name)` and
   `actorByName = name.lower() → id` (group `1`).
2. Load `incomingSnoReferences.json`.
3. Item-name resolver: `Item_<stem>.stl.json` → `arStrings` entry with
   `szLabel=="Name"` → `szText`.
4. Cosmetic-item index: scan `Item/*_Cosmetic_*.itm.json`, key
   `(slotWord, classWord, number) → itemStem`.
5. For each equipment `.app` stem:
   * **Weapon / off-hand** (`Axe_…`, `Shield_…`, `offHand…`, etc.):
     look up an Actor of the *same stem* → `incomingSnoReferences` →
     Item(s) (group 73).
   * **Armor non-cosmetic** (`base`/`crft`/`sets`/`gnrc`/`uniq`): parse
     `<classGender>_<token><num>_<SLOT>`; build Actor name
     `<SLOT>_<token><num>` (try zero-padded and unpadded);
     `incomingSnoReferences[actor]` → Item(s).
   * **Armor cosmetic** (`stor`/`dlux`/`dulx`/`pvpa`): look up the
     cosmetic-item index by `(slot, class, number)`.
   * Multiple items → **class-preference tiebreak**, then **quality
     tiebreak**; no item → keep the SNO stem.
6. Resolve chosen item stem → display name; build `stem → name` dict.

### 7.3 When the index builds
Background build at app startup, mirroring `AnimIndexBuildWorker` (the
project already starts that preemptively). Cold ~8–12 s is hidden behind
the splash / first interaction; warm is instant. Acceptable to fall back
to raw stems until the build completes.

### 7.4 Caching
On-disk JSON cache exactly like `_anim_index_cache_path` — under
`QStandardPaths.CacheLocation`, keyed by an md5 of the d4data path,
invalidated by the mtime of `Appearance/`, the StringList dir, and
`CoreTOC.dat.json`. Warm load is a single small JSON read.

### 7.5 UI changes
* **`_display_name_for(sno_path)` in `builder_page.py`** is the single
  chokepoint — it currently returns the stem. Route it through
  `item_lookup`. That alone fixes the SlotPanel labels (which already
  flow through `set_equipped(..., _display_name_for(sno_path))`) and the
  per-piece export entry labels.
* **`PieceBrowser`** renders raw SNO strings from `_all_entries`. To show
  *and search by* display names it needs the lookup too: keep the SNO
  path as the value/key but display the resolved name, and feed the
  display name (plus the stem, as a secondary key) into the fuzzy
  filter so a search for "wanderer" hits `barM_crft25_HLM`.
* **`models.py`** — `SlotState` already has `equipped_name`;
  `CustomizationOption` already has `display_name`. **No dataclass
  change is strictly required for equipment.** If the piece browser
  wants a richer per-entry record it can use a small local
  dataclass/tuple — but per the brief, don't touch the production
  dataclasses unless the implementer decides a `PieceEntry`-style record
  is cleaner.

### 7.6 Fallback handling in the UI
Unresolved pieces fall back to the SNO stem (today's behaviour). Consider
a faint `[asset]` tag so VFX fragments / test meshes read as
intentionally-unnamed rather than a lookup bug.

### 7.7 Effort estimate
Research + sampling is done and the algorithm is concrete. Implementation
≈ **4–6 hours**: `item_lookup.py` + resolver (~2–3 h), disk cache +
startup worker (~1 h), wiring `_display_name_for` + PieceBrowser
display/search (~1–1.5 h), Qt-free unit tests over the resolver with a
tiny fixture (~0.5–1 h).

### 7.8 One prompt or split?
**One implementation prompt.** The chain is *not* trivial (there is no
single name field — collapsing into "just read field X" is wrong), but
it is fully understood and the algorithm above is concrete and bounded.
It does not warrant a 3-way research/impl/UI split. A single focused
prompt covering the `item_lookup.py` module + cache + the two UI wiring
points is the right size.

---

## 8. Open questions

1. **Class-preference tiebreak across all 8 classes.** Verified deeply
   for `barM`; the non-cosmetic path is class-agnostic (Actor shared
   across class-genders) so it generalizes, and a `sorM` spot-check
   confirmed appearance naming holds. The *cosmetic* `cos_index` is
   class-keyed and was only verified for Barbarian — the implementer
   should spot-check Druid/Paladin/Warlock cosmetics (class-name tokens
   vary: `Barb`/`Barbarian`, `Sorc`/`Sorcerer`, `Necro`/`Necromancer`,
   and one observed typo `dulx` for `dlux`).
2. **Same-mesh / multiple-item ambiguity is real, not a bug.** Some
   meshes (`HLM_sets50`) genuinely back two different items (a Legendary
   and a Unique recolor). The "display name" is inherently one-of-N
   there — decide whether to pick by quality or show both. Cannot be
   resolved from data; it's a UX call.
3. **Dependency on `incomingSnoReferences.json`.** This 50 MB file is a
   d4data build artifact (from `parse.js`). If a user's d4data checkout
   lacks it, the actor→item edge must instead be built by scanning all
   11,428 `Item/*.itm.json` for `snoActor` (~38 s single-threaded /
   ~6 s threaded — slower than the 1 s JSON load, but self-contained).
   Worth a guarded fallback.
4. **Other equippable categories not in scope here.** Rings/Amulets
   (`Ring`, `Amulet` items) and the builder's *Jewelry* customization
   slot were not traced — jewelry uses a different appearance naming
   family (`jwl##_<prefix>.app`). If jewelry display names are wanted,
   that family needs its own short trace.
5. **Runtime-only confirmation.** All findings are from static d4data
   JSON. The actual `.app` files the builder shows come from a live
   CASC extraction; a final check against an extracted catalog would
   confirm the appearance-stem naming matches the d4data
   `Appearance/` dir 1:1 (expected — the builder already filters CASC
   stems with the same `_HLM/_TRS/...` regex).
