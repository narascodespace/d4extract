"""Extract the flCompression=1 research test corpus from CASC.

Mirrors research/flcompression5/extract.py. Uses
``RustyDemonCLI.extract_anim_pair_batch`` — one CASC open for the whole
corpus — and indexes results by file stem, so the output-dir-reuse bug
that affects ``extract_anim_pair`` does not apply.

Run once; payloads land under ``extracted/base/payload/Anim/`` for the
analysis scripts to read.
"""

from __future__ import annotations

import sys
from pathlib import Path

# d4extract/research/flcompression1/extract.py -> d4extract/src
_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SRC))

from d4extract.casc.rustydemon import RustyDemonCLI  # noqa: E402

GAME_DIR = Path(r"C:\Program Files (x86)\Diablo IV")
D4DATA = Path(
    r"C:\Users\ryant\Documents\claude\Projects\diablo4analyzer\d4data\json"
)
OUT = Path(__file__).resolve().parent / "extracted"

# Primary: comp=1 player content (the 10 known Character Builder failures).
COMP1_PLAYER = [
    "barM_mount_hth_event_dismount_damage",
    "warM_mount_horse_reac_dismount",
    "spiF_mount_hth_event_dismount_damage",
    "spiF_gla_attk_centipede_core",
    "spiF_gla_attk_gorillaDefensive",
    "spiF_gla_attk_sky_basic_new",
    "spiF_gla_attk_Plains_Offense_temp",
    "spiF_gla_attk_guardianImpact_repeat",
    "spiF_gla_nav_evade",                  # CROSS-MODE: p0 c1, p1-3 c3
    "spiF_hth_emote_taunt",
]

# Secondary: IGC sample (also comp=1).
IGC = ["IGC_CBH_t3barbarianm_1010"]

# comp=1 cross-mode jackpot candidates (mixed comp=1 + comp=3/5 in one
# payload — lets us test for physically-aliased curve blobs).
COMP1_CROSSMODE = [
    "druM_2HS_attk_StoneBurst",            # [1, 1, 1, 3]
    "palM_1hsShd_attk_impale",             # [5, 1]
    "druM_HTH_trav_ladder_climb_down",     # [3, 1, 3]
]

# comp=2 / comp=4 / comp=6 samples (secondary-task prevalence check).
OTHER_MODES = [
    "bandit_sword_nav_run",                # comp=2 [2, 3, 3] cross-mode
    "bandit_sword_nav_walk",               # comp=2 [2] pure
    "treasuregoblin_nav_idle_unalert_outro_still",  # comp=4 — ONLY file
    "warlock_tailStrike_attk_basic",       # comp=6 [6, 6, 3, 3, 3] cross-mode
]

ALL = COMP1_PLAYER + IGC + COMP1_CROSSMODE + OTHER_MODES


def main() -> None:
    cli = RustyDemonCLI(GAME_DIR)
    print(f"rustydemon: {cli.binary}")
    print(f"version:    {cli.version()}")
    print(f"extracting {len(ALL)} animations...")
    result = cli.extract_anim_pair_batch(ALL, OUT, d4data_path=D4DATA)
    print(f"extracted {len(result)}/{len(ALL)} animation payloads:\n")
    for name in ALL:
        entry = result.get(name)
        if entry is None:
            print(f"  {name}: NOT EXTRACTED")
            continue
        _meta, payload, shared = entry
        print(
            f"  {name}: payload={payload.stat().st_size}B shared={shared}"
        )


if __name__ == "__main__":
    main()
