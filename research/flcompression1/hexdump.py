"""Annotated hex dumps for the flCompression=1 research report.

Dumps:
  A. spiF_gla_nav_evade — a curve blob whose descriptor is referenced by
     BOTH the comp=1 permutation and a comp=3 permutation (same offset).
  B. spiF_gla_attk_centipede_core (comp=1) vs barM_HTH_nav_idle (comp=3)
     — same bone position, side-by-side structural comparison.
  C. IGC_CBH_t3barbarianm_1010 (comp=1, 2601 frames) — characterising
     why the long-form IGC clip differs from short comp=1 player anims.

Also resolves the three dismount anims against CoreTOCSharedPayloadsMapping.

Read-only.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from d4extract.casc.payload_resolver import resolve_payload_path  # noqa: E402
from d4extract.formats.anim_parser import parse_anim  # noqa: E402

D4DATA = Path(
    r"C:\Users\ryant\Documents\claude\Projects\diablo4analyzer\d4data\json"
)
META_DIR = D4DATA / "base" / "meta" / "Anim"
PAYLOAD_DIR = Path(__file__).resolve().parent / "extracted" / "base" / "payload" / "Anim"
COMP3_REF = (
    Path(__file__).resolve().parents[1] / "flcompression5" / "extracted"
    / "barM_HTH_nav_idle" / "base" / "payload" / "Anim" / "barM_HTH_nav_idle.ani"
)


def hexdump(blob: bytes, base: int, limit: int = 160) -> str:
    out = []
    shown = blob[:limit]
    for off in range(0, len(shown), 16):
        row = shown[off:off + 16]
        hexs = " ".join(f"{b:02x}" for b in row)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.append(f"  {base + off:08x}  {hexs:<47}  |{ascii_}|")
    if len(blob) > limit:
        out.append(f"  ... (+{len(blob) - limit} more bytes)")
    return "\n".join(out)


def _p(name: str) -> Path:
    return PAYLOAD_DIR / f"{name}.ani"


def _m(name: str) -> Path:
    return META_DIR / f"{name}.ani.json"


def section_a() -> None:
    print("=" * 78)
    print("A. spiF_gla_nav_evade — a curve blob ALIASED across the comp boundary")
    print("=" * 78)
    name = "spiF_gla_nav_evade"
    p0 = parse_anim(_m(name), _p(name), permutation_index=0)  # comp=1
    p1 = parse_anim(_m(name), _p(name), permutation_index=1)  # comp=3
    # find a non-empty translation curve with identical (offset,size)
    for i, (a, b) in enumerate(zip(p0.header.translation_curves,
                                   p1.header.translation_curves)):
        if a.keys_size and a.keys_offset == b.keys_offset and a.keys_size == b.keys_size:
            print(f"\nbone[{i}] hash=0x{p0.header.bone_names[i]:08x} "
                  f"translation curve:")
            print(f"  comp=1 p0 descriptor: offset={a.keys_offset} size={a.keys_size}")
            print(f"  comp=3 p1 descriptor: offset={b.keys_offset} size={b.keys_size}"
                  f"   <-- SAME physical bytes")
            print(hexdump(a.raw_keys, a.keys_offset))
            break
    # and an aliased rotation curve
    for i, (a, b) in enumerate(zip(p0.header.rotation_curves,
                                   p1.header.rotation_curves)):
        if (a.keys_size and a.keys_size > 12
                and a.keys_offset == b.keys_offset and a.keys_size == b.keys_size):
            print(f"\nbone[{i}] hash=0x{p0.header.bone_names[i]:08x} "
                  f"rotation curve (comp=1 p0 and comp=3 p1 both point at "
                  f"offset {a.keys_offset}, size {a.keys_size}):")
            print(hexdump(a.raw_keys, a.keys_offset))
            break


def _annotate_rot(blob: bytes) -> str:
    count = struct.unpack_from("<H", blob, 0)[0]
    return f"count(u16)={count}  size={len(blob)}"


def section_b() -> None:
    print("\n" + "=" * 78)
    print("B. comp=1 vs comp=3 — same structure, different files")
    print("=" * 78)
    c1 = parse_anim(_m("spiF_gla_attk_centipede_core"),
                    _p("spiF_gla_attk_centipede_core"), permutation_index=0)
    c3 = parse_anim(META_DIR / "barM_HTH_nav_idle.ani.json", COMP3_REF,
                    permutation_index=0)
    for label, perm in (("comp=1 spiF_gla_attk_centipede_core p0", c1),
                        ("comp=3 barM_HTH_nav_idle p0", c3)):
        # pick the first animated (count>=2) rotation curve
        for i, c in enumerate(perm.header.rotation_curves):
            if c.keys_size > 40:
                print(f"\n{label} — bone[{i}] rotation curve "
                      f"({c.keys_size}B): {_annotate_rot(c.raw_keys)}")
                print(hexdump(c.raw_keys, c.keys_offset, limit=96))
                break


def section_c() -> None:
    print("\n" + "=" * 78)
    print("C. IGC_CBH_t3barbarianm_1010 (comp=1, 2601 frames) — the anomaly")
    print("=" * 78)
    igc = parse_anim(_m("IGC_CBH_t3barbarianm_1010"),
                     _p("IGC_CBH_t3barbarianm_1010"), permutation_index=0)
    print(f"frames={igc.keyframe_count}  bones={igc.bone_count}")
    # show a few rotation curves' count fields and sizes
    print("\nfirst 8 non-empty rotation curves (count read as u16, size):")
    shown = 0
    for i, c in enumerate(igc.header.rotation_curves):
        if c.keys_size == 0:
            continue
        count = struct.unpack_from("<H", c.raw_keys, 0)[0]
        # what comp=3 u8-timestamp layout would predict for this count:
        predict = ((2 + count - 1 + 1) & ~1) + count * 8 + 2
        print(f"  bone[{i}]: size={c.keys_size:>6}  count(u16)={count:>5}  "
              f"comp3-u8-predict={predict:>6}  "
              f"{'MATCH' if predict == c.keys_size else 'mismatch'}")
        shown += 1
        if shown >= 8:
            break
    # dump one
    for i, c in enumerate(igc.header.rotation_curves):
        if c.keys_size > 200:
            print(f"\nbone[{i}] rotation curve head ({c.keys_size}B):")
            print(hexdump(c.raw_keys, c.keys_offset, limit=96))
            break


def section_shared() -> None:
    print("\n" + "=" * 78)
    print("SHARED-PAYLOAD CHECK — do the three dismount anims alias?")
    print("=" * 78)
    for name in ("barM_mount_hth_event_dismount_damage",
                 "warM_mount_horse_reac_dismount",
                 "spiF_mount_hth_event_dismount_damage"):
        try:
            resolved = resolve_payload_path(name, D4DATA, sno_group="Anim")
        except Exception as exc:  # noqa: BLE001
            resolved = f"<lookup error: {exc}>"
        print(f"  {name}: CoreTOCSharedPayloadsMapping -> {resolved}")
    print("  (None = own payload, not aliased to another anim)")


def main() -> None:
    section_a()
    section_b()
    section_c()
    section_shared()


if __name__ == "__main__":
    main()
