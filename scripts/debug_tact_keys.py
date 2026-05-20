"""Debug whether RustyDemonCLI is forwarding TACT keys to rustydemon-cli.

Instantiates RustyDemonCLI against the configured game install, prints
the resolved tact_keys_path and key count, then runs list_files with a
*stor251* filter to see whether the encrypted entry shows up.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make the package importable when running from a checkout without install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from d4extract.casc.rustydemon import RustyDemonCLI  # noqa: E402
from d4extract.gui.settings import AppSettings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)


def count_keys(path: Path) -> int:
    """Count non-blank, non-comment lines in a TACT key file."""
    total = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            total += 1
    return total


def main() -> int:
    # Pull the same path the GUI uses (QSettings-backed).
    settings = AppSettings()
    game_dir = settings.game_dir()
    if game_dir is None:
        print("ERROR: no game_dir configured in QSettings — open the GUI once "
              "and select your D4 install, or set it manually.")
        return 1
    print(f"game_dir: {game_dir}")

    rd = RustyDemonCLI(game_dir)
    print(f"binary:          {rd.binary}")
    print(f"tact_keys_path:  {rd.tact_keys_path}")

    if rd.tact_keys_path is None:
        print("ERROR: tact_keys_path is None — auto-discovery failed.")
        return 2
    if not rd.tact_keys_path.is_file():
        print(f"ERROR: tact_keys_path does not point to a file: {rd.tact_keys_path}")
        return 3

    n_keys = count_keys(rd.tact_keys_path)
    print(f"keys in file:    {n_keys}")

    print("\n--- list_files(path_filter='base/**/*stor251*') ---")
    entries = rd.list_files("base/**/*stor251*")
    print(f"entries returned: {len(entries)}")
    for entry in entries[:50]:
        print(f"  {entry.size:>10}  {entry.path}")
    if len(entries) > 50:
        print(f"  … and {len(entries) - 50} more")

    sorf_hits = [e for e in entries if "sorf_stor251trs" in e.path.lower()]
    print(f"\nsorF_stor251TRS hits: {len(sorf_hits)}")
    for hit in sorf_hits:
        print(f"  {hit.size:>10}  {hit.path}")

    # Diagnostic: which class prefixes appear in this stor251 set?
    from collections import Counter
    prefixes: Counter[str] = Counter()
    for e in entries:
        fname = e.path.rsplit("/", 1)[-1]
        prefix = fname.split("_", 1)[0]
        prefixes[prefix.lower()] += 1
    print("\nclass prefixes in stor251 listing:")
    for prefix, count in prefixes.most_common():
        print(f"  {count:>4}  {prefix}")

    return 0 if sorf_hits else 4


if __name__ == "__main__":
    sys.exit(main())
