"""Long-form (>255-frame) animation probe — reproduces report sections 6D & 8.

Two findings:
  * a d4data-wide scan of permutations with nKeyframeCount > 255, by
    compression mode (the long-form variant is mode-independent);
  * inspection of a long comp=3 animation (Amalgam_reac_Death, 271
    frames) showing it uses u16 timestamps — the same long-form layout
    as the comp=1 IGC clips.

Read-only.
"""

from __future__ import annotations

import glob
import json
import os
import struct
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from d4extract.casc.rustydemon import RustyDemonCLI  # noqa: E402
from d4extract.formats.anim_parser import parse_anim  # noqa: E402

GAME_DIR = Path(r"C:\Program Files (x86)\Diablo IV")
D4DATA = Path(
    r"C:\Users\ryant\Documents\claude\Projects\diablo4analyzer\d4data\json"
)
META_DIR = D4DATA / "base" / "meta" / "Anim"
OUT = Path(__file__).resolve().parent / "extracted_long"


def scan() -> None:
    print("=" * 72)
    print("LONG-FORM PREVALENCE — permutations with nKeyframeCount > 255")
    print("=" * 72)
    by_mode: dict[int, int] = {}
    maxf: dict[int, int] = {}
    total = igc = 0
    for fp in glob.glob(str(META_DIR / "*.ani.json")):
        try:
            m = json.load(open(fp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        name = os.path.basename(fp)[:-9]
        for p in m.get("ptPermutations", []):
            if not isinstance(p, dict):
                continue
            c = int(p.get("flCompression", 0))
            f = int(p.get("nKeyframeCount", 0))
            maxf[c] = max(maxf.get(c, 0), f)
            if f > 255:
                total += 1
                by_mode[c] = by_mode.get(c, 0) + 1
                if name.lower().startswith("igc_"):
                    igc += 1
    print(f"  >255-frame permutations: {total} "
          f"(IGC-prefixed {igc}, non-IGC {total - igc})")
    print(f"  by compression mode: {dict(sorted(by_mode.items()))}")
    print(f"  max nKeyframeCount per mode: {dict(sorted(maxf.items()))}")


def inspect_long_comp3() -> None:
    print("\n" + "=" * 72)
    print("LONG comp=3 ANIMATION — Amalgam_reac_Death (271 frames)")
    print("=" * 72)
    cli = RustyDemonCLI(GAME_DIR)
    res = cli.extract_anim_pair_batch(
        ["Amalgam_reac_Death"], OUT, d4data_path=D4DATA,
    )
    _meta, payload, _ = res["Amalgam_reac_Death"]
    perm = parse_anim(
        META_DIR / "Amalgam_reac_Death.ani.json", payload,
        permutation_index=0,
    )
    print(f"  comp={perm.compression} bones={perm.bone_count} "
          f"frames={perm.keyframe_count}")
    print("  rotation curves — size vs u8/u16 timestamp predictions:")
    shown = 0
    for i, c in enumerate(perm.header.rotation_curves):
        if c.keys_size < 300:
            continue
        count = struct.unpack_from("<H", c.raw_keys, 0)[0]
        u8 = ((2 + count - 1 + 1) & ~1) + count * 8 + 2
        u16 = 2 + count * 2 + count * 8 + 2
        verdict = "u16xcount MATCH" if u16 == c.keys_size else "?"
        print(f"    bone[{i}]: size={c.keys_size:>5} count={count:>4}  "
              f"u8-predict={u8:>5}  u16-predict={u16:>5}  -> {verdict}")
        if shown == 0:
            head = " ".join(f"{b:02x}" for b in c.raw_keys[:20])
            print(f"      head: {head}")
            print(f"      = count(u16)={count}, then u16 timestamps "
                  f"0,1,2,3,...")
        shown += 1
        if shown >= 6:
            break


def main() -> None:
    scan()
    inspect_long_comp3()


if __name__ == "__main__":
    main()
