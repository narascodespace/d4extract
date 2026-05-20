"""Structural probe: does flCompression=5 reuse the comp=0/3 curve layout?

For every curve blob in the corpus, this checks the raw bytes against
the size equations the comp=0/3 decoders rely on:

  translation/scale count>=2:  N == align4(hdr+count) + count*12
  rotation        count>=2:  N == align2(2 + count-1)  + count*8

If comp=5 blobs satisfy the SAME equations as comp=3 blobs, comp=5 is
structurally the comp=3 format and the decoder only needs its
``compression in (0, 3)`` guards widened. If they don't, comp=5 is a
different encoding and the mismatch pattern tells us how.

Read-only. Imports the production parser, changes nothing.
"""

from __future__ import annotations

import json
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
    ("barM_HTH_nav_idle", "barM_HTH_nav_idle.ani"),
]


def _align4(x: int) -> int:
    return (x + 3) & ~3


def _align2(x: int) -> int:
    return (x + 1) & ~1


def check_translation(blob: bytes) -> tuple[str, str]:
    """Return (verdict, detail) for a translation/scale-style curve."""
    if not blob:
        return "empty", ""
    n = len(blob)
    count = blob[0]
    fmt = blob[1]
    if count == 0:
        return ("OK" if n == 16 else "size?", f"count=0 n={n}")
    if count == 1:
        return ("OK" if n == 16 else "size?", f"count=1 n={n} fmt=0x{fmt:02x}")
    # count >= 2 : translation header is 16 bytes (2 + u16pad + 12 baseline)
    expect = _align4(16 + count) + count * 12
    verdict = "OK" if expect == n else "MISMATCH"
    return verdict, f"count={count} fmt=0x{fmt:02x} n={n} expect_t={expect}"


def check_scale(blob: bytes) -> tuple[str, str]:
    """comp=3 scale: slim 2-byte header, no 12-byte baseline."""
    if not blob:
        return "empty", ""
    n = len(blob)
    count = blob[0]
    fmt = blob[1]
    if count == 0:
        return ("OK" if n == 16 else "size?", f"count=0 n={n}")
    if count == 1:
        return ("OK" if n == 16 else "size?", f"count=1 n={n}")
    expect = _align4(2 + count) + count * 12
    verdict = "OK" if expect == n else "MISMATCH"
    return verdict, f"count={count} fmt=0x{fmt:02x} n={n} expect_s={expect}"


def check_rotation(blob: bytes) -> tuple[str, str]:
    if not blob:
        return "empty", ""
    n = len(blob)
    count = int.from_bytes(blob[0:2], "little")
    if count == 0:
        return ("OK" if n in (4, 12) else "size?", f"count=0 n={n}")
    if count == 1:
        return ("OK" if n == 12 else "size?", f"count=1 n={n}")
    ts_count = count - 1
    expect = _align2(2 + ts_count) + count * 8
    verdict = "OK" if expect == n else "MISMATCH"
    return verdict, f"count={count} n={n} expect_r={expect}"


def probe_perm(name: str, payload_path: Path, pidx: int) -> None:
    perm = parse_anim(
        META_DIR / f"{name}.ani.json", payload_path, permutation_index=pidx,
    )
    h = perm.header
    print(
        f"\n--- {name} p{pidx}  comp={perm.compression} "
        f"bones={perm.bone_count} frames={perm.keyframe_count} ---"
    )
    for label, curves, checker in (
        ("T", h.translation_curves, check_translation),
        ("R", h.rotation_curves, check_rotation),
        ("S", h.scale_curves, check_scale),
    ):
        tally: dict[str, int] = {}
        first_mismatch: list[str] = []
        sizes: dict[int, int] = {}
        for i, c in enumerate(curves):
            verdict, detail = checker(c.raw_keys)
            tally[verdict] = tally.get(verdict, 0) + 1
            sizes[c.keys_size] = sizes.get(c.keys_size, 0) + 1
            if verdict == "MISMATCH" and len(first_mismatch) < 4:
                first_mismatch.append(f"      bone[{i}]: {detail}")
        tally_s = " ".join(f"{k}={v}" for k, v in sorted(tally.items()))
        # most common sizes
        top_sizes = sorted(sizes.items(), key=lambda kv: -kv[1])[:6]
        sizes_s = " ".join(f"{sz}B×{ct}" for sz, ct in top_sizes)
        print(f"  {label}: {tally_s}")
        print(f"     sizes: {sizes_s}")
        for line in first_mismatch:
            print(line)


def main() -> None:
    print("=" * 78)
    print("flCompression=5 STRUCTURAL PROBE — comp=0/3 size-equation test")
    print("=" * 78)
    for name, payload_file in CORPUS:
        pp = EXTRACTED / name / "base" / "payload" / "Anim" / payload_file
        if not pp.is_file():
            print(f"!! missing {pp}")
            continue
        meta = json.loads((META_DIR / f"{name}.ani.json").read_text("utf-8"))
        for pidx in range(len(meta.get("ptPermutations", []))):
            probe_perm(name, pp, pidx)


if __name__ == "__main__":
    main()
