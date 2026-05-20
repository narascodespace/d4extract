---
name: d4-equipment-hashing
description: "Diablo IV hash function specification (DJB2), equipment slot mappings, SubObjectNameInfo structure, player class prefixes, and model naming conventions for filtering and categorizing appearance files. Covers the three DJB2 variants (type/full 32-bit, field/masked 28-bit, gbid/lowercase 32-bit), the confirmed dwSlotHash-to-slot mappings (hlm/trs/leg/bts/glv/bdy), the SubObjectNameInfo struct layout (40 bytes, 11 fields), player class filename prefixes (barM/necF/druM/rogF/sorF/sptM), and the hash_names.py runtime resolver. Use this skill whenever working on the character builder tab, filtering models by equipment slot or character class, resolving hash values to names, expanding the dictionary attack for submesh/bone names, implementing equipment slot filters in the model list, categorizing CASC catalog entries by class and slot, or building any UI that groups appearance files by equipment type. Also trigger when the user asks about D4 hashing, DJB2, slot hashes, class prefixes, model naming conventions, or how to identify what equipment slot an .app file belongs to."
---

# D4 Equipment Hashing & Model Categorization

This skill documents how Diablo IV identifies equipment slots, character classes, and submesh parts via hashing — and how the extraction pipeline uses this to categorize and filter models. The hash function has been fully identified and verified. The slot mappings are confirmed. The character builder tab will rely on both to filter the 13K+ model catalog down to class-specific equipment.

## The hash function: DJB2

