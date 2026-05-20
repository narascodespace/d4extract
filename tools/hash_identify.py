"""Phase 1: Identify the D4 32-bit name hash function.

Strategy
--------
1. Harvest a large ground-truth set of (name -> hash) pairs from
   d4data/definitions/!!D4FieldChecksums.yml and !!D4Checksums.yml.
   Both files map ``0xHEX: ["name", ...]`` (list because of collisions).
2. Run a battery of candidate hash algorithms against each pair.
3. Report the algorithm(s) that match all (or the most) pairs.

Strong prior from d4data/checksum/*.cpp (community-contributed sources):
  type checksum  : DJB2 seed=0,  full 32-bit
  field checksum : DJB2 seed=0,  masked with 0x0FFFFFFF (28-bit)
  gbid checksum  : lowercase + DJB2 seed=0, full 32-bit
where "DJB2 seed=0" means h = 0; for c in s: h = ((h<<5) + h + c) & 0xFFFFFFFF.

This script does not assume those are correct; it tests them alongside other
common 32-bit hashes so the answer is verified, not asserted.
"""
from __future__ import annotations

import re
import struct
import sys
import zlib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIELD_YML = ROOT / "d4data" / "definitions" / "!!D4FieldChecksums.yml"
TYPE_YML = ROOT / "d4data" / "definitions" / "!!D4Checksums.yml"

PROVIDED_PAIRS = {
    "transform": 0x017752DC,
    "wp": 0x00000FC7,
    "dwHash": 0x01D669FF,
    "nParentIndex": 0x0A7299F0,
    "eVBFormat": 0x071BDE86,
    "dwFlags": 0x0C8534C8,
    "ptShapes": 0x075DE528,
    "nLOD": 0x003D9F6D,
}


# --------------------------------------------------------------------------- #
# Ground-truth harvesting                                                     #
# --------------------------------------------------------------------------- #

YML_LINE = re.compile(r"^\s*0x([0-9a-fA-F]+)\s*:\s*\[(.*)\]\s*$")
NAME_TOKEN = re.compile(r'"([^"]*)"')


def parse_checksum_yml(path: Path) -> list[tuple[int, list[str]]]:
    """Parse a !!D4*.yml file into [(hash_int, [name, ...]), ...]."""
    pairs: list[tuple[int, list[str]]] = []
    if not path.exists():
        return pairs
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = YML_LINE.match(raw)
        if not m:
            continue
        hash_int = int(m.group(1), 16)
        names = NAME_TOKEN.findall(m.group(2))
        if names:
            pairs.append((hash_int, names))
    return pairs


# --------------------------------------------------------------------------- #
# Candidate algorithms                                                        #
# --------------------------------------------------------------------------- #

def djb2(data: bytes, seed: int = 5381) -> int:
    h = seed & 0xFFFFFFFF
    for c in data:
        h = ((h << 5) + h + c) & 0xFFFFFFFF
    return h


def djb2a(data: bytes, seed: int = 5381) -> int:
    h = seed & 0xFFFFFFFF
    for c in data:
        h = (((h << 5) + h) ^ c) & 0xFFFFFFFF
    return h


def fnv1(data: bytes) -> int:
    h = 0x811C9DC5
    for c in data:
        h = (h * 0x01000193) & 0xFFFFFFFF
        h ^= c
    return h


