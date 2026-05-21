"""First-run setup helpers (d4data path validation, install detection, etc.).

This subpackage holds *headless* setup logic — no Qt imports — so it can
be unit-tested without a display and reused from both the GUI's
``D4DataCard`` and the CLI / migration helpers.

Historically this also bundled an in-app d4data downloader; that was
removed after repeated failure modes (partial downloads, ``WinError 5``
during atomic swap with GUI handles open, slow fetches of a ~250 MB
payload) made it more trouble than it was worth. Users now obtain
d4data themselves from https://github.com/blizzhackers/d4data and point
the app at the folder.
"""

from d4extract.setup.d4data_paths import (
    D4DataInstallError,
    is_d4data_dir,
    resolve_d4data_json_path,
)

__all__ = [
    "D4DataInstallError",
    "is_d4data_dir",
    "resolve_d4data_json_path",
]
