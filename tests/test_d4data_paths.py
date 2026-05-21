"""Tests for the d4data path-validation helpers.

The in-app d4data downloader was removed; what remains is two
helpers — :func:`is_d4data_dir` and :func:`resolve_d4data_json_path` —
that recognise and normalise whatever folder the user picks via the
``D4DataCard`` or ``File → Set d4data Folder…``. Both accept the repo
root (``json/base/`` inside) and the ``json/`` subdirectory (``base/``
directly inside) so the user can't trip the app by picking either
level.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from d4extract.setup.d4data_paths import (
    D4DataInstallError,
    is_d4data_dir,
    resolve_d4data_json_path,
)


# ---------------------------------------------------------------------------
# is_d4data_dir
# ---------------------------------------------------------------------------


def test_is_d4data_dir_accepts_top_level_repo(tmp_path: Path) -> None:
    """Top-level d4data repo: json/base/ exists inside the picked dir."""
    (tmp_path / "json" / "base").mkdir(parents=True)
    assert is_d4data_dir(tmp_path)


def test_is_d4data_dir_accepts_json_subdir(tmp_path: Path) -> None:
    """User picked the json/ subdir directly: base/ is immediately inside."""
    (tmp_path / "base").mkdir()
    assert is_d4data_dir(tmp_path)


def test_is_d4data_dir_rejects_empty(tmp_path: Path) -> None:
    assert not is_d4data_dir(tmp_path)


def test_is_d4data_dir_rejects_nonexistent(tmp_path: Path) -> None:
    assert not is_d4data_dir(tmp_path / "does-not-exist")


def test_is_d4data_dir_rejects_file(tmp_path: Path) -> None:
    f = tmp_path / "not-a-dir"
    f.write_text("hi")
    assert not is_d4data_dir(f)


# ---------------------------------------------------------------------------
# resolve_d4data_json_path
# ---------------------------------------------------------------------------


def test_resolve_normalises_top_level_to_json(tmp_path: Path) -> None:
    (tmp_path / "json" / "base").mkdir(parents=True)
    got = resolve_d4data_json_path(tmp_path)
    assert got == tmp_path / "json"


def test_resolve_returns_json_subdir_unchanged(tmp_path: Path) -> None:
    (tmp_path / "base").mkdir()
    got = resolve_d4data_json_path(tmp_path)
    assert got == tmp_path


def test_resolve_raises_on_invalid_dir(tmp_path: Path) -> None:
    with pytest.raises(D4DataInstallError):
        resolve_d4data_json_path(tmp_path)


# ---------------------------------------------------------------------------
# Public API surface — re-exports from ``d4extract.setup``
# ---------------------------------------------------------------------------


def test_public_api_exports_only_path_helpers() -> None:
    """The package re-exports the validators and the error type — and
    nothing else. The old ``D4DataSource`` / ``default_d4data_dir`` /
    ``download_and_install_d4data`` symbols are gone for good; this
    test fails loudly if they sneak back in."""
    import d4extract.setup as setup_pkg

    assert set(setup_pkg.__all__) == {
        "D4DataInstallError",
        "is_d4data_dir",
        "resolve_d4data_json_path",
    }
    for removed in (
        "D4DataSource",
        "default_d4data_dir",
        "download_and_install_d4data",
    ):
        assert not hasattr(setup_pkg, removed), (
            f"{removed!r} unexpectedly re-introduced into "
            "d4extract.setup; the downloader removal regressed."
        )
