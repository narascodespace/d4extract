"""Verify the flCompression=5 fix against the full Warlock comp=5 surface.

The Character Builder export that produced the 56 decode failures pulls
every animation for the warM (Warlock male) skeleton. This script is the
decode-layer equivalent of that export: it finds every ``warM_*``
animation in d4data with a comp=5 permutation, extracts the payloads via
``RustyDemonCLI.extract_anim_pair_batch`` (one CASC open), and runs the
production decoder over every permutation.

A clean run (all comp=5 permutations decode) is the empirical proof that
widening the decoder guard clears the Warlock export failures.
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
OUT = Path(__file__).resolve().parent / "warlock_verify"


def find_comp5_anims() -> list[str]:
    """Every warM_* animation with at least one comp=5 permutation."""
    names: list[str] = []
    for fp in glob.glob(str(META_DIR / "warM_*.ani.json")):
        name = os.path.basename(fp)[: -len(".ani.json")]
        try:
            meta = json.load(open(fp, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        comps = {
            int(p.get("flCompression", 0))
            for p in meta.get("ptPermutations", [])
            if isinstance(p, dict)
        }
        if 5 in comps:
            names.append(name)
    return sorted(names)


def main() -> None:
    names = find_comp5_anims()
    print(f"warM_* animations with a comp=5 permutation: {len(names)}")

    cli = RustyDemonCLI(GAME_DIR)
    extracted = cli.extract_anim_pair_batch(names, OUT, d4data_path=D4DATA)
    print(f"extracted payloads for {len(extracted)}/{len(names)} animations\n")

    perm_total = 0
    comp_counts: dict[int, int] = {}
    failures: list[tuple[str, int, int, str]] = []  # name, pidx, comp, err
    decoded_ok = 0
    missing: list[str] = []

    for name in names:
        entry = extracted.get(name)
        if entry is None:
            missing.append(name)
            continue
        _meta_bin, payload, _shared = entry
        meta_json = META_DIR / f"{name}.ani.json"
        nperm = len(
            json.load(open(meta_json, encoding="utf-8")).get(
                "ptPermutations", []
            )
        )
        for pidx in range(nperm):
            perm_total += 1
            try:
                perm = parse_anim(meta_json, payload, permutation_index=pidx)
                comp_counts[perm.compression] = (
                    comp_counts.get(perm.compression, 0) + 1
                )
                decode_permutation(perm)
                decoded_ok += 1
            except AnimFormatError as exc:
                comp = -1
                try:
                    comp = parse_anim(
                        meta_json, payload, permutation_index=pidx,
                    ).compression
                except Exception:  # noqa: BLE001
                    pass
                failures.append((name, pidx, comp, str(exc)))
            except Exception as exc:  # noqa: BLE001
                failures.append((name, pidx, -1, f"{type(exc).__name__}: {exc}"))

    print(f"permutations decoded:   {decoded_ok}/{perm_total}")
    print(f"permutations by mode:   {dict(sorted(comp_counts.items()))}")
    if missing:
        print(f"payloads not extracted: {len(missing)} -> {missing}")
    if failures:
        print(f"\nDECODE FAILURES ({len(failures)}):")
        for name, pidx, comp, err in failures:
            print(f"  {name} p{pidx} (comp={comp}): {err}")
    else:
        print("\nNO DECODE FAILURES — every warM comp=5 permutation decoded.")


if __name__ == "__main__":
    main()
