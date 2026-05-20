"""Runtime resolver for D4 32-bit name hashes.

The lookup table at ``d4extract/data/hash_names.json`` maps decimal hash
keys to plaintext names recovered by ``tools/hash_dictionary_attack.py``.
The hash function is DJB2 with seed=0 (see ``tools/hash_identify.py``).

Only **exact** 32-bit matches are loaded — speculative masked-only hits
live in a sibling ``hash_names_speculative.json`` and are intentionally
not consulted from production code.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Final

# d4extract/src/d4extract/formats/hash_names.py
#                             ^---- parents[0]
#                       ^---------- parents[1]  (package: d4extract)
#                  ^-------------- parents[2]   (src/)
#             ^------------------- parents[3]   (project subdir: d4extract/)
_DATA_PATH: Final = Path(__file__).resolve().parents[3] / "data" / "hash_names.json"


def _load() -> dict[int, str]:
    try:
        raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {int(k): v for k, v in raw.items()}


_TABLE: Final[dict[int, str]] = _load()


def resolve_hash(hash_value: int) -> str | None:
    """Return the plaintext name for ``hash_value`` or ``None``.

    ``hash_value`` is the raw 32-bit DJB2 result observed in the binary.
    """
    return _TABLE.get(hash_value)
