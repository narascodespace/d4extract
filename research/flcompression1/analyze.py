"""Analyse flCompression=1 (and 2/4/6) .ani payloads vs the comp=3 baseline.

Adapted from research/flcompression5/{analyze,probe,valuetest}.py. Reads
the corpus extracted by ``extract.py`` plus the d4data meta JSONs,
parses every permutation with the unmodified ``anim_parser``, and prints:

  * AnimPayloadData header confirmation (13 arrays, offsets, sizes)
  * per-curve byte totals + bytes/bone/frame comparison table
  * structural probe — do comp=1 blobs satisfy the comp=3 size equations?
  * cross-mode blob sharing — within a mixed-mode payload, are curve
    blobs physically aliased across the comp=1/comp=3 boundary?
  * raw int16 quaternion magnitudes (xyzw / 32767)

Read-only: imports the production parser, changes nothing.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import numpy as np

_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from d4extract.formats.anim_parser import (  # noqa: E402
    ANIM_SLOT_ELEMENT_SIZES,
    ANIM_SLOT_NAMES,
    parse_anim,
)

D4DATA = Path(
    r"C:\Users\ryant\Documents\claude\Projects\diablo4analyzer\d4data\json"
)
META_DIR = D4DATA / "base" / "meta" / "Anim"
PAYLOAD_DIR = Path(__file__).resolve().parent / "extracted" / "base" / "payload" / "Anim"
# comp=3 reference reused from the comp=5 research corpus.
COMP3_REF = (
    Path(__file__).resolve().parents[1] / "flcompression5" / "extracted"
    / "barM_HTH_nav_idle" / "base" / "payload" / "Anim" / "barM_HTH_nav_idle.ani"
)

COMP1_PLAYER = [
    "barM_mount_hth_event_dismount_damage",
    "warM_mount_horse_reac_dismount",
    "spiF_mount_hth_event_dismount_damage",
    "spiF_gla_attk_centipede_core",
    "spiF_gla_attk_gorillaDefensive",
    "spiF_gla_attk_sky_basic_new",
    "spiF_gla_attk_Plains_Offense_temp",
    "spiF_gla_attk_guardianImpact_repeat",
    "spiF_gla_nav_evade",
    "spiF_hth_emote_taunt",
]
IGC = ["IGC_CBH_t3barbarianm_1010"]
CROSSMODE = [
    "druM_2HS_attk_StoneBurst",
    "palM_1hsShd_attk_impale",
    "druM_HTH_trav_ladder_climb_down",
]
OTHER = [
    "bandit_sword_nav_run",
    "bandit_sword_nav_walk",
    "treasuregoblin_nav_idle_unalert_outro_still",
    "warlock_tailStrike_attk_basic",
]


def _align4(x: int) -> int:
    return (x + 3) & ~3


def _align2(x: int) -> int:
    return (x + 1) & ~1


def _payload(name: str) -> Path:
    return PAYLOAD_DIR / f"{name}.ani"


def _meta(name: str) -> Path:
    return META_DIR / f"{name}.ani.json"


def _nperm(name: str) -> int:
    return len(
        json.loads(_meta(name).read_text("utf-8")).get("ptPermutations", [])
    )


# ── size-equation checkers (the comp=3 layout) ──────────────────────────


def check_translation(blob: bytes) -> str:
    if not blob:
        return "empty"
    n, count = len(blob), blob[0]
    if count <= 1:
        return "OK" if n == 16 else "size?"
    return "OK" if _align4(16 + count) + count * 12 == n else "MISMATCH"


def check_scale(blob: bytes) -> str:
    if not blob:
        return "empty"
    n, count = len(blob), blob[0]
    if count <= 1:
        return "OK" if n == 16 else "size?"
    return "OK" if _align4(2 + count) + count * 12 == n else "MISMATCH"


def check_rotation(blob: bytes) -> str:
    """comp=3 rotation: align2(2+count-1) + count*8, plus a 2-byte trailer."""
    if not blob:
        return "empty"
    n = len(blob)
    count = int.from_bytes(blob[0:2], "little")
    if count == 0:
        return "OK" if n in (4, 12) else "size?"
    if count == 1:
        return "OK" if n == 12 else "size?"
    expect = _align2(2 + count - 1) + count * 8 + 2   # +2 trailer
    return "OK" if expect == n else "MISMATCH"


def raw_quat_mags(blob: bytes) -> np.ndarray:
    if not blob or len(blob) < 4:
        return np.array([])
    count = int.from_bytes(blob[0:2], "little")
    if count == 0:
        return np.array([])
    if count == 1:
        if len(blob) < 10:
            return np.array([])
        q = np.array(struct.unpack_from("<4h", blob, 2), dtype=np.float64)
        return np.array([np.linalg.norm(q / 32767.0)])
    data_off = _align2(2 + count - 1)
    if data_off + count * 8 > len(blob):
        return np.array([])
    q = (np.frombuffer(blob, "<i2", count * 4, data_off)
         .astype(np.float64).reshape(count, 4) / 32767.0)
    return np.linalg.norm(q, axis=1)


# ── per-permutation analysis ────────────────────────────────────────────


def analyze(name: str, payload: Path, pidx: int) -> dict:
    perm = parse_anim(_meta(name), payload, permutation_index=pidx)
    h = perm.header
    t = sum(c.keys_size for c in h.translation_curves)
    r = sum(c.keys_size for c in h.rotation_curves)
    s = sum(c.keys_size for c in h.scale_curves)
    bpf = perm.bone_count * perm.keyframe_count
    return {"perm": perm, "t": t, "r": r, "s": s, "total": t + r + s,
            "bpbf": (t + r + s) / bpf if bpf else 0.0}


def main() -> None:
    rows = []

    print("=" * 80)
    print("HEADER CONFIRMATION + BYTE COUNTS")
    print("=" * 80)
    # comp=3 reference first.
    ref = parse_anim(
        META_DIR / "barM_HTH_nav_idle.ani.json", COMP3_REF, permutation_index=0,
    )
    rh = ref.header
    rt = sum(c.keys_size for c in rh.translation_curves)
    rr = sum(c.keys_size for c in rh.rotation_curves)
    rs = sum(c.keys_size for c in rh.scale_curves)
    rows.append(("barM_HTH_nav_idle p0", ref, rt, rr, rs,
                 (rt + rr + rs) / (ref.bone_count * ref.keyframe_count)))

    for name in COMP1_PLAYER + IGC + CROSSMODE + OTHER:
        pp = _payload(name)
        if not pp.is_file():
            print(f"!! missing payload: {name}")
            continue
        for pidx in range(_nperm(name)):
            try:
                a = analyze(name, pp, pidx)
            except Exception as exc:  # noqa: BLE001
                print(f"  {name} p{pidx}: PARSE FAILED — {exc}")
                continue
            perm = a["perm"]
            rows.append((f"{name} p{pidx}", perm, a["t"], a["r"], a["s"],
                         a["bpbf"]))

    # one detailed header dump for a comp=1 file
    detail = parse_anim(_meta("spiF_gla_attk_centipede_core"),
                        _payload("spiF_gla_attk_centipede_core"),
                        permutation_index=0)
    print(f"\nDetailed header — spiF_gla_attk_centipede_core p0 "
          f"(comp={detail.compression}, {detail.bone_count} bones, "
          f"{detail.keyframe_count} frames):")
    for i, arr in enumerate(detail.header.raw_arrays):
        es = ANIM_SLOT_ELEMENT_SIZES[i]
        cnt = arr.data_size // es if es else 0
        print(f"  [{i:>2}] {ANIM_SLOT_NAMES[i]:<20} off={arr.data_offset:>9} "
              f"size={arr.data_size:>7} ({cnt}x{es}B)")

    print("\n" + "=" * 80)
    print("BYTE-COUNT COMPARISON TABLE")
    print("=" * 80)
    hdr = (f"{'File':<42} {'comp':>4} {'bones':>5} {'frames':>6} "
           f"{'T':>8} {'R':>9} {'S':>7} {'b/bone/frame':>12}")
    print(hdr)
    print("-" * len(hdr))
    for label, perm, t, r, s, bpbf in rows:
        print(f"{label:<42} {perm.compression:>4} {perm.bone_count:>5} "
              f"{perm.keyframe_count:>6} {t:>8} {r:>9} {s:>7} {bpbf:>12.3f}")

    # ── structural probe ────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("STRUCTURAL PROBE — do comp=1 blobs satisfy comp=3 size equations?")
    print("=" * 80)
    for name in COMP1_PLAYER + IGC + OTHER:
        pp = _payload(name)
        if not pp.is_file():
            continue
        for pidx in range(_nperm(name)):
            perm = parse_anim(_meta(name), pp, permutation_index=pidx)
            tally: dict[str, dict[str, int]] = {"T": {}, "R": {}, "S": {}}
            for lbl, curves, chk in (
                ("T", perm.header.translation_curves, check_translation),
                ("R", perm.header.rotation_curves, check_rotation),
                ("S", perm.header.scale_curves, check_scale)):
                for c in curves:
                    v = chk(c.raw_keys)
                    tally[lbl][v] = tally[lbl].get(v, 0) + 1
            ts = "  ".join(
                f"{k}:{' '.join(f'{vv}={cc}' for vv, cc in sorted(v.items()))}"
                for k, v in tally.items())
            print(f"  {name} p{pidx} (comp={perm.compression}): {ts}")

    # ── cross-mode blob sharing ─────────────────────────────────────
    print("\n" + "=" * 80)
    print("CROSS-MODE BLOB SHARING — aliased curve blobs across comp boundary")
    print("=" * 80)
    for name in ["spiF_gla_nav_evade"] + CROSSMODE + ["warlock_tailStrike_attk_basic",
                                                      "bandit_sword_nav_run"]:
        pp = _payload(name)
        if not pp.is_file():
            continue
        perms = [parse_anim(_meta(name), pp, permutation_index=i)
                 for i in range(_nperm(name))]
        comps = [p.compression for p in perms]
        print(f"\n  {name}: permutation modes = {comps}")
        # compare every pair of differing-mode perms
        for i in range(len(perms)):
            for j in range(i + 1, len(perms)):
                if perms[i].compression == perms[j].compression:
                    continue
                shared = {"T": 0, "R": 0, "S": 0}
                nonempty = {"T": 0, "R": 0, "S": 0}
                for lbl, ai, aj in (
                    ("T", perms[i].header.translation_curves,
                     perms[j].header.translation_curves),
                    ("R", perms[i].header.rotation_curves,
                     perms[j].header.rotation_curves),
                    ("S", perms[i].header.scale_curves,
                     perms[j].header.scale_curves)):
                    for x, y in zip(ai, aj):
                        if x.keys_size:
                            nonempty[lbl] += 1
                        if (x.keys_size and x.keys_offset == y.keys_offset
                                and x.keys_size == y.keys_size):
                            shared[lbl] += 1
                tot_shared = sum(shared.values())
                tag = " <== ALIASED" if tot_shared else ""
                print(f"    p{i}(c{perms[i].compression}) vs "
                      f"p{j}(c{perms[j].compression}): "
                      f"T={shared['T']}/{nonempty['T']} "
                      f"R={shared['R']}/{nonempty['R']} "
                      f"S={shared['S']}/{nonempty['S']}{tag}")

    # ── quaternion magnitudes ───────────────────────────────────────
    print("\n" + "=" * 80)
    print("RAW INT16 QUATERNION MAGNITUDES (xyzw / 32767)")
    print("=" * 80)
    for name in COMP1_PLAYER + IGC + OTHER:
        pp = _payload(name)
        if not pp.is_file():
            continue
        for pidx in range(_nperm(name)):
            perm = parse_anim(_meta(name), pp, permutation_index=pidx)
            mags: list[float] = []
            for c in perm.header.rotation_curves:
                mags.extend(raw_quat_mags(c.raw_keys).tolist())
            if not mags:
                continue
            a = np.array(mags)
            nu = int(np.sum(np.abs(a - 1.0) < 0.02))
            print(f"  {name} p{pidx} (c{perm.compression}): {len(a)} quats  "
                  f"|q| min={a.min():.4f} med={np.median(a):.4f} "
                  f"max={a.max():.4f}  near-unit={100*nu/len(a):.1f}%")


if __name__ == "__main__":
    main()
