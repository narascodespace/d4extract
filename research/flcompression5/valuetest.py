"""Value test: decode flCompression=5 curves with the comp=3 decoder.

The structural probe showed comp=5 blobs satisfy the same size
equations as comp=3. That proves the *layout* matches but not the
*values*. This script checks whether the bytes actually decode to
sane animation data when run through the comp=3 reader:

  1. raw int16 quaternion magnitudes — comp=3 stores full xyzw quats
     normalised by 32767, so |q| ~= 1.0. A different scheme (smallest-3,
     8-bit, different normaliser) would give wildly non-unit magnitudes.
  2. rest-pose known-plaintext — decode frame 0 of each comp=5 perm
     and compare against the warM_base00 skeleton rest pose.
  3. cross-permutation constants — warM_2HM_attk_apocalypse holds
     comp=5 (p0) and comp=3 (p2) permutations of the SAME animation on
     the SAME skeleton. Bones static in both must store the same
     count=1 baseline. If the comp=5 baseline float32s equal the
     comp=3 ones, the constant-curve encoding is identical.

Read-only: it sets ``perm.compression = 3`` on parsed-in-memory
objects to bypass the decoder's guard. It never touches src/.
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
    decode_permutation,
    parse_anim,
    validate_against_rest_pose,
)
from d4extract.formats.app_parser import parse_app  # noqa: E402

D4DATA = Path(
    r"C:\Users\ryant\Documents\claude\Projects\diablo4analyzer\d4data\json"
)
META_DIR = D4DATA / "base" / "meta" / "Anim"
EXTRACTED = Path(__file__).resolve().parent / "extracted"

CORPUS = [
    ("warM_2HS_attk_weaponAttack", "warM_2HS_attk_weaponAttack.ani"),
    ("warM_1hsOh_attk_weaponAttack", "warM_1hsOh_attk_weaponAttack.ani"),
    ("warM_demonForm_nav_runStop", "warM_demonForm_nav_runStop.ani"),
    ("warM_2HM_attk_apocalypse", "warM_2HM_attk_apocalypse.ani"),
    ("warM_1HS_attk_SigilOfFlames", "warM_1HS_attk_SigilOfSummons.ani"),
    ("barM_HTH_nav_idle", "barM_HTH_nav_idle.ani"),
]


def _align2(x: int) -> int:
    return (x + 1) & ~1


def raw_quat_mags(blob: bytes) -> np.ndarray:
    """Decode the raw int16 xyzw quats in a rotation blob, return |q| array."""
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
    data_off = _align2(2 + (count - 1))
    need = data_off + count * 8
    if need > len(blob):
        return np.array([])
    q = (
        np.frombuffer(blob, dtype="<i2", count=count * 4, offset=data_off)
        .astype(np.float64)
        .reshape(count, 4)
        / 32767.0
    )
    return np.linalg.norm(q, axis=1)


def quat_mag_report(name: str, payload: Path, pidx: int) -> None:
    perm = parse_anim(META_DIR / f"{name}.ani.json", payload,
                      permutation_index=pidx)
    mags: list[float] = []
    for c in perm.header.rotation_curves:
        m = raw_quat_mags(c.raw_keys)
        mags.extend(m.tolist())
    if not mags:
        print(f"  {name} p{pidx} (comp={perm.compression}): no rotation keys")
        return
    a = np.array(mags)
    # comp=3 W-recovery: the LAST keyframe of a curve is sometimes stored
    # with W=0, so a small fraction of |q| land near sqrt(x^2+y^2+z^2)<1.
    near_unit = np.sum(np.abs(a - 1.0) < 0.02)
    print(
        f"  {name} p{pidx} (comp={perm.compression}): {len(a)} quats  "
        f"|q| min={a.min():.4f} med={np.median(a):.4f} max={a.max():.4f}  "
        f"near-unit(±0.02)={near_unit}/{len(a)} ({100*near_unit/len(a):.1f}%)"
    )


def load_rest_pose() -> dict[int, tuple]:
    skel_dir = EXTRACTED / "_skeleton" / "base"
    meta = skel_dir / "meta" / "Appearance" / "warM_base00.app"
    payload = skel_dir / "payload" / "Appearance" / "warM_base00.app"
    mesh = parse_app(meta, payload)
    skel = mesh.skeleton
    return {
        b.name_hash: (b.local_trs.q, b.local_trs.wp, b.local_trs.scale)
        for b in skel.bones
    }


def rest_pose_test(name: str, payload: Path, pidx: int, rest: dict) -> None:
    perm = parse_anim(META_DIR / f"{name}.ani.json", payload,
                      permutation_index=pidx)
    perm.compression = 3  # in-memory override — treat comp=5 bytes as comp=3
    decoded = decode_permutation(perm, rest_pose=rest)
    _results, summary = validate_against_rest_pose(decoded, rest, frame=0)
    print(f"  {name} p{pidx}:")
    for line in summary.splitlines():
        print(f"  {line}")


def cross_perm_constants() -> None:
    """warM_2HM_attk_apocalypse: comp=5 p0 vs comp=3 p2 constant curves."""
    name = "warM_2HM_attk_apocalypse"
    payload = EXTRACTED / name / "base" / "payload" / "Anim" / f"{name}.ani"
    p5 = parse_anim(META_DIR / f"{name}.ani.json", payload, permutation_index=0)
    p3 = parse_anim(META_DIR / f"{name}.ani.json", payload, permutation_index=2)
    print(f"  comp=5 p0 ({p5.keyframe_count}f) vs comp=3 p2 "
          f"({p3.keyframe_count}f), same skeleton:")

    for label, ca, cb in (
        ("translation", p5.header.translation_curves,
         p3.header.translation_curves),
        ("rotation", p5.header.rotation_curves, p3.header.rotation_curves),
    ):
        both_const = 0
        match = 0
        examples: list[str] = []
        for i, (a, b) in enumerate(zip(ca, cb)):
            ba, bb = a.raw_keys, b.raw_keys
            if len(ba) < 12 or len(bb) < 12:
                continue
            if label == "translation":
                if ba[0] != 1 or bb[0] != 1:
                    continue
                va = struct.unpack_from("<3f", ba, 4)
                vb = struct.unpack_from("<3f", bb, 4)
            else:
                if (int.from_bytes(ba[0:2], "little") != 1
                        or int.from_bytes(bb[0:2], "little") != 1):
                    continue
                va = struct.unpack_from("<4h", ba, 2)
                vb = struct.unpack_from("<4h", bb, 2)
            both_const += 1
            same = all(abs(x - y) < 1e-4 for x, y in zip(va, vb))
            if same:
                match += 1
            elif len(examples) < 3:
                examples.append(
                    f"      bone[{i}]: c5={tuple(round(x,4) for x in va)} "
                    f"c3={tuple(round(x,4) for x in vb)}"
                )
        print(f"    {label}: {match}/{both_const} count=1 curves "
              f"have identical baselines")
        for e in examples:
            print(e)


def main() -> None:
    print("=" * 78)
    print("TEST 1 — raw int16 quaternion magnitudes (xyzw / 32767)")
    print("=" * 78)
    for name, pf in CORPUS:
        payload = EXTRACTED / name / "base" / "payload" / "Anim" / pf
        meta = json.loads((META_DIR / f"{name}.ani.json").read_text("utf-8"))
        for pidx in range(len(meta.get("ptPermutations", []))):
            quat_mag_report(name, payload, pidx)

    print("\n" + "=" * 78)
    print("TEST 2 — rest-pose validation (comp=5 decoded AS comp=3, frame 0)")
    print("=" * 78)
    rest = load_rest_pose()
    print(f"  warM_base00 skeleton: {len(rest)} bones\n")
    for name, pf in CORPUS:
        payload = EXTRACTED / name / "base" / "payload" / "Anim" / pf
        meta = json.loads((META_DIR / f"{name}.ani.json").read_text("utf-8"))
        for pidx in range(len(meta.get("ptPermutations", []))):
            rest_pose_test(name, payload, pidx, rest)

    print("\n" + "=" * 78)
    print("TEST 3 — cross-permutation constant curves (known plaintext)")
    print("=" * 78)
    cross_perm_constants()


if __name__ == "__main__":
    main()
