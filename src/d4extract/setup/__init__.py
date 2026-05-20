"""First-run setup helpers (d4data fetching, install detection, etc.).

This subpackage holds *headless* setup logic — no Qt imports — so it can
be unit-tested without a display and reused from both the GUI wizard and
the CLI.
"""

from d4extract.setup.d4data_downloader import (
    D4DataInstallError,
    D4DataSource,
    default_d4data_dir,
    download_and_install_d4data,
    is_d4data_dir,
    resolve_d4data_json_path,
)

__all__ = [
    "D4DataInstallError",
    "D4DataSource",
    "default_d4data_dir",
    "download_and_install_d4data",
    "is_d4data_dir",
    "resolve_d4data_json_path",
]
