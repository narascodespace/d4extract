"""Decode-layer verification of the comp=1/2/4/6 + long-form-guard fix.

The Character Builder export decodes every animation discovered for a
class skeleton. This is the decode-layer equivalent for the four
affected classes: it finds every non-IGC ``<class>_*`` animation that is
either (a) comp 1/2/4/6 — newly supported, should now decode — or (b)
>255 frames — long-form, should now fail *cleanly* via the new guard
instead of silently mis-decoding.

Extracts via RustyDemonCLI.extract_anim_pair_batch and runs the
production decoder. Read-only w.r.t. the codebase.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from d4extract.casc.rustydemon import RustyDemonCLI  # noqa: E402
from d4extract.formats.anim_parser import (  # noqa: E402
    AnimFormatError,
    decode_permutation,
    parse_anim,
)

GAME_DIR = Path(r"C:\Program Files (x86)\Diablo IV")
D4DATA = Path(
    r"C:\Users\ryant\Documents\claude\Projects\diablo4analyzer\d4data\json"
)
META_DIR = D4DATA / "base" / "meta" / "Anim"
OUT = Path(__file__).resolve().parent / "class_verify"
CLASSES = ["barM", "warM", "spiM", "spiF"]


def affected_anims(prefix: str) -> tuple[list[str], list[str]]:
    """(comp1/2/4/6 anims, >255-frame anims) for a class — non-IGC only."""
    comp1246: set[str] = set()
    longform: set[str] = set()
    for fp in glob.glob(str(META_DIR / f"{prefix}_*.ani.json")):
        base = os.path.basename(fp)
        if base.lower().startswith("igc_"):
            continue
        name = base[: -len(".ani.json")]
        try:
            meta = json.load(open(fp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for p in meta.get("ptPermutations", []):
            if not isinstance(p, dict):
                continue
            if int(p.get("flCompression", 0)) in (1, 2, 4, 6):
                comp1246.add(name)
            if int(p.get("nKeyframeCount", 0)) > 255:
                longform.add(name)
    return sorted(comp1246), sorted(longform)


def classify(exc: AnimFormatError) -> str:
    s = str(exc)
    if "long-form" in s:
        return "long_form"
    if "unsupported flCompression" in s:
        return "unsupported_compression"
    return "other"


def main() -> None:
    cli = RustyDemonCLI(GAME_DIR)
    for prefix in CLASSES:
        comp1246, longform = affected_anims(prefix)
        to_extract = sorted(set(comp1246) | set(longform))
        print(f"\n{'=' * 70}")
        print(f"{prefix}: {len(comp1246)} comp1/2/4/6 anims, "
              f"{len(longform)} long-form (>255f) anims")
        print("=" * 70)
        if not to_extract:
            print("  (nothing affected)")
            continue
        extracted = cli.extract_anim_pair_batch(
            to_extract, OUT / prefix, d4data_path=D4DATA,
        )

        # comp=1/2/4/6 anims: every permutation should decode.
        c_ok = c_fail = 0
        c_failures: list[str] = []
        for name in comp1246:
            entry = extracted.get(name)
            if entry is None:
                c_failures.append(f"{name}: not extracted")
                continue
            _m, payload, _s = entry
            mj = META_DIR / f"{name}.ani.json"
            nperm = len(json.load(open(mj, encoding="utf-8"))
                        .get("ptPermutations", []))
            for pidx in range(nperm):
                try:
                    perm = parse_anim(mj, payload, permutation_index=pidx)
                    decode_permutation(perm)
                    c_ok += 1
                except AnimFormatError as exc:
                    c_fail += 1
                    c_failures.append(f"{name} p{pidx} [{classify(exc)}]: {exc}")

        # long-form anims: every >255-frame permutation should raise the
        # long-form error (clean failure, not silent garbage).
        lf_clean = lf_other = 0
        lf_notes: list[str] = []
        for name in longform:
            entry = extracted.get(name)
            if entry is None:
                lf_notes.append(f"{name}: not extracted")
                continue
            _m, payload, _s = entry
            mj = META_DIR / f"{name}.ani.json"
            perms = json.load(open(mj, encoding="utf-8")).get(
                "ptPermutations", [])
            for pidx, p in enumerate(perms):
                if int(p.get("nKeyframeCount", 0)) <= 255:
                    continue
                try:
                    perm = parse_anim(mj, payload, permutation_index=pidx)
                    decode_permutation(perm)
                    lf_other += 1
                    lf_notes.append(f"{name} p{pidx}: decoded (NO guard!)")
                except AnimFormatError as exc:
                    if classify(exc) == "long_form":
                        lf_clean += 1
                    else:
                        lf_other += 1
                        lf_notes.append(f"{name} p{pidx} [{classify(exc)}]")

        print(f"  comp=1/2/4/6 permutations: {c_ok} decoded OK, "
              f"{c_fail} failed")
        for f in c_failures:
            print(f"    FAIL: {f}")
        print(f"  long-form permutations: {lf_clean} raised the clean "
              f"long-form guard, {lf_other} other")
        for n in lf_notes[:8]:
            print(f"    note: {n}")


if __name__ == "__main__":
    main()
