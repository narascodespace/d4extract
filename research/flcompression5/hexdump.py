"""Annotated hex dumps + fmt-byte audit for the flCompression=5 report.

Dumps the same bone indices from warM_2HM_attk_apocalypse permutation 0
(comp=5) and permutation 2 (comp=3) — same animation, same skeleton,
one payload file — so the byte layouts can be compared directly.

Also audits the translation/scale ``fmt`` byte across every comp=5
curve: the comp=0/3 decoders hard-require fmt==0x04.

Read-only.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from d4extract.formats.anim_parser import parse_anim  # noqa: E402

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
]


def hexdump(blob: bytes, base_off: int) -> str:
    out = []
    for off in range(0, len(blob), 16):
        row = blob[off:off + 16]
        hexs = " ".join(f"{b:02x}" for b in row)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.append(f"  {base_off + off:08x}  {hexs:<47}  |{ascii_}|")
    return "\n".join(out)


def annotate_translation(blob: bytes) -> str:
    if len(blob) < 16:
        return f"  (too small: {len(blob)}B)"
    count, fmt = blob[0], blob[1]
    pad = struct.unpack_from("<H", blob, 2)[0]
    base = struct.unpack_from("<3f", blob, 4)
    lines = [
        f"  count={count}  fmt=0x{fmt:02x}  pad=0x{pad:04x}",
        f"  baseline xyz = {tuple(round(x, 5) for x in base)}",
    ]
    if count >= 2:
        ts = list(blob[16:16 + count])
        lines.append(f"  timestamps[{count}] = {ts}")
    return "\n".join(lines)


def annotate_rotation(blob: bytes) -> str:
    if len(blob) < 4:
        return f"  (too small: {len(blob)}B)"
    count = struct.unpack_from("<H", blob, 0)[0]
    lines = [f"  count={count}"]
    if count == 1:
        q = struct.unpack_from("<4h", blob, 2)
        lines.append(f"  quat i16 xyzw = {q}  -> {tuple(round(x/32767,4) for x in q)}")
    elif count >= 2:
        ts = list(blob[2:2 + count - 1])
        lines.append(f"  timestamps[{count - 1}] = {ts}")
    return "\n".join(lines)


def dump_bone(name: str, payload: Path, pidx: int, bone_idx: int) -> None:
    perm = parse_anim(META_DIR / f"{name}.ani.json", payload,
                      permutation_index=pidx)
    h = perm.header
    bh = h.bone_names[bone_idx]
    print(f"\n{'-' * 74}")
    print(f"{name} p{pidx} (comp={perm.compression}, {perm.keyframe_count}f) "
          f"bone[{bone_idx}] hash=0x{bh:08x}")
    print(f"{'-' * 74}")
    for label, curve, annot in (
        ("TRANSLATION", h.translation_curves[bone_idx], annotate_translation),
        ("ROTATION", h.rotation_curves[bone_idx], annotate_rotation),
        ("SCALE", h.scale_curves[bone_idx], annotate_translation),
    ):
        print(f"\n {label}  ({curve.keys_size}B @ payload offset {curve.keys_offset})")
        if curve.is_empty:
            print("  (empty curve)")
            continue
        print(annot(curve.raw_keys))
        print(hexdump(curve.raw_keys, curve.keys_offset))


def fmt_audit() -> None:
    print("\n" + "=" * 74)
    print("FMT-BYTE AUDIT — translation/scale curves across all comp=5 perms")
    print("=" * 74)
    bad = 0
    total = 0
    for name, pf in CORPUS:
        payload = EXTRACTED / name / "base" / "payload" / "Anim" / pf
        meta = json.loads((META_DIR / f"{name}.ani.json").read_text("utf-8"))
        for pidx, p in enumerate(meta["ptPermutations"]):
            if int(p.get("flCompression", 0)) != 5:
                continue
            perm = parse_anim(META_DIR / f"{name}.ani.json", payload,
                              permutation_index=pidx)
            for label, curves in (("T", perm.header.translation_curves),
                                  ("S", perm.header.scale_curves)):
                for i, c in enumerate(curves):
                    if c.is_empty or len(c.raw_keys) < 2:
                        continue
                    cnt = c.raw_keys[0]
                    if cnt == 0:
                        continue
                    total += 1
                    fmt = c.raw_keys[1]
                    if fmt != 0x04:
                        bad += 1
                        if bad <= 8:
                            print(f"  NON-0x04: {name} p{pidx} {label}[{i}] "
                                  f"count={cnt} fmt=0x{fmt:02x}")
    print(f"  {total - bad}/{total} non-empty comp=5 T/S curves have fmt==0x04")


def main() -> None:
    apoc = EXTRACTED / "warM_2HM_attk_apocalypse" / "base" / "payload" / "Anim"
    apoc_payload = apoc / "warM_2HM_attk_apocalypse.ani"
    print("=" * 74)
    print("HEX DUMPS — warM_2HM_attk_apocalypse: comp=5 p0 vs comp=3 p2")
    print("(same animation, same 190-bone warM_base00 skeleton, one payload)")
    print("=" * 74)
    # bone 6 and 8 are animated (count>=2) in both permutations.
    for bone_idx in (6, 8):
        dump_bone("warM_2HM_attk_apocalypse", apoc_payload, 0, bone_idx)
        dump_bone("warM_2HM_attk_apocalypse", apoc_payload, 2, bone_idx)

    fmt_audit()


if __name__ == "__main__":
    main()
