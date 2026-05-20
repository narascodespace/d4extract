"""Value test: decode flCompression=1/2/4/6 curves with the comp=3 decoder.

Cross-mode blob sharing (see analyze.py) already proves comp=1/2/6 share
the comp=3 curve layout. This script confirms the *values* decode sanely:

  1. decode every comp=1/2/4/6 permutation through the production
     decoder (compression overridden to 3 in memory) — count per-curve
     fallbacks and check output quaternion unit-length.
  2. cross-permutation constants — spiF_gla_nav_evade carries comp=1 and
     comp=3 permutations; count=1 curve baselines must match.
  3. rest-pose known-plaintext — decode each comp=1 player anim and
     compare frame 0 against the matching skeleton rest pose.

Read-only: sets ``perm.compression = 3`` on parsed-in-memory objects to
bypass the decoder guard. It never touches src/.
"""

from __future__ import annotations

import io
import json
import logging
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
ROOT = Path(__file__).resolve().parent
PAYLOAD_DIR = ROOT / "extracted" / "base" / "payload" / "Anim"

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
OTHER = [
    "IGC_CBH_t3barbarianm_1010",
    "druM_2HS_attk_StoneBurst",
    "palM_1hsShd_attk_impale",
    "druM_HTH_trav_ladder_climb_down",
    "bandit_sword_nav_run",
    "bandit_sword_nav_walk",
    "treasuregoblin_nav_idle_unalert_outro_still",
    "warlock_tailStrike_attk_basic",
]

# skeleton .app pairs (meta, payload) per filename prefix
_SKEL5 = ROOT.parent / "flcompression5" / "extracted" / "_skeleton" / "base"
_SKEL1 = ROOT / "extracted" / "_skeleton"
SKELETONS = {
    "warM": (_SKEL5 / "meta" / "Appearance" / "warM_base00.app",
             _SKEL5 / "payload" / "Appearance" / "warM_base00.app"),
    "spiF": (_SKEL1 / "spiF_base00" / "base" / "meta" / "Appearance" / "spiF_base00.app",
             _SKEL1 / "spiF_base00" / "base" / "payload" / "Appearance" / "spiF_base00.app"),
    "barM": (_SKEL1 / "barM_base00" / "base" / "meta" / "Appearance" / "barM_base00.app",
             _SKEL1 / "barM_base00" / "base" / "payload" / "Appearance" / "barM_base00.app"),
}


def _payload(name: str) -> Path:
    return PAYLOAD_DIR / f"{name}.ani"


def _meta(name: str) -> Path:
    return META_DIR / f"{name}.ani.json"


def _nperm(name: str) -> int:
    return len(json.loads(_meta(name).read_text("utf-8")).get(
        "ptPermutations", []))


def _rest_pose(prefix: str) -> dict | None:
    pair = SKELETONS.get(prefix)
    if pair is None or not pair[0].is_file():
        return None
    mesh = parse_app(pair[0], pair[1])
    return {b.name_hash: (b.local_trs.q, b.local_trs.wp, b.local_trs.scale)
            for b in mesh.skeleton.bones}


def main() -> None:
    # capture decoder fallback warnings
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.WARNING)
    log = logging.getLogger("d4extract.formats.anim_parser")
    log.addHandler(handler)
    log.setLevel(logging.WARNING)

    print("=" * 80)
    print("TEST 1 — decode every permutation through the comp=3 decoder")
    print("=" * 80)
    for name in COMP1_PLAYER + OTHER:
        pp = _payload(name)
        if not pp.is_file():
            continue
        for pidx in range(_nperm(name)):
            buf.truncate(0)
            buf.seek(0)
            perm = parse_anim(_meta(name), pp, permutation_index=pidx)
            orig = perm.compression
            perm.compression = 3
            try:
                decoded = decode_permutation(perm)
            except Exception as exc:  # noqa: BLE001
                print(f"  {name} p{pidx} (c{orig}): DECODE RAISED — {exc}")
                continue
            # output quaternion unit-length
            bad = 0
            total = 0
            for b in decoded.bone_animations:
                for q in b.rotations:
                    total += 1
                    ln = float((q[0]**2 + q[1]**2 + q[2]**2 + q[3]**2) ** 0.5)
                    if abs(ln - 1.0) >= 0.02:
                        bad += 1
            warns = buf.getvalue().count("fell back")
            flag = "" if (bad == 0 and warns == 0) else "   <-- ANOMALY"
            print(f"  {name} p{pidx} (c{orig}): "
                  f"non-unit quats {bad}/{total}, "
                  f"fallback warnings={warns}{flag}")

    print("\n" + "=" * 80)
    print("TEST 2 — cross-permutation constants (spiF_gla_nav_evade)")
    print("=" * 80)
    name = "spiF_gla_nav_evade"
    p1 = parse_anim(_meta(name), _payload(name), permutation_index=0)  # c1
    p3 = parse_anim(_meta(name), _payload(name), permutation_index=1)  # c3
    for lbl, ca, cb in (
        ("translation", p1.header.translation_curves,
         p3.header.translation_curves),
        ("rotation", p1.header.rotation_curves, p3.header.rotation_curves)):
        both = match = 0
        for a, b in zip(ca, cb):
            ba, bb = a.raw_keys, b.raw_keys
            if len(ba) < 12 or len(bb) < 12:
                continue
            if lbl == "translation":
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
            both += 1
            if all(abs(x - y) < 1e-4 for x, y in zip(va, vb)):
                match += 1
        print(f"  {lbl}: {match}/{both} count=1 curves have identical "
              f"baselines (comp=1 p0 vs comp=3 p1)")

    print("\n" + "=" * 80)
    print("TEST 3 — rest-pose validation (comp=1 player anims, frame 0)")
    print("=" * 80)
    for name in COMP1_PLAYER:
        prefix = name[:4]
        rest = _rest_pose(prefix)
        if rest is None:
            print(f"  {name}: no skeleton for prefix {prefix!r}")
            continue
        perm = parse_anim(_meta(name), _payload(name), permutation_index=0)
        perm.compression = 3
        decoded = decode_permutation(perm, rest_pose=rest)
        _r, summary = validate_against_rest_pose(decoded, rest, frame=0)
        line = [ln for ln in summary.splitlines() if "match-rest" in ln]
        print(f"  {name} p0:")
        for ln in line:
            print(f"   {ln}")


if __name__ == "__main__":
    main()
