#!/usr/bin/env python
"""Diagnostic: inspect Amazon_Prop_Barrel_02_Dyn skeleton and animations.

Run AFTER extract_barrel.py:
    python scripts/barrel_diagnostic.py

Prints:
  1. Rest-pose rotations for all 3 bones (identity vs non-identity)
  2. Animation curve data for Neutral + Death animations
  3. Comparison of frame-0 anim values vs rest-pose values
     (tells us whether rotations are absolute or additive deltas)
"""

from pathlib import Path
import math
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from d4extract.formats.app_parser import parse_app
from d4extract.formats.anim_parser import (
    parse_anim,
    decode_permutation,
    validate_against_rest_pose,
)

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
D4DATA = Path(__file__).resolve().parent.parent.parent / "d4data" / "json"

MODEL = "Amazon_Prop_Barrel_02_Dyn"
ANIMS = [
    "Amazon_Prop_Barrel_02_Dyn_Neutral",
    "Amazon_Prop_Barrel_02_Dyn_Death",
]

IDENTITY_QUAT = (0.0, 0.0, 0.0, 1.0)


def quat_angle_deg(a, b):
    """Angle in degrees between two unit quaternions."""
    dot = abs(a[0]*b[0] + a[1]*b[1] + a[2]*b[2] + a[3]*b[3])
    dot = min(dot, 1.0)
    return math.degrees(2.0 * math.acos(dot))


def vec3_dist(a, b):
    return math.sqrt(sum((ai - bi)**2 for ai, bi in zip(a, b)))


def main():
    # ── Parse model ──────────────────────────────────────────────────
    meta = SAMPLES / f"base/meta/Appearance/{MODEL}.app"
    payload = SAMPLES / f"base/payload/Appearance/{MODEL}.app"

    if not meta.exists() or not payload.exists():
        print(f"ERROR: Barrel .app files not found at {meta.parent}")
        print("Run extract_barrel.py first!")
        return

    mesh = parse_app(meta, payload)
    skel = mesh.skeleton
    if not skel:
        print("ERROR: No skeleton found in barrel model")
        return

    print(f"═══ {MODEL} Skeleton ({len(skel.bones)} bones) ═══")
    print()

    non_identity_count = 0
    for bone in skel.bones:
        q = bone.local_trs.q
        t = bone.local_trs.wp
        s = bone.local_trs.scale
        angle = quat_angle_deg(q, IDENTITY_QUAT)
        is_identity = angle < 0.1
        marker = "" if is_identity else "  ← NON-IDENTITY"
        if not is_identity:
            non_identity_count += 1

        print(f"  bone[{bone.index}] hash=0x{bone.name_hash:08X} parent={bone.parent_index}")
        print(f"    rest_q = ({q[0]:.6f}, {q[1]:.6f}, {q[2]:.6f}, {q[3]:.6f})"
              f"  angle_from_identity={angle:.2f}°{marker}")
        print(f"    rest_t = ({t[0]:.6f}, {t[1]:.6f}, {t[2]:.6f})")
        print(f"    rest_s = ({s[0]:.6f}, {s[1]:.6f}, {s[2]:.6f})")
        print()

    if non_identity_count == 0:
        print("  ⚠ ALL bones have identity rest rotations!")
        print("  This model won't distinguish absolute vs additive rotation.")
    else:
        print(f"  ✓ {non_identity_count} bone(s) have non-identity rest rotations")
        print("  This model CAN distinguish absolute vs additive rotation.")
    print()

    # ── Build rest-pose map ──────────────────────────────────────────
    rest_pose = {
        b.name_hash: (b.local_trs.q, b.local_trs.wp, b.local_trs.scale)
        for b in skel.bones
    }

    # ── Parse animations ─────────────────────────────────────────────
    for anim_name in ANIMS:
        anim_meta = D4DATA / f"base/meta/Anim/{anim_name}.ani.json"
        anim_payload = SAMPLES / f"base/payload/Anim/{anim_name}.ani"

        if not anim_meta.exists():
            print(f"  SKIP {anim_name}: meta not found at {anim_meta}")
            continue
        if not anim_payload.exists():
            print(f"  SKIP {anim_name}: payload not found at {anim_payload}")
            continue

        perm = parse_anim(anim_meta, anim_payload)
        decoded = decode_permutation(perm, rest_pose=rest_pose)

        print(f"═══ Animation: {anim_name} ═══")
        print(f"  frames={decoded.frame_count}, fps={decoded.frame_rate}, "
              f"comp={decoded.compression}")
        print(f"  bone_animations={len(decoded.bone_animations)}")
        print()

        for ba in decoded.bone_animations:
            rest = rest_pose.get(ba.bone_hash)
            if rest is None:
                print(f"  bone 0x{ba.bone_hash:08X}: NOT IN SKELETON")
                continue

            rest_q, rest_t, rest_s = rest
            # Frame 0 values
            anim_q0 = ba.rotations[0] if ba.rotations else None
            anim_t0 = ba.translations[0] if ba.translations else None

            print(f"  bone 0x{ba.bone_hash:08X}:")
            if anim_t0 is not None:
                t_dist = vec3_dist(anim_t0, (0, 0, 0))
                t_from_rest = vec3_dist(anim_t0, rest_t)
                print(f"    frame0 t = ({anim_t0[0]:.6f}, {anim_t0[1]:.6f}, {anim_t0[2]:.6f})")
                print(f"    rest   t = ({rest_t[0]:.6f}, {rest_t[1]:.6f}, {rest_t[2]:.6f})")
                print(f"    |anim_t - (0,0,0)|  = {t_dist:.6f}  (if delta, this is the offset)")
                print(f"    |anim_t - rest_t|   = {t_from_rest:.6f}  (if absolute, this is zero for rest)")
                if t_dist < 1e-3:
                    print(f"    → anim_t ≈ zero → CONSISTENT WITH DELTA (rest+0=rest)")
                elif t_from_rest < 1e-3:
                    print(f"    → anim_t ≈ rest_t → CONSISTENT WITH ABSOLUTE")
                else:
                    print(f"    → neither zero nor rest — could be actual motion")

            if anim_q0 is not None:
                angle_from_identity = quat_angle_deg(anim_q0, IDENTITY_QUAT)
                angle_from_rest = quat_angle_deg(anim_q0, rest_q)
                print(f"    frame0 q = ({anim_q0[0]:.6f}, {anim_q0[1]:.6f}, {anim_q0[2]:.6f}, {anim_q0[3]:.6f})")
                print(f"    rest   q = ({rest_q[0]:.6f}, {rest_q[1]:.6f}, {rest_q[2]:.6f}, {rest_q[3]:.6f})")
                print(f"    angle(anim_q, identity) = {angle_from_identity:.4f}°  (if delta, Neutral frame0 ≈ 0)")
                print(f"    angle(anim_q, rest_q)   = {angle_from_rest:.4f}°  (if absolute, Neutral frame0 ≈ 0)")
                if angle_from_identity < 1.0:
                    print(f"    → anim_q ≈ identity → ROTATION IS DELTA")
                elif angle_from_rest < 1.0:
                    print(f"    → anim_q ≈ rest_q → ROTATION IS ABSOLUTE")
                else:
                    print(f"    → neither identity nor rest — actual rotation")
            print()

        # Also run the validator
        results, summary = validate_against_rest_pose(decoded, rest_pose, frame=0)
        print(f"  Validation (frame 0 vs rest):")
        print(f"  {summary}")
        print()


if __name__ == "__main__":
    main()
