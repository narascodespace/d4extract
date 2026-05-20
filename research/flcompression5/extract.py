"""Extract the flCompression=5 research test corpus from CASC.

Pulls the meta+payload .ani pair for each test animation via the
existing RustyDemonCLI wrapper (no direct rustydemon shell-out).

Each animation goes into its OWN subdirectory under ``extracted/``.
``RustyDemonCLI.extract`` snapshots the whole output dir with os.walk,
so ``extract_anim_pair`` returns the alphabetically-first meta/payload
in that dir — sharing one dir across calls makes the return tuple (and
the is_shared flag) point at the wrong file. Isolated dirs keep each
``(meta, payload, shared)`` correct, which matters for the shared
payload (warM_1HS_attk_SigilOfFlames).

Run once; payloads land under ``extracted/<name>/`` for the analysis
scripts to read.
"""

from __future__ import annotations

import sys
from pathlib import Path

# d4extract/research/flcompression5/extract.py -> d4extract/src
_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from d4extract.casc.rustydemon import RustyDemonCLI  # noqa: E402

GAME_DIR = Path(r"C:\Program Files (x86)\Diablo IV")
D4DATA = Path(
    r"C:\Users\ryant\Documents\claude\Projects\diablo4analyzer\d4data\json"
)
OUT = Path(__file__).resolve().parent / "extracted"

NAMES = [
    "warM_2HS_attk_weaponAttack",     # comp=5, simple
    "warM_1hsOh_attk_weaponAttack",   # comp=5, diff target
    "warM_demonForm_nav_runStop",     # comp=5, locomotion
    "warM_2HM_attk_apocalypse",       # comp=5 + comp=3 in one file
    "warM_1HS_attk_SigilOfFlames",    # comp=5, channeled cast (shared payload)
    "barM_HTH_nav_idle",              # comp=3 reference baseline
]


def main() -> None:
    cli = RustyDemonCLI(GAME_DIR)
    print(f"rustydemon: {cli.binary}")
    print(f"version:    {cli.version()}")
    for name in NAMES:
        sub = OUT / name
        meta, payload, shared = cli.extract_anim_pair(
            name, sub, d4data_path=D4DATA,
        )
        print(
            f"  {name}:\n"
            f"    meta    = {meta}  ({meta.stat().st_size}B)\n"
            f"    payload = {payload}  ({payload.stat().st_size}B)\n"
            f"    shared  = {shared}"
        )


if __name__ == "__main__":
    main()
