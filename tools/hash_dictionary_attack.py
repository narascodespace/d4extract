"""Phase 2: Dictionary attack on D4 32-bit name hashes.

Phase 1 confirmed:
  hash = djb2(name, seed=0)            # type/full hash
  field_hash = hash & 0x0FFFFFFF       # field hash (28-bit mask)

Both variants are tested for every candidate, since:
  * dwSubObjectHash  -> looks like full 32-bit (values up to ~4.2e9)
  * dwSlotHash       -> small values, but those are short strings whose
                        full DJB2 happens to fit in 17 bits — *not* masked.
  * Bone dwHash      -> empirically full 32-bit
  * Generic field    -> 28-bit masked

Output: d4extract/data/hash_names.json with {decimal_hash: "name", ...}
"""
from __future__ import annotations

import itertools
import json
import string
import struct
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DICT_PATH = ROOT / "d4data" / "dict.txt"
ENGLISH_DICT_PATH = ROOT / "d4data" / "english_dict.txt"
NAMES_FIELDS = ROOT / "d4data" / "names" / "fields.txt"
NAMES_TYPES = ROOT / "d4data" / "names" / "types.txt"
SAMPLES_META = ROOT / "d4extract" / "samples" / "base" / "meta" / "Appearance"
SAMPLES_PAYLOAD = ROOT / "d4extract" / "samples" / "base" / "payload" / "Appearance"
OUT_PATH = ROOT / "d4extract" / "data" / "hash_names.json"

MASK28 = 0x0FFFFFFF


# --------------------------------------------------------------------------- #
# Hash                                                                        #
# --------------------------------------------------------------------------- #

def djb2(s: str) -> int:
    h = 0
    for c in s.encode("utf-8"):
        h = ((h << 5) + h + c) & 0xFFFFFFFF
    return h


# --------------------------------------------------------------------------- #
# Targets                                                                     #
# --------------------------------------------------------------------------- #

# dwSlotHash values empirically observed (full 32-bit DJB2 of 3-letter abbrevs)
SLOT_HASHES: list[int] = [
    116929,   # hlm  -> Helmet
    121048,   # leg  -> Leg
    130201,   # trs  -> Torso
    110665,   # bts  -> Boots
    115849,   # glv  -> Gloves
    110143,   # bdy  -> Body
]

# dwSubObjectHash values from segments_truth.json
SUBOBJECT_HASHES: list[int] = [
    381309005, 1289864568, 787723793, 33351310, 4215393393,
    57083827, 532513039, 330399287, 1700103411,
]


# --------------------------------------------------------------------------- #
# Bone-hash harvest from sample .app payloads                                 #
# --------------------------------------------------------------------------- #

_BONE_STRUCT_SIZE = 232
_BONE_HASH_OFFSET = 0x20
_BONE_PARENT_OFFSET = 0x28
_BONE_LOCAL_TRS_OFFSET = 0x94


def _scan_bone_array(meta: bytes, payload: bytes) -> tuple[int, int] | None:
    n = len(meta)
    pn = len(payload)
    for off in range(0, n - 16, 4):
        h0, h1 = struct.unpack_from("<2I", meta, off)
        if h0 != 0 or h1 != 0:
            continue
        do, ds = struct.unpack_from("<2I", meta, off + 8)
        if do == 0 or ds == 0 or ds % _BONE_STRUCT_SIZE != 0:
            continue
        if do + ds > pn or do + _BONE_STRUCT_SIZE > pn:
            continue
        parent = struct.unpack_from("<h", payload, do + _BONE_PARENT_OFFSET)[0]
        if parent != -1:
            continue
        try:
            qx, qy, qz, qw = struct.unpack_from(
                "<4f", payload, do + _BONE_LOCAL_TRS_OFFSET,
            )
        except struct.error:
            continue
        if not (0.5 < qx * qx + qy * qy + qz * qz + qw * qw < 1.5):
            continue
        return do, ds // _BONE_STRUCT_SIZE
    return None


