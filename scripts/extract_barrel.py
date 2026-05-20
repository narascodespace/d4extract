#!/usr/bin/env python
"""Extract Amazon_Prop_Barrel_02_Dyn .app + .ani files from CASC.

Run from the d4extract root:
    python scripts/extract_barrel.py

Extracts to samples/base/{meta,payload}/Appearance/ and
samples/base/{meta,payload}/Anim/ alongside the existing chest fixtures.
"""

from pathlib import Path
import sys

# Add src to path so we can import d4extract
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from d4extract.casc.rustydemon import RustyDemonCLI

GAME_DIR = Path(r"C:\Program Files (x86)\Diablo IV")
D4DATA = Path(__file__).resolve().parent.parent.parent / "d4data" / "json"
SAMPLES = Path(__file__).resolve().parent.parent / "samples"

MODEL_NAME = "Amazon_Prop_Barrel_02_Dyn"
ANIM_NAMES = [
    "Amazon_Prop_Barrel_02_Dyn_Neutral",
    "Amazon_Prop_Barrel_02_Dyn_Death",
]


def main():
    print(f"Game dir:  {GAME_DIR}")
    print(f"D4data:    {D4DATA}")
    print(f"Samples:   {SAMPLES}")
    print()

    cli = RustyDemonCLI(GAME_DIR)
    print(f"rustydemon: {cli.binary}")
    print()

    # Extract model .app pair
    print(f"Extracting model: {MODEL_NAME}")
    meta, payload, shared = cli.extract_model_pair(
        MODEL_NAME, SAMPLES, d4data_path=D4DATA,
    )
    print(f"  meta:    {meta}")
    print(f"  payload: {payload}")
    print(f"  shared:  {shared}")
    print()

    # Extract animation .ani pairs
    for anim_name in ANIM_NAMES:
        print(f"Extracting animation: {anim_name}")
        try:
            meta, payload, shared = cli.extract_anim_pair(
                anim_name, SAMPLES, d4data_path=D4DATA,
            )
            print(f"  meta:    {meta}")
            print(f"  payload: {payload}")
            print(f"  shared:  {shared}")
        except Exception as e:
            print(f"  FAILED: {e}")
        print()

    print("Done! Run the barrel diagnostic script next.")


if __name__ == "__main__":
    main()