def fnv1a(data: bytes) -> int:
    h = 0x811C9DC5
    for c in data:
        h ^= c
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def jenkins_oaat(data: bytes) -> int:
    h = 0
    for c in data:
        h = (h + c) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= (h >> 6)
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= (h >> 11)
    h = (h + (h << 15)) & 0xFFFFFFFF
    return h


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def murmur3_32(data: bytes, seed: int = 0) -> int:
    c1 = 0xCC9E2D51
    c2 = 0x1B873593
    h1 = seed & 0xFFFFFFFF
    nblocks = len(data) // 4
    for i in range(nblocks):
        k1 = struct.unpack_from("<I", data, i * 4)[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = _rotl(k1, 15)
        k1 = (k1 * c2) & 0xFFFFFFFF
        h1 ^= k1
        h1 = _rotl(h1, 13)
        h1 = (h1 * 5 + 0xE6546B64) & 0xFFFFFFFF
    tail = data[nblocks * 4 :]
    k1 = 0
    if len(tail) == 3:
        k1 ^= tail[2] << 16
    if len(tail) >= 2:
        k1 ^= tail[1] << 8
    if len(tail) >= 1:
        k1 ^= tail[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = _rotl(k1, 15)
        k1 = (k1 * c2) & 0xFFFFFFFF
        h1 ^= k1
    h1 ^= len(data)
    h1 ^= h1 >> 16
    h1 = (h1 * 0x85EBCA6B) & 0xFFFFFFFF
    h1 ^= h1 >> 13
    h1 = (h1 * 0xC2B2AE35) & 0xFFFFFFFF
    h1 ^= h1 >> 16
    return h1


def superfasthash(data: bytes) -> int:
    length = len(data)
    if length == 0:
        return 0
    h = length & 0xFFFFFFFF
    rem = length & 3
    n_main = length >> 2
    pos = 0
    for _ in range(n_main):
        h = (h + struct.unpack_from("<H", data, pos)[0]) & 0xFFFFFFFF
        tmp = (((struct.unpack_from("<H", data, pos + 2)[0]) << 11) ^ h) & 0xFFFFFFFF
        h = (((h << 16) & 0xFFFFFFFF) ^ tmp) & 0xFFFFFFFF
        h = (h + (h >> 11)) & 0xFFFFFFFF
        pos += 4
    if rem == 3:
        h = (h + struct.unpack_from("<H", data, pos)[0]) & 0xFFFFFFFF
        h ^= (h << 16) & 0xFFFFFFFF
        h ^= (data[pos + 2] << 18) & 0xFFFFFFFF
        h = (h + (h >> 11)) & 0xFFFFFFFF
    elif rem == 2:
        h = (h + struct.unpack_from("<H", data, pos)[0]) & 0xFFFFFFFF
        h ^= (h << 11) & 0xFFFFFFFF
        h = (h + (h >> 17)) & 0xFFFFFFFF
    elif rem == 1:
        h = (h + data[pos]) & 0xFFFFFFFF
        h ^= (h << 10) & 0xFFFFFFFF
        h = (h + (h >> 1)) & 0xFFFFFFFF
    h ^= (h << 3) & 0xFFFFFFFF
    h = (h + (h >> 5)) & 0xFFFFFFFF
    h ^= (h << 4) & 0xFFFFFFFF
    h = (h + (h >> 17)) & 0xFFFFFFFF
    h ^= (h << 25) & 0xFFFFFFFF
    h = (h + (h >> 6)) & 0xFFFFFFFF
    return h


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


# --------------------------------------------------------------------------- #
# Test harness                                                                #
# --------------------------------------------------------------------------- #

# (algorithm-id, function, applies-to-modes)
# A "mode" is a (case_transform, mask) tuple applied around the algorithm.
ALGORITHMS = [
    ("djb2_seed0",         lambda b: djb2(b, 0)),
    ("djb2_seed1",         lambda b: djb2(b, 1)),
    ("djb2_seed5381",      lambda b: djb2(b, 5381)),
    ("djb2a_seed0",        lambda b: djb2a(b, 0)),
    ("djb2a_seed5381",     lambda b: djb2a(b, 5381)),
    ("fnv1",               fnv1),
    ("fnv1a",              fnv1a),
    ("jenkins_oaat",       jenkins_oaat),
    ("murmur3_seed0",      lambda b: murmur3_32(b, 0)),
    ("murmur3_seed1",      lambda b: murmur3_32(b, 1)),
    ("murmur3_seed1337",   lambda b: murmur3_32(b, 0x1337)),
    ("murmur3_deadbeef",   lambda b: murmur3_32(b, 0xDEADBEEF)),
    ("superfasthash",      superfasthash),
    ("crc32",              crc32),
]

CASE_VARIANTS = [
    ("asis",     lambda s: s),
    ("lower",    lambda s: s.lower()),
    ("upper",    lambda s: s.upper()),
]

NULL_VARIANTS = [
    ("",         lambda b: b),
    ("\\0",      lambda b: b + b"\x00"),
]

MASKS = [
    ("none",     lambda h: h),
    ("28bit",    lambda h: h & 0x0FFFFFFF),
    ("shr4",     lambda h: h >> 4),
]


def run_match_matrix(pairs: list[tuple[str, int]]) -> list[tuple[str, int, int]]:
    """Return [(combo_id, matched, total), ...] sorted best-first."""
    results: list[tuple[str, int, int]] = []
    for algo_name, algo in ALGORITHMS:
        for case_name, case_fn in CASE_VARIANTS:
            for null_name, null_fn in NULL_VARIANTS:
                for mask_name, mask_fn in MASKS:
                    matched = 0
                    for name, expected in pairs:
                        try:
                            data = null_fn(case_fn(name).encode("utf-8"))
                            h = mask_fn(algo(data))
                        except Exception:
                            break
                        if h == expected:
                            matched += 1
                    combo = f"{algo_name:<18} case={case_name:<5} null={null_name or 'no':<3} mask={mask_name}"
                    results.append((combo, matched, len(pairs)))
    results.sort(key=lambda x: -x[1])
    return results


def main() -> int:
    field_pairs = parse_checksum_yml(FIELD_YML)
    type_pairs = parse_checksum_yml(TYPE_YML)
    print(f"Loaded {sum(len(n) for _, n in field_pairs)} field names "
          f"({len(field_pairs)} unique hashes) from {FIELD_YML.name}")
    print(f"Loaded {sum(len(n) for _, n in type_pairs)} type names "
          f"({len(type_pairs)} unique hashes) from {TYPE_YML.name}")

    # Flatten to (name, expected_hash) -- skip collision groups so we can
    # uniquely judge a hash function.  A collision group cannot match because
    # the algorithm can produce only one value per name.
    flat_field = [(names[0], h) for h, names in field_pairs if len(names) == 1]
    flat_type  = [(names[0], h) for h, names in type_pairs if len(names) == 1]

    # Add the user-provided sanity pairs (overlap with flat_field is fine).
    extra = [(n, h) for n, h in PROVIDED_PAIRS.items()]

    print("\n=== FIELD CHECKSUMS ===")
    print(f"Testing {len(flat_field)} unique field pairs + {len(extra)} provided pairs")
    field_results = run_match_matrix(flat_field + extra)
    print_top(field_results, n=8)

    print("\n=== TYPE CHECKSUMS ===")
    print(f"Testing {len(flat_type)} unique type pairs")
    type_results = run_match_matrix(flat_type)
    print_top(type_results, n=8)

    # Spot-check on the user-provided pairs with what we believe is the answer.
    print("\n=== SPOT CHECK: DJB2 seed=0, 28-bit mask (field hash) ===")
    all_ok = True
    for name, expected in PROVIDED_PAIRS.items():
        got = djb2(name.encode("utf-8"), 0) & 0x0FFFFFFF
        ok = got == expected
        all_ok &= ok
        flag = "OK " if ok else "FAIL"
        print(f"  {flag}  {name:<14} expected=0x{expected:08x}  got=0x{got:08x}")
    print(f"\nResult: {'ALL PROVIDED PAIRS MATCH' if all_ok else 'MISMATCH'}")
    return 0 if all_ok else 1


def print_top(results: list[tuple[str, int, int]], n: int = 5) -> None:
    for combo, matched, total in results[:n]:
        pct = 100.0 * matched / total if total else 0.0
        marker = "<-- PERFECT" if matched == total else ""
        print(f"  {matched:>5}/{total}  ({pct:5.1f}%)  {combo}  {marker}")


if __name__ == "__main__":
    sys.exit(main())