def harvest_bone_hashes() -> set[int]:
    out: set[int] = set()
    if not SAMPLES_META.is_dir():
        return out
    for meta_path in sorted(SAMPLES_META.glob("*.app")):
        payload_path = SAMPLES_PAYLOAD / meta_path.name
        if not payload_path.is_file():
            continue
        try:
            meta = meta_path.read_bytes()
            payload = payload_path.read_bytes()
        except OSError:
            continue
        info = _scan_bone_array(meta, payload)
        if info is None:
            continue
        do, count = info
        for i in range(count):
            base = do + i * _BONE_STRUCT_SIZE
            if base + _BONE_HASH_OFFSET + 4 > len(payload):
                break
            h = struct.unpack_from("<I", payload, base + _BONE_HASH_OFFSET)[0]
            if h:
                out.add(h)
    return out


# --------------------------------------------------------------------------- #
# Candidate generation                                                        #
# --------------------------------------------------------------------------- #

def load_words(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [
        line.strip() for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.startswith("#")
    ]


# Body-part / rig vocabulary curated for D4 / Blizzard rigs.
BODY_PARTS = [
    "root", "world", "origin", "master",
    "pelvis", "hips", "hip", "spine", "spine1", "spine2", "spine3",
    "chest", "ribcage", "torso", "body", "upperbody", "lowerbody",
    "neck", "neck1", "head", "skull", "jaw", "tongue", "eye", "eyes",
    "eyelid", "eyebrow", "ear", "nose", "mouth", "lip", "tooth",
    "clavicle", "scapula", "shoulder", "shoulderpad",
    "upperarm", "arm", "forearm", "elbow", "wrist", "hand", "palm",
    "thumb", "index", "middle", "ring", "pinky", "finger",
    "thigh", "upperleg", "leg", "calf", "lowerleg", "knee",
    "ankle", "foot", "ball", "toe", "heel",
    "tail", "wing", "horn", "tentacle", "claw", "fang", "spike",
    "fur", "mane", "trophy", "chain", "extras", "extra",
    "weapon", "shield", "scabbard", "sheath", "quiver",
    "cloak", "hood", "robe", "skirt", "cape",
    "cloth", "twist", "roll", "helper", "attach", "fx", "hardpoint",
    "ik", "fk", "pole", "target",
    "prop", "propa", "propb", "propc",
    "muzzle", "snout",
    "breast", "chestcloth",
    "pouch", "belt",
    "ponytail", "hair", "beard",
    "eyebrowl", "eyebrowr",
    "dummy", "facial", "face",
    "Bip01", "Bip001",
]

EQUIPMENT_SLOTS = [
    "helm", "helmet", "head", "hood", "mask",
    "chest", "torso", "body", "shirt", "robe",
    "gloves", "glove", "hand", "hands", "gauntlet", "gauntlets",
    "boots", "boot", "feet", "foot", "shoes",
    "pants", "legs", "leg", "leggings", "trousers", "greaves",
    "shoulder", "shoulders", "pauldron", "pauldrons",
    "belt", "waist", "girdle", "sash",
    "cloak", "cape", "back",
    "weapon", "mainhand", "offhand", "shield", "amulet", "ring", "ring1", "ring2",
]

# Prefixes/suffixes (Blizzard / DCC convention)
PREFIXES = [
    "", "l_", "r_", "L_", "R_", "left_", "right_", "Left_", "Right_",
    "lt_", "rt_", "_l_", "_r_",
    "b_", "B_", "bone_", "Bone_", "jnt_", "Jnt_", "Joint_",
    "Hero_", "hero_", "Bip01_", "Bip001_",
    "Mecha_", "Tail_",
]

SUFFIXES = [
    "", "_l", "_r", "_L", "_R",
    "_left", "_right", "_Left", "_Right",
    "_tip", "_end", "_root", "_helper", "_attach",
    "_twist", "_roll", "_cloth", "_fx", "_hardpoint",
    "_a", "_b", "_c", "_A", "_B", "_C",
    "_in", "_out", "_inner", "_outer",
    "_front", "_back", "_top", "_bot", "_bottom",
    "_jnt", "_Jnt", "_bone", "_Bone",
] + [f"_{i:02d}" for i in range(1, 16)] + [f"_{i}" for i in range(1, 11)]

# Casing transforms applied to base candidate strings.
def case_variants(s: str) -> list[str]:
    if not s:
        return [""]
    out = {s, s.lower(), s.upper()}
    if len(s) > 1:
        out.add(s[0].upper() + s[1:])
        out.add(s[0].lower() + s[1:])
    return list(out)


def expand_with_affixes(base: str) -> list[str]:
    """base + affix combinations.  Skips the empty/empty pair already in the seed list."""
    out: set[str] = set()
    for pre in PREFIXES:
        for suf in SUFFIXES:
            if not pre and not suf:
                continue
            out.add(f"{pre}{base}{suf}")
    return list(out)


def expand_pairs(parts: list[str]) -> list[str]:
    """Two-part combinations like 'arm_twist', 'spine_01_helper'."""
    seps = ["_", ""]
    out: set[str] = set()
    for a, b in itertools.product(parts, repeat=2):
        if a == b:
            continue
        for sep in seps:
            out.add(f"{a}{sep}{b}")
            out.add(f"{a}{sep}{b.capitalize()}")
            out.add(f"{a.capitalize()}{sep}{b.capitalize()}")
    return list(out)


def short_alpha_brute(min_len: int = 1, max_len: int = 4) -> list[str]:
    """All ASCII-lowercase strings of length [min_len..max_len]."""
    out: list[str] = []
    alpha = string.ascii_lowercase
    for L in range(min_len, max_len + 1):
        out.extend("".join(t) for t in itertools.product(alpha, repeat=L))
    return out


# --------------------------------------------------------------------------- #
# Attack                                                                      #
# --------------------------------------------------------------------------- #

def attack(targets: set[int]) -> dict[int, str]:
    """Hash every candidate string; return {target_hash: name} for any hits.

    Two kinds of match are tracked **per target T**:
      * exact:   djb2(s) == T              (full 32-bit identity)
      * masked:  djb2(s) & 0xFFFFFFF == T  (only when T < 2**28; T treated
                 as a possible 28-bit-masked field hash)

    Both are recorded and an exact match is preferred at resolution time.
    For ties (multiple candidates with the same hash) the shortest wins —
    accidental 32-bit collisions are rare so this mostly favours real
    short names like "bdy" over noisy affix-expanded look-alikes.
    """
    exact_hits: dict[int, str] = {}
    masked_hits: dict[int, str] = {}
    masked_targets = {t for t in targets if t < (1 << 28)}

    def feed(name: str) -> None:
        h = djb2(name)
        if h in targets:
            cur = exact_hits.get(h)
            if cur is None or len(name) < len(cur):
                exact_hits[h] = name
        m = h & MASK28
        if m in masked_targets:
            cur = masked_hits.get(m)
            if cur is None or len(name) < len(cur):
                masked_hits[m] = name

    seen: set[str] = set()

    def push(s: str) -> None:
        if s and s not in seen:
            seen.add(s)
            feed(s)

    # 1. Curated rigging/equipment vocabulary -- aggressive affix expansion.
    rig_seed: list[str] = []
    for w in BODY_PARTS + EQUIPMENT_SLOTS:
        rig_seed.extend(case_variants(w))
    rig_seed = sorted(set(rig_seed))
    rig_estimate = len(rig_seed) * len(PREFIXES) * len(SUFFIXES)
    print(f"[1] rig vocabulary: {len(rig_seed)} entries "
          f"(with affixes -> ~{rig_estimate:,} strings)")
    for w in rig_seed:
        push(w)
        for v in expand_with_affixes(w):
            push(v)

    # 2. Pair combinations of rig vocabulary
    print(f"[2] rig pair combinations")
    for v in expand_pairs(BODY_PARTS + EQUIPMENT_SLOTS):
        push(v)
        for v2 in expand_with_affixes(v):
            push(v2)

    # 3. Short-string brute force (covers 3-letter slot abbreviations + tags)
    print(f"[3] alpha brute force (1..4 chars)")
    for v in short_alpha_brute(1, 4):
        push(v)

    # 4. d4data SDK field/type names -- single pass + case variants.
    sdk_names = load_words(NAMES_FIELDS) + load_words(NAMES_TYPES)
    print(f"[4] d4data SDK field/type names ({len(sdk_names)} words)")
    for w in sdk_names:
        for v in case_variants(w):
            push(v)

    # 5. Full d4data dict.txt + english dict -- single pass + case variants.
    dict_words = load_words(DICT_PATH) + load_words(ENGLISH_DICT_PATH)
    print(f"[5] dict.txt + english_dict ({len(dict_words)} words)")
    for w in dict_words:
        for v in case_variants(w):
            push(v)
    # 6. Affix the *short* dictionary words (where rig vocabulary often lives).
    short_dict = [w for w in dict_words if 2 <= len(w) <= 8]
    print(f"[6] affixed short dict ({len(short_dict)} x affixes)")
    for w in short_dict:
        for v in expand_with_affixes(w):
            push(v)

    print(f"     candidates tested: {len(seen):,}")
    print(f"     exact matches:  {len(exact_hits)} / {len(targets)}")
    masked_only = {t: masked_hits[t] for t in masked_hits
                   if t not in exact_hits}
    print(f"     masked-only:    {len(masked_only)} (low-confidence, "
          f"~1 false positive per target by chance)")
    return exact_hits, masked_only


# --------------------------------------------------------------------------- #
# Driver                                                                      #
# --------------------------------------------------------------------------- #

def main() -> int:
    print("Phase 2: D4 hash dictionary attack\n")

    bone_hashes = harvest_bone_hashes()
    print(f"Harvested {len(bone_hashes)} unique bone dwHash values from samples\n")

    targets: set[int] = set()
    targets.update(SLOT_HASHES)
    targets.update(SUBOBJECT_HASHES)
    targets.update(bone_hashes)

    exact, masked_only = attack(targets)

    # Sanity-check: every saved name must hash back to its key.
    resolved: dict[int, str] = {}
    for h, name in exact.items():
        if djb2(name) == h:
            resolved[h] = name
        else:
            print(f"  DROP exact {h:#010x} -> {name!r}")
    speculative: dict[int, str] = {}
    for h, name in masked_only.items():
        if (djb2(name) & MASK28) == h:
            speculative[h] = name

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    serialised = {str(k): v for k, v in resolved.items()}
    OUT_PATH.write_text(json.dumps(serialised, indent=2, sort_keys=True), encoding="utf-8")
    spec_path = OUT_PATH.with_name("hash_names_speculative.json")
    spec_path.write_text(
        json.dumps({str(k): v for k, v in speculative.items()}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # ---- summary ------------------------------------------------------------
    def report(name: str, targets: list[int]) -> None:
        n_hit = sum(1 for t in targets if t in resolved)
        print(f"\n=== {name}: {n_hit}/{len(targets)} resolved ===")
        for t in targets:
            v = resolved.get(t)
            tag = f'"{v}"' if v else "(unresolved)"
            print(f"  {t:>10}  0x{t:08x}  -> {tag}")

    report("dwSlotHash", SLOT_HASHES)
    report("dwSubObjectHash", SUBOBJECT_HASHES)

    bone_resolved = sum(1 for h in bone_hashes if h in resolved)
    print(f"\n=== Bone dwHash: {bone_resolved}/{len(bone_hashes)} resolved ===")
    for h in sorted(bone_hashes):
        if h in resolved:
            print(f'  {h:>10}  0x{h:08x}  -> "{resolved[h]}"')
    print(f"\nWrote {len(resolved)} confident entries to {OUT_PATH}")
    print(f"Wrote {len(speculative)} low-confidence (masked-only) entries to {spec_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
