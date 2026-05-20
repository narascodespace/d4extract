"""Analyse flCompression=5 .ani payloads against the comp=3 baseline.

Reads the corpus extracted by ``extract.py`` plus the d4data meta
JSONs, parses every permutation with the existing (unmodified)
``anim_parser``, and prints:

  * AnimPayloadData header confirmation (13 arrays, offsets, sizes)
  * per-curve byte totals + bytes/bone/frame
  * annotated hex dumps of individual bones' curve blobs

Read-only: it imports the production parser but changes nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

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
EXTRACTED = Path(__file__).resolve().parent / "extracted"


# (anim name, payload filename inside extracted/<name>/.../Anim/)
CORPUS = [
    ("warM_2HS_attk_weaponAttack", "warM_2HS_attk_weaponAttack.ani"),
    ("warM_1hsOh_attk_weaponAttack", "warM_1hsOh_attk_weaponAttack.ani"),
    ("warM_demonForm_nav_runStop", "warM_demonForm_nav_runStop.ani"),
    ("warM_2HM_attk_apocalypse", "warM_2HM_attk_apocalypse.ani"),
    ("warM_1HS_attk_SigilOfFlames", "warM_1HS_attk_SigilOfSummons.ani"),
    ("barM_HTH_nav_idle", "barM_HTH_nav_idle.ani"),
]


def _payload_path(name: str, payload_file: str) -> Path:
    return EXTRACTED / name / "base" / "payload" / "Anim" / payload_file


def _meta_path(name: str) -> Path:
    return META_DIR / f"{name}.ani.json"


def hexdump(blob: bytes, base: int = 0, limit: int = 256) -> str:
    """xxd-style hex+ASCII dump."""
    if not blob:
        return "    (empty)"
    out = []
    shown = blob[:limit]
    for off in range(0, len(shown), 16):
        row = shown[off:off + 16]
        hexs = " ".join(f"{b:02x}" for b in row)
        hexs = f"{hexs:<47}"
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.append(f"    {base + off:08x}  {hexs}  |{ascii_}|")
    if len(blob) > limit:
        out.append(f"    … (+{len(blob) - limit} more bytes)")
    return "\n".join(out)


def analyze_permutation(name: str, payload_path: Path, pidx: int) -> dict:
    perm = parse_anim(_meta_path(name), payload_path, permutation_index=pidx)
    h = perm.header
    t_bytes = sum(c.keys_size for c in h.translation_curves)
    r_bytes = sum(c.keys_size for c in h.rotation_curves)
    s_bytes = sum(c.keys_size for c in h.scale_curves)
    total = t_bytes + r_bytes + s_bytes
    bpf = perm.bone_count * perm.keyframe_count
    return {
        "perm": perm,
        "t_bytes": t_bytes,
        "r_bytes": r_bytes,
        "s_bytes": s_bytes,
        "total": total,
        "bpbf": (total / bpf) if bpf else 0.0,
    }


def main() -> None:
    print("=" * 78)
    print("flCompression=5 RESEARCH — payload analysis")
    print("=" * 78)

    rows = []
    for name, payload_file in CORPUS:
        pp = _payload_path(name, payload_file)
        if not pp.is_file():
            print(f"\n!! missing payload for {name}: {pp}")
            continue
        # how many permutations?
        import json
        meta = json.loads(_meta_path(name).read_text(encoding="utf-8"))
        nperm = len(meta.get("ptPermutations", []))
        print(f"\n### {name}  ({nperm} permutation(s), payload {pp.stat().st_size}B)")
        for pidx in range(nperm):
            try:
                a = analyze_permutation(name, pp, pidx)
            except Exception as exc:  # noqa: BLE001
                print(f"  p{pidx}: PARSE FAILED — {exc}")
                continue
            perm = a["perm"]
            print(
                f"  p{pidx}: comp={perm.compression} bones={perm.bone_count} "
                f"frames={perm.keyframe_count} payload_off={perm.payload_offset}"
            )
            # header arrays
            for i, arr in enumerate(perm.header.raw_arrays):
                es = ANIM_SLOT_ELEMENT_SIZES[i]
                cnt = arr.data_size // es if es else 0
                flag = "" if arr.data_size else "  (empty)"
                print(
                    f"      [{i:>2}] {ANIM_SLOT_NAMES[i]:<20} "
                    f"off={arr.data_offset:>9} size={arr.data_size:>7} "
                    f"({cnt}x{es}B){flag}"
                )
            print(
                f"      curve bytes: T={a['t_bytes']} R={a['r_bytes']} "
                f"S={a['s_bytes']} total={a['total']}  "
                f"bytes/bone/frame={a['bpbf']:.3f}"
            )
            rows.append((name, pidx, perm, a))

    # ── comparison table ────────────────────────────────────────────
    print("\n" + "=" * 78)
    print("BYTE-COUNT COMPARISON TABLE")
    print("=" * 78)
    hdr = (
        f"{'File':<34} {'comp':>4} {'bones':>5} {'frames':>6} "
        f"{'T':>8} {'R':>8} {'S':>6} {'b/bone/frame':>12}"
    )
    print(hdr)
    print("-" * len(hdr))
    for name, pidx, perm, a in rows:
        print(
            f"{name + ' p' + str(pidx):<34} {perm.compression:>4} "
            f"{perm.bone_count:>5} {perm.keyframe_count:>6} "
            f"{a['t_bytes']:>8} {a['r_bytes']:>8} {a['s_bytes']:>6} "
            f"{a['bpbf']:>12.3f}"
        )


if __name__ == "__main__":
    main()
