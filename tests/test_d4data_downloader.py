"""Tests for d4extract.setup.d4data_downloader.

These tests do not hit the network. The downloader supports an explicit
URL via :class:`D4DataSource(explicit_url=...)`, so we point it at a
``file://`` URL backed by a fixture zip built per-test.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from d4extract.setup.d4data_downloader import (
    D4DataInstallError,
    D4DataSource,
    default_d4data_dir,
    download_and_install_d4data,
    is_d4data_dir,
    resolve_d4data_json_path,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_zip(tmp_path: Path, *, top_dir: str, files: dict[str, bytes]) -> Path:
    """Build a zip that wraps ``files`` under ``top_dir/`` and return its path.

    Mirrors GitHub's zipball layout: a single top-level directory wraps
    the entire repo contents.
    """
    zip_path = tmp_path / f"{top_dir}.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Write the directory entry explicitly so empty dirs survive.
        zf.writestr(f"{top_dir}/", b"")
        for relpath, payload in files.items():
            zf.writestr(f"{top_dir}/{relpath}", payload)
    zip_path.write_bytes(buf.getvalue())
    return zip_path


def _file_url(path: Path) -> str:
    """Turn a Path into a ``file://`` URL urlopen will accept."""
    # ``Path.as_uri()`` handles Windows drive letters correctly.
    return path.as_uri()


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


def test_is_d4data_dir_rejects_nonexistent(tmp_path: Path) -> None:
    assert not is_d4data_dir(tmp_path / "does-not-exist")


def test_is_d4data_dir_rejects_file(tmp_path: Path) -> None:
    f = tmp_path / "not-a-dir"
    f.write_text("hi")
    assert not is_d4data_dir(f)


# ---------------------------------------------------------------------------
# default_d4data_dir
# ---------------------------------------------------------------------------


def test_default_d4data_dir_under_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "nt":
        pytest.skip("LOCALAPPDATA semantics are Windows-only")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\fake\AppData\Local")
    got = default_d4data_dir()
    # Compare via parts to stay path-separator-agnostic.
    assert got.parts[-2:] == ("d4extract", "d4data")


def test_default_d4data_dir_returns_path_object() -> None:
    # Smoke test: must return something path-like even if env is empty.
    got = default_d4data_dir()
    assert isinstance(got, Path)
    assert got.name == "d4data"


# ---------------------------------------------------------------------------
# Full install round-trip
# ---------------------------------------------------------------------------


def test_install_extracts_into_target(tmp_path: Path) -> None:
    zip_path = _make_zip(
        tmp_path,
        top_dir="d4data-master",
        files={
            "json/base/sentinel.txt": b"hello",
            "basic_definitions/types.json": b"{}",
        },
    )
    target = tmp_path / "install" / "d4data"
    source = D4DataSource(explicit_url=_file_url(zip_path))

    # Returned path is the normalised json/ subpath that the codebase
    # consumes — not the top-level install directory.
    result = download_and_install_d4data(target_dir=target, source=source)

    assert result == target / "json"
    assert (target / "json" / "base" / "sentinel.txt").read_bytes() == b"hello"
    assert (target / "basic_definitions" / "types.json").read_bytes() == b"{}"


def test_install_replaces_existing_atomically(tmp_path: Path) -> None:
    # Seed an "old" install in the same top-level + json layout.
    target = tmp_path / "install" / "d4data"
    (target / "json" / "base").mkdir(parents=True)
    (target / "json" / "base" / "old.txt").write_text("OLD")

    zip_path = _make_zip(
        tmp_path,
        top_dir="d4data-master",
        files={"json/base/new.txt": b"NEW"},
    )
    source = D4DataSource(explicit_url=_file_url(zip_path))

    download_and_install_d4data(target_dir=target, source=source)

    # Old file is gone, new file is present, and no stale .old sibling
    # remains.
    assert not (target / "json" / "base" / "old.txt").exists()
    assert (target / "json" / "base" / "new.txt").read_bytes() == b"NEW"
    assert not (target.with_suffix(target.suffix + ".old")).exists()


def test_install_invokes_progress_callback(tmp_path: Path) -> None:
    zip_path = _make_zip(
        tmp_path,
        top_dir="d4data-master",
        files={"json/base/x.txt": b"abcdef"},
    )
    target = tmp_path / "install" / "d4data"
    source = D4DataSource(explicit_url=_file_url(zip_path))

    seen_stages: set[str] = set()

    def progress(done: int, total: int | None, stage: str) -> None:
        seen_stages.add(stage)
        assert done >= 0
        if total is not None:
            assert done <= total

    download_and_install_d4data(target_dir=target, source=source, progress=progress)

    assert "extracting" in seen_stages
    # ``downloading`` may or may not fire depending on whether
    # urlopen returns a Content-Length over ``file://`` — we just
    # require that at least one stage was reported.


def test_install_rejects_archive_without_signature(tmp_path: Path) -> None:
    # Build a zip that is structurally valid but does not contain the
    # ``json/base/`` signature we require.
    zip_path = _make_zip(
        tmp_path,
        top_dir="d4data-master",
        files={"other_dir/x.txt": b""},
    )
    target = tmp_path / "install" / "d4data"
    source = D4DataSource(explicit_url=_file_url(zip_path))

    with pytest.raises(D4DataInstallError):
        download_and_install_d4data(target_dir=target, source=source)


def test_install_raises_on_bad_zip(tmp_path: Path) -> None:
    fake = tmp_path / "junk.zip"
    fake.write_bytes(b"this is definitely not a zip")
    target = tmp_path / "install" / "d4data"
    source = D4DataSource(explicit_url=_file_url(fake))

    with pytest.raises(D4DataInstallError):
        download_and_install_d4data(target_dir=target, source=source)


def test_install_raises_on_missing_url(tmp_path: Path) -> None:
    missing = tmp_path / "nope.zip"
    target = tmp_path / "install" / "d4data"
    source = D4DataSource(explicit_url=_file_url(missing))

    with pytest.raises(D4DataInstallError):
        download_and_install_d4data(target_dir=target, source=source)


# ---------------------------------------------------------------------------
# D4DataSource
# ---------------------------------------------------------------------------


def test_source_default_url_points_at_blizzhackers() -> None:
    s = D4DataSource()
    assert "blizzhackers" in s.url
    assert "d4data" in s.url
    assert s.url.endswith("master.zip")


def test_source_explicit_url_overrides() -> None:
    s = D4DataSource(explicit_url="https://example.com/foo.zip")
    assert s.url == "https://example.com/foo.zip"


def test_source_display_name() -> None:
    s = D4DataSource(owner="me", repo="r", ref="dev")
    assert s.display_name == "me/r@dev"