D4 uses DJB2 (Daniel J. Bernstein's hash) with seed 0 everywhere. There are three variants that differ only in pre-processing and post-processing:

```python
def djb2(s: str) -> int:
    """Core DJB2 hash. Seed=0, case-sensitive, no null terminator."""
    h = 0
    for c in s.encode("ascii"):
        h = ((h << 5) + h + c) & 0xFFFFFFFF
    return h
```

### Three variants

| Variant | Pre-processing | Post-processing | Use site | Example |
|---------|---------------|-----------------|----------|---------|
| **Type hash** | none | none (full 32-bit) | Struct/class names in definitions.json, `dwSubObjectHash` values, bone `dwHash` values, `dwNPCComponentNameHash`, `dwDetailHash` | `djb2("SubObject")` → `0xF5780E21` |
| **Field hash** | none | `& 0x0FFFFFFF` (mask to 28 bits) | Field names in definitions.json, `dwSlotHash` values | `djb2("hlm") & 0x0FFFFFFF` → `116929` |
| **GBID hash** | `s.lower()` | none (full 32-bit) | Asset ID checksums (gbid) | `djb2("SomeAsset".lower())` |

This was verified against 8,650 field name pairs and 3,216 type name pairs from `d4data/definitions/!!D4FieldChecksums.yml` and `!!D4Checksums.yml`. Zero mismatches.

Reference C++ implementations live at `d4data/checksum/checksum.cpp` (type), `field_checksum.cpp` (field, with `& 0xFFFFFFF`), and `gbid_checksum.cpp` (lowercase + full 32-bit).

### Determining which variant a hash value uses

When you encounter an unknown hash value in the binary:

- **Field names** (the names of struct fields like `dwHash`, `nLOD`, `dwSlotHash`) use the **masked** variant
- **Values stored in those fields** (the actual identity hashes) typically use the **full 32-bit type** variant
- **Exception**: `dwSlotHash` values happen to be small enough to fit in 28 bits because the input strings are short (3 letters like "hlm"), so they work with either variant — but they are technically field hashes (masked)
- If a value exceeds `0x0FFFFFFF`, it must be a full 32-bit type hash

## Equipment slot mappings (confirmed)

These six `dwSlotHash` values are fully verified — the hash function produces them from the 3-letter abbreviations:

| dwSlotHash | Hex | DJB2 input | Pretty name | File suffix |
|-----------|-----|-----------|-------------|-------------|
| 116929 | 0x0001c8c1 | `hlm` | Helmet | `_HLM.app` |
| 130201 | 0x0001fc99 | `trs` | Torso | `_TRS.app` |
| 121048 | 0x0001d8d8 | `leg` | Legs | `_LEG.app` |
| 110665 | 0x0001b049 | `bts` | Boots | `_BTS.app` |
| 115849 | 0x0001c489 | `glv` | Gloves | `_GLV.app` |
| 110143 | 0x0001ae3f | `bdy` | Body | (creatures/mounts) |

The `dwSlotHash` lives inside the `SubObjectNameInfo` struct embedded in each SubObject at offset +0x38 in the meta file. Every SubObject in the same .app file shares the same slot hash — it's a model-level tag, not a per-submesh differentiator.

### Where slot hashes live in the binary

SubObject (240 bytes) layout, relevant fields:

```
+0x00  uint32  dwFlags
+0x38  SubObjectNameInfo  tNameInfo       ← 40-byte embedded struct
  +0x38+0x00  uint32  eType             (0 = none, 5 = equipment)
  +0x38+0x10  uint32  dwSlotHash        ← equipment slot identifier
  +0x38+0x14  uint32  dwNPCComponentNameHash
  +0x38+0x18  uint32  dwDetailHash
+0x60  int32   nMaterialIndex
+0x64  uint32  dwSubObjectHash           ← per-submesh identity hash
+0x68  int32   nVertBufferIndex
+0x6C  int32   nIndexBufferIndex
```

The parser reads `dwSubObjectHash` from `meta[so_start + 0x64]` and `dwSlotHash` from `meta[so_start + 0x48]` (i.e., `0x38 + 0x10`).

### SubObjectNameInfo struct (40 bytes)

| Offset | Size | Field | Purpose |
|--------|------|-------|---------|
| 0x00 | 4 | eType | 0 = no equipment info, 5 = equipment piece |
| 0x04 | 4 | dwFlags | Flags (1 = alternate/hidden variant) |
| 0x08 | 1 | dwPersona | Persona ID |
| 0x09 | 1 | dwState | State ID |
| 0x0C | 4 | dwStyle | Style/variant number (e.g., 196 for store item 196) |
| 0x10 | 4 | dwSlotHash | Equipment slot (DJB2 of "hlm"/"trs"/etc.) |
| 0x14 | 4 | dwNPCComponentNameHash | NPC component identity (full 32-bit hash) |
| 0x18 | 4 | dwDetailHash | Sub-variant/detail identifier |
| 0x1C | 4 | unk_c15f7d2 | Unknown |
| 0x20 | 4 | unk_92fdd14 | Unknown |
| 0x24 | 4 | unk_b0b7d46 | Unknown |

For player equipment: `eType` = 5, `dwSlotHash` is always populated, `dwStyle` encodes the store/set variant number.

For creatures and props: `eType` = 0 or 5 (varies), `dwSlotHash` = 0 (no equipment slot), `dwNPCComponentNameHash` may identify the creature part type.

## Player class prefixes in filenames

D4 appearance files follow a strict naming convention that encodes class and gender:

| Prefix | Class | Gender |
|--------|-------|--------|
| `barM_` / `barF_` | Barbarian | Male / Female |
| `necM_` / `necF_` | Necromancer | Male / Female |
| `druM_` / `druF_` | Druid | Male / Female |
| `rogM_` / `rogF_` | Rogue | Male / Female |
| `sorM_` / `sorF_` | Sorcerer/ess | Male / Female |
| `sptM_` / `sptF_` | Spiritborn | Male / Female |

The full pattern is: `{classGender}_{setName}_{SLOT}.app`

Examples:
- `necF_stor229_HLM.app` → Necromancer Female, store set 229, Helmet
- `barM_base01_TRS.app` → Barbarian Male, base set 01, Torso
- `druF_uniq99_GLV.app` → Druid Female, unique set 99, Gloves

### Set naming patterns

| Pattern | Meaning | Example |
|---------|---------|---------|
| `base##` | Default/starter gear | `barF_base01_BTS` |
| `stor###` | Store/MTX cosmetic | `necF_stor229_HLM` |
| `uniq##` | Unique/legendary | `druM_uniq99_HLM` |
| `rare##` | Rare transmog | `rogF_rare05_TRS` |
| `set##` | Set item appearance | `sorM_set03_LEG` |
| `pvp##` | PvP reward gear | `barM_pvp01_GLV` |

## Character builder filtering strategy

For the character builder tab, models need to be filtered by class + slot from the 13K+ CASC catalog. Two complementary approaches:

### Approach 1: Filename parsing (fast, no binary reads)

Parse the SNO path directly — the naming convention is reliable for player equipment:

```python
import re

_CLASS_PATTERN = re.compile(
    r"^(bar|nec|dru|rog|sor|spt)(M|F)_",
    re.IGNORECASE,
)
_SLOT_SUFFIX = re.compile(
    r"_(HLM|TRS|LEG|BTS|GLV)\.app$",
    re.IGNORECASE,
)

def classify_equipment(sno_path: str) -> tuple[str, str, str] | None:
    """Return (class_abbrev, gender, slot) or None if not player equipment."""
    stem = sno_path.rsplit("/", 1)[-1]
    cls = _CLASS_PATTERN.match(stem)
    slot = _SLOT_SUFFIX.search(stem)
    if cls and slot:
        return cls.group(1).lower(), cls.group(2).upper(), slot.group(1).upper()
    return None
```

This works on the catalog listing without extracting any files — suitable for building the filter pills and initial categorization during catalog load.

### Approach 2: Binary slot hash (authoritative, requires meta read)

For already-loaded models, read `dwSlotHash` from the parsed `Submesh.slot_hash` field. This is the authoritative source and catches edge cases the filename convention might miss (e.g., mount armor with `bdy` slot, creature equipment).

```python
from d4extract.formats.hash_names import resolve_hash

slot_name = resolve_hash(submesh.slot_hash)  # "hlm", "trs", etc.
```

### Recommended character builder flow

1. **Catalog load**: parse all 13K SNO paths with `classify_equipment()` to build a class → slot → models index
2. **Class picker**: user selects Barbarian/Necromancer/etc. — filter to matching prefix
3. **Slot tabs**: Helmet / Torso / Legs / Boots / Gloves — filter by suffix
4. **Gender toggle**: M/F variant switch
5. **Model preview**: click to load in viewport (uses existing `LoadModelWorker`)
6. **Assembly**: combine pieces from different slots under shared skeleton template (see d4-skeleton-animation-assembly skill for `skeleton_template_id` matching)

## Runtime hash resolver

The module `d4extract/src/d4extract/formats/hash_names.py` loads `d4extract/data/hash_names.json` at import time and exports:

```python
def resolve_hash(hash_value: int) -> str | None:
    """Return the plaintext name for ``hash_value`` or ``None``."""
```

Currently resolves 32 entries: all 6 slot hashes plus 26 bone/submesh names from the dictionary attack. The lookup table can be expanded by running `tools/hash_dictionary_attack.py` with additional candidate strings.

### Expanding the dictionary

To crack more `dwSubObjectHash` or bone `dwHash` values:

1. Add candidate strings to the wordlists in `tools/hash_dictionary_attack.py`
2. Good sources for new candidates: material names from `d4data/json/base/meta/Material/*.mat.json` filenames, appearance file stems, artist-convention part names
3. Run the script — it tests DJB2 against target hashes and appends matches to `data/hash_names.json`
4. The runtime resolver picks up new entries on next import

## Key files

| File | Purpose |
|------|---------|
| `d4extract/src/d4extract/formats/hash_names.py` | Runtime resolver — `resolve_hash(int) -> str \| None` |
| `d4extract/data/hash_names.json` | Hash → name lookup table (32 verified entries) |
| `d4extract/data/hash_names_speculative.json` | Low-confidence masked-only hits (not loaded at runtime) |
| `d4extract/tools/hash_identify.py` | Phase 1 — algorithm identification script |
| `d4extract/tools/hash_dictionary_attack.py` | Phase 2 — brute-force name cracking |
| `d4data/checksum/checksum.cpp` | Reference C++ — type hash (full 32-bit DJB2) |
| `d4data/checksum/field_checksum.cpp` | Reference C++ — field hash (DJB2 & 0xFFFFFFF) |
| `d4data/checksum/gbid_checksum.cpp` | Reference C++ — gbid hash (lowercase + full DJB2) |
| `d4data/definitions/!!D4FieldChecksums.yml` | 8,650 field name → hash ground-truth pairs |
| `d4data/definitions/!!D4Checksums.yml` | 3,216 type name → hash ground-truth pairs |
| `d4extract/src/d4extract/formats/app_parser.py` | SubObject parsing — reads dwSubObjectHash at +0x64, dwSlotHash at +0x48 |
