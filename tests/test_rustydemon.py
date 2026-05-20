"""Tests for the rustydemon-cli wrapper and CASC data model."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from d4extract.casc.archive import CASCArchive, CASCEntry
from d4extract.casc.rustydemon import CASCExtractionError, RustyDemonCLI


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _make_game_dir(tmp_path: Path) -> Path:
    """Create a minimal fake D4 game directory."""
    game_dir = tmp_path / "diablo4"
    game_dir.mkdir()
    (game_dir / "Data").mkdir()
    return game_dir


def _make_rd(tmp_path: Path) -> RustyDemonCLI:
    """Create a RustyDemonCLI with a fake game dir and binary."""
    game_dir = _make_game_dir(tmp_path)
    with patch.object(RustyDemonCLI, "find_binary", return_value=Path("/fake/bin")):
        return RustyDemonCLI(game_dir)


@pytest.fixture(autouse=True)
def _isolate_tact_keys(request, monkeypatch):
    """Keep tests Qt-free and independent of the dev machine's QSettings.

    ``RustyDemonCLI.__init__`` calls ``find_tact_keys``, which (after
    this branch) lazy-imports ``PySide6.QtCore.QSettings``. We don't
    want to drag Qt into the unit test suite (see
    ``feedback_qt_free_tests``) and we don't want a developer's
    GUI-saved TACT keys path to leak into ``rd.tact_keys_path``. So
    every test in this file gets ``find_tact_keys`` short-circuited to
    return ``None`` and the env var cleared.

    The ``TestFindTactKeys`` class opts out so it can exercise the
    real precedence logic with explicit mocks.
    """
    if request.cls is not None and request.cls.__name__ == "TestFindTactKeys":
        yield
        return
    monkeypatch.delenv("D4EXTRACT_TACT_KEYS", raising=False)
    monkeypatch.setattr(
        RustyDemonCLI, "find_tact_keys", classmethod(lambda cls: None),
    )
    yield


# ---------------------------------------------------------------
# CASCEntry data model
# ---------------------------------------------------------------


class TestCASCEntry:
    def test_basic_creation(self):
        entry = CASCEntry(path="base/meta/Appearance/Sorc.app", size=1024)
        assert entry.path == "base/meta/Appearance/Sorc.app"
        assert entry.size == 1024
        assert entry.is_directory is False

    def test_directory_entry(self):
        entry = CASCEntry(path="base/meta/Appearance/", size=0, is_directory=True)
        assert entry.is_directory is True

    def test_name_property(self):
        entry = CASCEntry(path="base/meta/Appearance/Sorc.app", size=0)
        assert entry.name == "Sorc.app"

    def test_name_no_directory(self):
        entry = CASCEntry(path="file.app", size=0)
        assert entry.name == "file.app"

    def test_extension(self):
        entry = CASCEntry(path="base/models/test.app", size=0)
        assert entry.extension == ".app"

    def test_extension_none(self):
        entry = CASCEntry(path="base/models/noext", size=0)
        assert entry.extension == ""

    def test_extension_multiple_dots(self):
        entry = CASCEntry(path="base/models/test.backup.app", size=0)
        assert entry.extension == ".app"


# ---------------------------------------------------------------
# CASCArchive data model
# ---------------------------------------------------------------


class TestCASCArchive:
    @pytest.fixture()
    def archive(self) -> CASCArchive:
        entries = [
            CASCEntry("base/meta/Appearance/Characters/Sorc.app", 100),
            CASCEntry("base/meta/Appearance/Weapons/Sword.app", 200),
            CASCEntry("base/payload/Appearance/Characters/Sorc.app", 5000),
            CASCEntry("base/payload/Appearance/Weapons/Sword.app", 8000),
            CASCEntry("base/meta/Other/config.dat", 50),
        ]
        return CASCArchive(
            game_dir=Path("C:/fake/diablo4"),
            product="fenris",
            entries=entries,
        )

    def test_filter_glob_matches(self, archive: CASCArchive):
        # fnmatch.fnmatch treats * as matching everything including /
        result = archive.filter("base/meta/Appearance/*.app")
        assert len(result) == 2
        assert result[0].path == "base/meta/Appearance/Characters/Sorc.app"
        assert result[1].path == "base/meta/Appearance/Weapons/Sword.app"

    def test_filter_glob_excludes_non_matching(self, archive: CASCArchive):
        result = archive.filter("base/meta/Appearance/*.app")
        paths = {e.path for e in result}
        assert "base/meta/Other/config.dat" not in paths
        assert "base/payload/Appearance/Characters/Sorc.app" not in paths

    def test_filter_exact(self, archive: CASCArchive):
        result = archive.filter("base/meta/Other/config.dat")
        assert len(result) == 1
        assert result[0].size == 50

    def test_filter_no_match(self, archive: CASCArchive):
        result = archive.filter("nonexistent/*")
        assert len(result) == 0

    def test_appearances(self, archive: CASCArchive):
        apps = archive.appearances()
        assert len(apps) == 4
        assert all(e.path.endswith(".app") for e in apps)
        assert all("/Appearance/" in e.path for e in apps)

    def test_meta_appearances(self, archive: CASCArchive):
        metas = archive.meta_appearances()
        assert len(metas) == 2
        assert all(e.path.startswith("base/meta/Appearance/") for e in metas)

    def test_payload_appearances(self, archive: CASCArchive):
        payloads = archive.payload_appearances()
        assert len(payloads) == 2
        assert all(e.path.startswith("base/payload/Appearance/") for e in payloads)

    def test_empty_archive(self):
        archive = CASCArchive(game_dir=Path("/fake"), product="fenris")
        assert archive.filter("*") == []
        assert archive.appearances() == []
        assert archive.meta_appearances() == []
        assert archive.payload_appearances() == []


# ---------------------------------------------------------------
# CASCExtractionError
# ---------------------------------------------------------------


class TestCASCExtractionError:
    def test_message(self):
        err = CASCExtractionError("something broke")
        assert str(err) == "something broke"
        assert err.cmd is None
        assert err.stderr == ""

    def test_with_cmd_and_stderr(self):
        err = CASCExtractionError(
            "failed", cmd=["rustydemon-cli", "list"], stderr="bad input"
        )
        assert err.cmd == ["rustydemon-cli", "list"]
        assert err.stderr == "bad input"


# ---------------------------------------------------------------
# RustyDemonCLI.find_binary
# ---------------------------------------------------------------


class TestFindBinary:
    def _env_without_rustydemon(self) -> dict[str, str]:
        """Current env minus D4EXTRACT_RUSTYDEMON."""
        return {k: v for k, v in os.environ.items() if k != "D4EXTRACT_RUSTYDEMON"}

    def test_returns_none_when_not_installed(self):
        """find_binary should return None gracefully, not raise."""
        with (
            patch.dict("os.environ", self._env_without_rustydemon(), clear=True),
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.is_file", return_value=False),
        ):
            result = RustyDemonCLI.find_binary()
            assert result is None

    def test_env_var_override(self, tmp_path: Path):
        binary = tmp_path / "rustydemon-cli"
        binary.touch()
        with patch.dict("os.environ", {"D4EXTRACT_RUSTYDEMON": str(binary)}):
            result = RustyDemonCLI.find_binary()
            assert result is not None
            assert result.name == "rustydemon-cli"

    def test_env_var_missing_file_falls_through(self, tmp_path: Path):
        """If D4EXTRACT_RUSTYDEMON points to a nonexistent file, keep searching."""
        with (
            patch.dict(
                "os.environ",
                {**self._env_without_rustydemon(), "D4EXTRACT_RUSTYDEMON": "/no/such/file"},
                clear=True,
            ),
            patch("shutil.which", return_value=None),
            patch("pathlib.Path.is_file", return_value=False),
        ):
            result = RustyDemonCLI.find_binary()
            assert result is None

    def test_system_path(self):
        with (
            patch.dict("os.environ", self._env_without_rustydemon(), clear=True),
            patch("shutil.which", return_value="/usr/bin/rustydemon-cli"),
        ):
            result = RustyDemonCLI.find_binary()
            assert result is not None


# ---------------------------------------------------------------
# RustyDemonCLI.__init__ validation
# ---------------------------------------------------------------


class TestRustyDemonInit:
    def test_missing_game_dir(self, tmp_path: Path):
        fake_dir = tmp_path / "nonexistent"
        with pytest.raises(CASCExtractionError, match="does not exist"):
            RustyDemonCLI(fake_dir)

    def test_invalid_game_dir(self, tmp_path: Path):
        """A directory without D4 markers should fail validation."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with pytest.raises(CASCExtractionError, match="does not look like"):
            RustyDemonCLI(empty_dir)

    def test_no_binary_found(self, tmp_path: Path):
        """Should raise with install instructions when binary not found."""
        game_dir = _make_game_dir(tmp_path)
        with patch.object(RustyDemonCLI, "find_binary", return_value=None):
            with pytest.raises(CASCExtractionError, match="rustydemon-cli not found"):
                RustyDemonCLI(game_dir)

    def test_valid_game_dir_with_data(self, tmp_path: Path):
        game_dir = _make_game_dir(tmp_path)
        with patch.object(RustyDemonCLI, "find_binary", return_value=Path("/fake/bin")):
            rd = RustyDemonCLI(game_dir)
            assert rd.game_dir == game_dir.resolve()

    def test_valid_game_dir_with_build_info(self, tmp_path: Path):
        game_dir = tmp_path / "diablo4"
        game_dir.mkdir()
        (game_dir / ".build.info").touch()
        with patch.object(RustyDemonCLI, "find_binary", return_value=Path("/fake/bin")):
            rd = RustyDemonCLI(game_dir)
            assert rd.game_dir == game_dir.resolve()

    def test_explicit_binary_path(self, tmp_path: Path):
        game_dir = _make_game_dir(tmp_path)
        rd = RustyDemonCLI(game_dir, binary=Path("/my/rustydemon-cli"))
        assert rd.binary == Path("/my/rustydemon-cli")


# ---------------------------------------------------------------
# RustyDemonCLI._parse_file_listing
# ---------------------------------------------------------------


class TestParseFileListing:
    def test_tab_separated(self):
        stdout = "base/meta/Appearance/Sorc.app\t1024\nbase/meta/Other.dat\t512\n"
        entries = RustyDemonCLI._parse_file_listing(stdout)
        assert len(entries) == 2
        assert entries[0].path == "base/meta/Appearance/Sorc.app"
        assert entries[0].size == 1024
        assert entries[1].path == "base/meta/Other.dat"
        assert entries[1].size == 512

    def test_tab_separated_invalid_size(self):
        stdout = "base/test.app\tNOTANUMBER\n"
        entries = RustyDemonCLI._parse_file_listing(stdout)
        assert len(entries) == 1
        assert entries[0].size == 0

    def test_space_separated(self):
        stdout = "base/meta/Appearance/Sorc.app  1024\n"
        entries = RustyDemonCLI._parse_file_listing(stdout)
        assert len(entries) == 1
        assert entries[0].path == "base/meta/Appearance/Sorc.app"
        assert entries[0].size == 1024

    def test_path_only(self):
        stdout = "base/meta/Appearance/Sorc.app\n"
        entries = RustyDemonCLI._parse_file_listing(stdout)
        assert len(entries) == 1
        assert entries[0].path == "base/meta/Appearance/Sorc.app"
        assert entries[0].size == 0

    def test_empty_lines_and_comments_skipped(self):
        stdout = "\n# header\n\nbase/test.app\t100\n\n# footer\n"
        entries = RustyDemonCLI._parse_file_listing(stdout)
        assert len(entries) == 1
        assert entries[0].path == "base/test.app"

    def test_empty_string(self):
        assert RustyDemonCLI._parse_file_listing("") == []

    def test_whitespace_only(self):
        assert RustyDemonCLI._parse_file_listing("   \n  \n") == []


# ---------------------------------------------------------------
# RustyDemonCLI._run (subprocess wrapper)
# ---------------------------------------------------------------


class TestRun:
    def test_success(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok\n", stderr=""
        )
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            result = rd._run(["--version"])
            assert result.stdout == "ok\n"
            mock_run.assert_called_once()
            # Verify the binary path is the first arg
            call_args = mock_run.call_args[0][0]
            assert call_args[0] == str(rd.binary)
            assert "--version" in call_args

    def test_wraps_called_process_error(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(
                1, "rustydemon-cli", stderr="some error"
            ),
        ):
            with pytest.raises(CASCExtractionError, match="exit 1") as exc_info:
                rd._run(["list"])
            assert exc_info.value.stderr == "some error"
            assert exc_info.value.cmd is not None


# ---------------------------------------------------------------
# RustyDemonCLI.version
# ---------------------------------------------------------------


class TestVersion:
    def test_returns_stripped_stdout(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="rustydemon-cli 0.3.1\n", stderr=""
        )
        with patch("subprocess.run", return_value=fake_result):
            assert rd.version() == "rustydemon-cli 0.3.1"


# ---------------------------------------------------------------
# RustyDemonCLI.list_files
# ---------------------------------------------------------------


class TestListFiles:
    def test_constructs_command_without_filter(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            rd.list_files()
            cmd = mock_run.call_args[0][0]
            assert "export" in cmd
            assert "--archive" in cmd
            assert "--dry-run" in cmd
            # Default path filter is "base/*"
            path_idx = cmd.index("--path")
            assert cmd[path_idx + 1] == "base/*"

    def test_constructs_command_with_filter(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="base/test.app\t100\n", stderr=""
        )
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            entries = rd.list_files("base/meta/*")
            cmd = mock_run.call_args[0][0]
            path_idx = cmd.index("--path")
            assert cmd[path_idx + 1] == "base/meta/*"
            assert len(entries) == 1

    def test_parses_output(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        fake_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="base/meta/Appearance/Sorc.app\t1024\nbase/meta/Appearance/Barb.app\t2048\n",
            stderr="",
        )
        with patch("subprocess.run", return_value=fake_result):
            entries = rd.list_files()
            assert len(entries) == 2
            assert entries[0].path == "base/meta/Appearance/Sorc.app"
            assert entries[1].size == 2048


# ---------------------------------------------------------------
# RustyDemonCLI.extract
# ---------------------------------------------------------------


class TestExtract:
    def test_constructs_command(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "out"

        with patch("subprocess.Popen") as mock_popen:
            proc_mock = mock_popen.return_value
            proc_mock.stderr = iter([])
            proc_mock.stdout = None
            proc_mock.wait.return_value = None
            proc_mock.returncode = 0

            rd.extract(["base/meta/*", "base/payload/*"], output_dir, workers=8)

            cmd = mock_popen.call_args[0][0]
            assert "export" in cmd
            assert "--archive" in cmd
            assert "--parallel" in cmd
            parallel_idx = cmd.index("--parallel")
            assert cmd[parallel_idx + 1] == "8"
            assert "--flat" not in cmd
            assert "--overwrite" in cmd
            # Both patterns passed
            path_indices = [i for i, v in enumerate(cmd) if v == "--path"]
            assert len(path_indices) == 2

    def test_flatten_flag(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "out"

        with patch("subprocess.Popen") as mock_popen:
            proc_mock = mock_popen.return_value
            proc_mock.stderr = iter([])
            proc_mock.stdout = None
            proc_mock.wait.return_value = None
            proc_mock.returncode = 0

            rd.extract(["base/*"], output_dir, flatten=True)

            cmd = mock_popen.call_args[0][0]
            assert "--flat" in cmd

    def test_collects_extracted_files(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "out"
        # Pre-create files that would have been "extracted"
        (output_dir / "base" / "meta").mkdir(parents=True)
        (output_dir / "base" / "meta" / "Sorc.app").touch()
        (output_dir / "base" / "meta" / "Barb.app").touch()

        with patch("subprocess.Popen") as mock_popen:
            proc_mock = mock_popen.return_value
            proc_mock.stderr = iter([])
            proc_mock.stdout = None
            proc_mock.wait.return_value = None
            proc_mock.returncode = 0

            result = rd.extract(["base/*"], output_dir)
            assert len(result) == 2
            names = {p.name for p in result}
            assert names == {"Barb.app", "Sorc.app"}

    def test_creates_output_dir(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "deep" / "nested" / "out"
        assert not output_dir.exists()

        with patch("subprocess.Popen") as mock_popen:
            proc_mock = mock_popen.return_value
            proc_mock.stderr = iter([])
            proc_mock.stdout = None
            proc_mock.wait.return_value = None
            proc_mock.returncode = 0

            rd.extract(["base/*"], output_dir)
            assert output_dir.is_dir()


# ---------------------------------------------------------------
# RustyDemonCLI._run_streaming
# ---------------------------------------------------------------


class TestRunStreaming:
    def test_raises_on_nonzero_exit(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        with patch("subprocess.Popen") as mock_popen:
            proc_mock = mock_popen.return_value
            proc_mock.stderr = iter([])
            proc_mock.stdout = None
            proc_mock.wait.return_value = None
            proc_mock.returncode = 1

            with pytest.raises(CASCExtractionError, match="exit 1"):
                rd._run_streaming(["extract"])

    def test_raises_on_missing_binary(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        with patch("subprocess.Popen", side_effect=FileNotFoundError):
            with pytest.raises(CASCExtractionError, match="Could not execute"):
                rd._run_streaming(["extract"])


# ---------------------------------------------------------------
# RustyDemonCLI.extract_model_pair
# ---------------------------------------------------------------


class TestExtractModelPair:
    def test_constructs_correct_patterns(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        captured_patterns: list[str] = []

        def fake_extract(patterns, output_dir, **kwargs):
            captured_patterns.extend(patterns)
            meta = output_dir / "base" / "meta" / "Appearance" / "TestModel.app"
            payload = output_dir / "base" / "payload" / "Appearance" / "TestModel.app"
            meta.parent.mkdir(parents=True, exist_ok=True)
            payload.parent.mkdir(parents=True, exist_ok=True)
            meta.touch()
            payload.touch()
            return [meta, payload]

        with patch.object(rd, "extract", side_effect=fake_extract):
            meta, data, is_shared = rd.extract_model_pair(
                "TestModel", tmp_path / "out",
            )

        assert "base/meta/Appearance/*TestModel*" in captured_patterns
        assert "base/payload/Appearance/*TestModel*" in captured_patterns
        assert "meta" in str(meta)
        assert "payload" in str(data)
        assert is_shared is False

    def test_raises_when_no_files_extracted(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        with patch.object(rd, "extract", return_value=[]):
            with pytest.raises(CASCExtractionError, match="No meta"):
                rd.extract_model_pair("Missing", tmp_path / "out")

    def test_raises_when_payload_missing(self, tmp_path: Path):
        """Meta found but no payload should raise about missing payload."""
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "extract_out"
        meta_file = output_dir / "base" / "meta" / "Appearance" / "Model.app"
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        meta_file.touch()

        with patch.object(rd, "extract", return_value=[meta_file]):
            with pytest.raises(CASCExtractionError, match="No payload"):
                rd.extract_model_pair("Model", output_dir)

    def test_raises_when_meta_missing_but_payload_found(self, tmp_path: Path):
        """Payload found but no meta should raise about missing meta."""
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "extract_out"
        payload_file = output_dir / "base" / "payload" / "Appearance" / "Model.app"
        payload_file.parent.mkdir(parents=True, exist_ok=True)
        payload_file.touch()

        with patch.object(rd, "extract", return_value=[payload_file]):
            with pytest.raises(CASCExtractionError, match="No meta"):
                rd.extract_model_pair("Model", output_dir)

    def test_uses_workers_2(self, tmp_path: Path):
        """extract_model_pair should use workers=2."""
        rd = _make_rd(tmp_path)
        captured_kwargs: dict = {}

        def fake_extract(patterns, output_dir, **kwargs):
            captured_kwargs.update(kwargs)
            meta = output_dir / "base" / "meta" / "Appearance" / "X.app"
            payload = output_dir / "base" / "payload" / "Appearance" / "X.app"
            meta.parent.mkdir(parents=True, exist_ok=True)
            payload.parent.mkdir(parents=True, exist_ok=True)
            meta.touch()
            payload.touch()
            return [meta, payload]

        with patch.object(rd, "extract", side_effect=fake_extract):
            rd.extract_model_pair("X", tmp_path / "out")

        assert captured_kwargs.get("workers") == 2

    def test_falls_back_to_shared_payload_override(self, tmp_path: Path):
        """When the same-name payload misses and a d4data override exists,
        the resolver path should be re-extracted and returned."""
        import json

        from d4extract.casc import payload_resolver

        payload_resolver.clear_cache()
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "extract_out"
        meta_path = output_dir / "base" / "meta" / "Appearance" / "Aliased.app"
        override_payload = (
            output_dir / "base" / "payload" / "Appearance" / "RealHost.app"
        )

        # Stand up a fake d4data with the mapping file.
        d4data = tmp_path / "d4data" / "json"
        (d4data / "base").mkdir(parents=True)
        (d4data / "base" / "CoreTOCSharedPayloadsMapping.dat.json").write_text(
            json.dumps({
                "base/payload/Appearance/Aliased.app":
                    "base/payload/Appearance/RealHost.app",
            }),
            encoding="utf-8",
        )

        captured_patterns: list[str] = []

        def fake_extract(patterns, out_dir, **kwargs):
            captured_patterns.extend(patterns)
            pat = patterns[0]
            if pat.startswith("base/meta/"):
                meta_path.parent.mkdir(parents=True, exist_ok=True)
                meta_path.touch()
                return [meta_path]
            # Same-name payload pattern: produces nothing.
            if "*Aliased*" in pat:
                return []
            # Override extraction (exact path) writes the real payload.
            if pat == "base/payload/Appearance/RealHost.app":
                override_payload.parent.mkdir(parents=True, exist_ok=True)
                override_payload.touch()
                return [meta_path, override_payload]
            return []

        with patch.object(rd, "extract", side_effect=fake_extract):
            meta, data, is_shared = rd.extract_model_pair(
                "Aliased", output_dir, d4data_path=d4data,
            )

        assert meta == meta_path
        assert data == override_payload
        assert is_shared is True
        assert "base/payload/Appearance/RealHost.app" in captured_patterns
        payload_resolver.clear_cache()

    def test_no_d4data_path_does_not_consult_resolver(self, tmp_path: Path):
        """Without d4data_path, missing payload should error without
        attempting the override lookup. Error message hints at d4data."""
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "extract_out"
        meta_file = output_dir / "base" / "meta" / "Appearance" / "Aliased.app"
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        meta_file.touch()

        with patch.object(rd, "extract", return_value=[meta_file]):
            with pytest.raises(CASCExtractionError) as exc_info:
                rd.extract_model_pair("Aliased", output_dir)

        assert "shared payload" in str(exc_info.value)

    def test_shared_payload_detected_from_stale_cache(self, tmp_path: Path):
        """A previously-resolved override file left on disk by an earlier
        session must still be flagged as ``is_shared_payload=True`` —
        ``self.extract()`` snapshots the whole output_dir and returns the
        stale file even when the same-name CASC pattern matches nothing.
        Without the stem-based detection the link-ID check would fire.
        """
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "extract_out"
        meta_path = output_dir / "base" / "meta" / "Appearance" / "Aliased.app"
        # The override payload from a previous run is already on disk —
        # the resolver branch should NOT need to be entered to recognise
        # this as a shared payload.
        stale_payload = (
            output_dir / "base" / "payload" / "Appearance" / "RealHost.app"
        )
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.touch()
        stale_payload.parent.mkdir(parents=True, exist_ok=True)
        stale_payload.touch()

        # Both the meta and payload "extract" calls return the cached
        # files via the os.walk snapshot; no extra extraction happens.
        def fake_extract(patterns, out_dir, **kwargs):
            return [meta_path, stale_payload]

        with patch.object(rd, "extract", side_effect=fake_extract):
            meta, data, is_shared = rd.extract_model_pair(
                "Aliased", output_dir,
            )

        assert meta == meta_path
        assert data == stale_payload
        assert is_shared is True

    def test_override_miss_still_raises(self, tmp_path: Path):
        """If the resolver returns None, the fallback raises as before."""
        import json

        from d4extract.casc import payload_resolver

        payload_resolver.clear_cache()
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "extract_out"
        meta_file = output_dir / "base" / "meta" / "Appearance" / "Solo.app"
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        meta_file.touch()

        d4data = tmp_path / "d4data" / "json"
        (d4data / "base").mkdir(parents=True)
        (d4data / "base" / "CoreTOCSharedPayloadsMapping.dat.json").write_text(
            json.dumps({}), encoding="utf-8",
        )

        with patch.object(rd, "extract", return_value=[meta_file]):
            with pytest.raises(CASCExtractionError, match="No payload"):
                rd.extract_model_pair(
                    "Solo", output_dir, d4data_path=d4data,
                )
        payload_resolver.clear_cache()


# ---------------------------------------------------------------
# RustyDemonCLI.extract / extract_many — overwrite control
# ---------------------------------------------------------------


def _streaming_popen(mock_popen) -> None:
    """Wire a Popen mock so _run_streaming sees a clean exit."""
    proc_mock = mock_popen.return_value
    proc_mock.stderr = iter([])
    proc_mock.stdout = None
    proc_mock.wait.return_value = None
    proc_mock.returncode = 0


class TestExtractOverwrite:
    def test_overwrite_true_by_default(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        with patch("subprocess.Popen") as mock_popen:
            _streaming_popen(mock_popen)
            rd.extract(["base/*"], tmp_path / "out")
            assert "--overwrite" in mock_popen.call_args[0][0]

    def test_overwrite_false_omits_flag(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        with patch("subprocess.Popen") as mock_popen:
            _streaming_popen(mock_popen)
            rd.extract(["base/*"], tmp_path / "out", overwrite=False)
            assert "--overwrite" not in mock_popen.call_args[0][0]

    def test_extract_many_forwards_overwrite(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        captured: list[dict] = []

        def fake_extract(patterns, output_dir, **kwargs):
            captured.append(kwargs)
            return []

        with patch.object(rd, "extract", side_effect=fake_extract):
            rd.extract_many(
                ["base/payload/Texture/a.tex"], tmp_path / "out",
                overwrite=False,
            )

        assert captured
        assert all(k.get("overwrite") is False for k in captured)


# ---------------------------------------------------------------
# RustyDemonCLI.extract_anim_pair_batch
# ---------------------------------------------------------------


class TestExtractAnimPairBatch:
    @staticmethod
    def _land(output_dir: Path, *, meta=(), payload=()) -> None:
        """Create fake extracted .ani files under ``output_dir``."""
        meta_d = output_dir / "base" / "meta" / "Anim"
        payload_d = output_dir / "base" / "payload" / "Anim"
        meta_d.mkdir(parents=True, exist_ok=True)
        payload_d.mkdir(parents=True, exist_ok=True)
        for n in meta:
            (meta_d / f"{n}.ani").touch()
        for n in payload:
            (payload_d / f"{n}.ani").touch()

    def test_single_bulk_call_covers_meta_and_payload(self, tmp_path: Path):
        """Two anims collapse into one brace glob spanning both trees."""
        rd = _make_rd(tmp_path)
        out = tmp_path / "out"
        patterns: list[str] = []
        kwargs_seen: list[dict] = []

        def fake_extract(pats, output_dir, **kwargs):
            patterns.append(pats[0])
            kwargs_seen.append(kwargs)
            self._land(output_dir, meta=("a", "b"), payload=("a", "b"))
            return []

        with patch.object(rd, "extract", side_effect=fake_extract):
            result = rd.extract_anim_pair_batch(["a", "b"], out)

        assert patterns == ["base/{meta,payload}/Anim/{a.ani,b.ani}"]
        assert kwargs_seen[0]["overwrite"] is False
        assert set(result) == {"a", "b"}
        meta_a, payload_a, shared_a = result["a"]
        assert meta_a.name == "a.ani" and "meta" in meta_a.parts
        assert payload_a.name == "a.ani" and "payload" in payload_a.parts
        assert shared_a is False

    def test_single_name_uses_plain_name(self, tmp_path: Path):
        """One name needs no inner brace group — just the meta/payload one."""
        rd = _make_rd(tmp_path)
        out = tmp_path / "out"
        patterns: list[str] = []

        def fake_extract(pats, output_dir, **kwargs):
            patterns.append(pats[0])
            self._land(output_dir, meta=("solo",), payload=("solo",))
            return []

        with patch.object(rd, "extract", side_effect=fake_extract):
            result = rd.extract_anim_pair_batch(["solo"], out)

        assert patterns == ["base/{meta,payload}/Anim/solo.ani"]
        assert "solo" in result

    def test_chunks_by_group_size(self, tmp_path: Path):
        """N names with group_size G issue ceil(N/G) bulk calls."""
        rd = _make_rd(tmp_path)
        out = tmp_path / "out"
        patterns: list[str] = []

        def fake_extract(pats, output_dir, **kwargs):
            patterns.append(pats[0])
            return []

        with (
            patch.object(rd, "extract", side_effect=fake_extract),
            patch.object(
                rd, "extract_anim_pair",
                side_effect=CASCExtractionError("not found"),
            ),
        ):
            rd.extract_anim_pair_batch(
                [f"anim{i}" for i in range(5)], out, group_size=2,
            )

        assert len(patterns) == 3

    def test_dedupes_repeated_names(self, tmp_path: Path):
        rd = _make_rd(tmp_path)
        out = tmp_path / "out"
        patterns: list[str] = []

        def fake_extract(pats, output_dir, **kwargs):
            patterns.append(pats[0])
            self._land(output_dir, meta=("a", "b"), payload=("a", "b"))
            return []

        with patch.object(rd, "extract", side_effect=fake_extract):
            result = rd.extract_anim_pair_batch(["a", "b", "a", "b"], out)

        assert patterns == ["base/{meta,payload}/Anim/{a.ani,b.ani}"]
        assert set(result) == {"a", "b"}

    def test_bulk_miss_falls_back_to_extract_anim_pair(self, tmp_path: Path):
        """A miss with no shared-payload mapping entry falls through to
        the per-name extract_anim_pair retry (the last-resort path).

        With no CoreTOCSharedPayloadsMapping present, the batched alias
        resolution finds nothing, so the name lands in the last-resort
        loop exactly as a genuinely-broken animation would.
        """
        rd = _make_rd(tmp_path)
        out = tmp_path / "out"

        def fake_extract(pats, output_dir, **kwargs):
            self._land(output_dir, meta=("shared",), payload=())
            return []

        fallback: list[tuple[str, Path]] = []

        def fake_pair(name, output_dir, **kwargs):
            fallback.append((name, Path(output_dir)))
            meta = output_dir / "base" / "meta" / "Anim" / f"{name}.ani"
            payload = output_dir / "base" / "payload" / "Anim" / "host.ani"
            meta.parent.mkdir(parents=True, exist_ok=True)
            payload.parent.mkdir(parents=True, exist_ok=True)
            meta.touch()
            payload.touch()
            return meta, payload, True

        with (
            patch.object(rd, "extract", side_effect=fake_extract),
            patch.object(rd, "extract_anim_pair", side_effect=fake_pair),
        ):
            result = rd.extract_anim_pair_batch(
                ["shared"], out, d4data_path=tmp_path / "d4data",
            )

        assert len(fallback) == 1
        name, retry_dir = fallback[0]
        assert name == "shared"
        # The retry gets an isolated subdir, never the shared root.
        assert retry_dir != out
        assert out in retry_dir.parents
        meta, payload, is_shared = result["shared"]
        assert is_shared is True
        assert payload.name == "host.ani"

    def test_bulk_miss_uses_batched_alias_resolution(self, tmp_path: Path):
        """Shared-payload misses resolve via the mapping in ONE extra
        bulk call — not one extract_anim_pair invocation per name."""
        from d4extract.casc.payload_resolver import clear_cache

        rd = _make_rd(tmp_path)
        out = tmp_path / "out"

        # d4data tree with a shared-payload mapping: c/d/e are aliased
        # to real payloads under different names; a/b own same-name
        # payloads and land cleanly in the first bulk pass.
        d4data = tmp_path / "d4data"
        mapping_dir = d4data / "base"
        mapping_dir.mkdir(parents=True)
        (mapping_dir / "CoreTOCSharedPayloadsMapping.dat.json").write_text(
            json.dumps({
                "base/payload/Anim/c.ani": "base/payload/Anim/realC.ani",
                "base/payload/Anim/d.ani": "base/payload/Anim/realD.ani",
                "base/payload/Anim/e.ani": "base/payload/Anim/realE.ani",
            }),
            encoding="utf-8",
        )
        clear_cache()  # drop any mapping cached by a prior test

        patterns: list[str] = []

        def fake_extract(pats, output_dir, **kwargs):
            pattern = pats[0]
            patterns.append(pattern)
            output_dir = Path(output_dir)
            if "{meta,payload}" in pattern:
                # First bulk pass: every meta lands, only a/b payloads.
                self._land(
                    output_dir,
                    meta=("a", "b", "c", "d", "e"),
                    payload=("a", "b"),
                )
            else:
                # Second bulk pass: the aliased real-name payloads.
                self._land(
                    output_dir, payload=("realC", "realD", "realE"),
                )
            return []

        fallback_calls: list[str] = []

        def fake_pair(name, output_dir, **kwargs):
            fallback_calls.append(name)
            raise CASCExtractionError("per-name retry must not be reached")

        with (
            patch.object(rd, "extract", side_effect=fake_extract),
            patch.object(rd, "extract_anim_pair", side_effect=fake_pair),
        ):
            result = rd.extract_anim_pair_batch(
                ["a", "b", "c", "d", "e"], out, d4data_path=d4data,
            )

        # Exactly two rustydemon invocations: the meta+payload bulk pass
        # and one batched payload-only pass for the aliased misses.
        assert len(patterns) == 2
        assert "{meta,payload}" in patterns[0]
        assert patterns[1].startswith("base/payload/Anim/")
        assert "{meta,payload}" not in patterns[1]
        # No per-name fallback — every miss resolved via the batch.
        assert fallback_calls == []
        # All five animations present.
        assert set(result) == {"a", "b", "c", "d", "e"}
        # Same-name hits are not shared; aliased ones are.
        assert result["a"][2] is False
        assert result["b"][2] is False
        for n in ("c", "d", "e"):
            meta, payload, is_shared = result[n]
            assert is_shared is True, n
            # Payload materialised at the animation's own name so the
            # downstream cache check (keyed on anim name) finds it.
            assert payload.name == f"{n}.ani"
            assert payload.is_file()
            assert meta.name == f"{n}.ani" and "meta" in meta.parts

    def test_unresolvable_anim_omitted_from_result(self, tmp_path: Path):
        """A name found nowhere is dropped, not raised."""
        rd = _make_rd(tmp_path)
        out = tmp_path / "out"

        with (
            patch.object(rd, "extract", side_effect=lambda *a, **k: []),
            patch.object(
                rd, "extract_anim_pair",
                side_effect=CASCExtractionError("no .ani"),
            ),
        ):
            result = rd.extract_anim_pair_batch(["ghost"], out)

        assert result == {}

    def test_glob_unsafe_name_routed_to_fallback(self, tmp_path: Path):
        """A glob-metacharacter name skips the brace pass entirely."""
        rd = _make_rd(tmp_path)
        out = tmp_path / "out"
        patterns: list[str] = []
        seen: list[str] = []

        def fake_extract(pats, output_dir, **kwargs):
            patterns.append(pats[0])
            return []

        def fake_pair(name, output_dir, **kwargs):
            seen.append(name)
            raise CASCExtractionError("unsupported")

        with (
            patch.object(rd, "extract", side_effect=fake_extract),
            patch.object(rd, "extract_anim_pair", side_effect=fake_pair),
        ):
            rd.extract_anim_pair_batch(["bad{name}"], out)

        assert patterns == []
        assert seen == ["bad{name}"]


# ---------------------------------------------------------------
# RustyDemonCLI.extract_anim_pair
# ---------------------------------------------------------------


class TestExtractAnimPair:
    @staticmethod
    def _land(output_dir: Path, *, meta=(), payload=()) -> None:
        """Create fake extracted .ani files under ``output_dir``.

        rustydemon mirrors the CASC tree, so meta and payload land flat
        under ``base/meta/Anim`` and ``base/payload/Anim`` — both with
        the ``.ani`` extension.
        """
        meta_d = output_dir / "base" / "meta" / "Anim"
        payload_d = output_dir / "base" / "payload" / "Anim"
        meta_d.mkdir(parents=True, exist_ok=True)
        payload_d.mkdir(parents=True, exist_ok=True)
        for n in meta:
            (meta_d / f"{n}.ani").touch()
        for n in payload:
            (payload_d / f"{n}.ani").touch()

    def test_same_dir_returns_name_matched_files(self, tmp_path: Path):
        """Multi-call same-dir use must return name-matched pairs, not the
        alphabetically-first directory-walk result.

        Regression: discovered during flCompression=5 research. Six anims
        extracted to one shared dir all returned the same (bogus) first
        pair — ``extract()`` snapshots the whole output_dir, so the old
        ``meta_files[0]``/``payload_files[0]`` resolution picked whatever
        sorted first instead of the requested animation.
        """
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "shared"

        # Simulate three prior extractions already populating the dir.
        self._land(
            output_dir,
            meta=("aaa_first", "mmm_middle", "zzz_last"),
            payload=("aaa_first", "mmm_middle", "zzz_last"),
        )

        # extract() is a no-op — the files are already on disk, as they
        # would be after a warm-cache rustydemon run.
        with patch.object(rd, "extract", side_effect=lambda *a, **k: []):
            meta, payload, is_shared = rd.extract_anim_pair(
                "mmm_middle", output_dir, d4data_path=None,
            )

        meta_d = output_dir / "base" / "meta" / "Anim"
        payload_d = output_dir / "base" / "payload" / "Anim"
        assert meta == meta_d / "mmm_middle.ani"
        assert payload == payload_d / "mmm_middle.ani"
        assert is_shared is False
        # The alphabetically-first pair must NOT be what got returned.
        assert "aaa_first" not in str(meta)
        assert "aaa_first" not in str(payload)

    def test_same_dir_shared_payload_resolves_alias(self, tmp_path: Path):
        """A shared-payload animation still resolves to the correct alias
        — and reports is_shared=True — even in a reused output dir.

        The requested anim's payload is absent under its own name; the
        real payload lives under the aliased host stem. An unrelated pair
        shares the dir to prove resolution stays name-matched.
        """
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "shared"

        self._land(
            output_dir,
            meta=("aaa_other", "warM_shared_anim"),
            payload=("aaa_other", "host_real_payload"),
        )

        with (
            patch.object(rd, "extract", side_effect=lambda *a, **k: []),
            patch(
                "d4extract.casc.payload_resolver.resolve_payload_path",
                return_value="base/payload/Anim/host_real_payload.ani",
            ),
        ):
            meta, payload, is_shared = rd.extract_anim_pair(
                "warM_shared_anim", output_dir,
                d4data_path=tmp_path / "d4data",
            )

        meta_d = output_dir / "base" / "meta" / "Anim"
        payload_d = output_dir / "base" / "payload" / "Anim"
        assert meta == meta_d / "warM_shared_anim.ani"
        assert payload == payload_d / "host_real_payload.ani"
        assert is_shared is True

    def test_fresh_dir_single_call_still_works(self, tmp_path: Path):
        """The successful single-call-fresh-dir path is unaffected."""
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "fresh"

        def fake_extract(pats, output_dir, **kwargs):
            self._land(
                Path(output_dir), meta=("solo_anim",), payload=("solo_anim",),
            )
            return []

        with patch.object(rd, "extract", side_effect=fake_extract):
            meta, payload, is_shared = rd.extract_anim_pair(
                "solo_anim", output_dir,
            )

        assert meta.name == "solo_anim.ani" and "meta" in meta.parts
        assert payload.name == "solo_anim.ani" and "payload" in payload.parts
        assert is_shared is False

    def test_missing_payload_raises(self, tmp_path: Path):
        """A name-matched meta with no payload (and no d4data) raises."""
        rd = _make_rd(tmp_path)
        output_dir = tmp_path / "shared"
        self._land(output_dir, meta=("lonely", "other"), payload=("other",))

        with patch.object(rd, "extract", side_effect=lambda *a, **k: []):
            with pytest.raises(CASCExtractionError, match="No payload"):
                rd.extract_anim_pair("lonely", output_dir, d4data_path=None)


# ---------------------------------------------------------------
# RustyDemonCLI.find_tact_keys — precedence
# ---------------------------------------------------------------


class TestFindTactKeys:
    """The autouse fixture above opts this class out so we can drive
    the real ``find_tact_keys`` against explicit mocks of the
    config-module lookup and ``D4EXTRACT_TACT_KEYS``."""

    def _patch_config(self, monkeypatch, return_value):
        """Replace ``d4extract.config.get_tact_keys_path`` for one test.

        The real function imports ``PySide6.QtCore`` and reads
        QSettings; both are sandboxed by replacing the function with a
        stub that needs neither.
        """
        import d4extract.config as cfg

        monkeypatch.setattr(cfg, "get_tact_keys_path", lambda: return_value)

    def test_qsettings_wins_over_env_var(self, monkeypatch, tmp_path: Path):
        env_file = tmp_path / "env_keys.txt"
        env_file.touch()
        cached_file = tmp_path / "cached_keys.txt"
        cached_file.touch()
        self._patch_config(monkeypatch, cached_file)
        monkeypatch.setenv("D4EXTRACT_TACT_KEYS", str(env_file))

        result = RustyDemonCLI.find_tact_keys()
        assert result == cached_file

    def test_falls_back_to_env_var(self, monkeypatch, tmp_path: Path):
        env_file = tmp_path / "env_keys.txt"
        env_file.touch()
        self._patch_config(monkeypatch, None)
        monkeypatch.setenv("D4EXTRACT_TACT_KEYS", str(env_file))

        result = RustyDemonCLI.find_tact_keys()
        # The function returns a resolved path; compare resolved forms.
        assert result == env_file.resolve()

    def test_returns_none_when_nothing_configured(self, monkeypatch):
        self._patch_config(monkeypatch, None)
        monkeypatch.delenv("D4EXTRACT_TACT_KEYS", raising=False)

        assert RustyDemonCLI.find_tact_keys() is None

    def test_env_var_pointing_at_missing_file_returns_none(
        self, monkeypatch, tmp_path: Path,
    ):
        self._patch_config(monkeypatch, None)
        monkeypatch.setenv(
            "D4EXTRACT_TACT_KEYS", str(tmp_path / "no_such_file.txt"),
        )

        assert RustyDemonCLI.find_tact_keys() is None

    def test_qsettings_pointing_at_missing_file_falls_through(
        self, monkeypatch, tmp_path: Path,
    ):
        """When the cached path is gone, ``get_tact_keys_path`` already
        returns None — so the env var fallback should still fire."""
        env_file = tmp_path / "env_keys.txt"
        env_file.touch()
        self._patch_config(monkeypatch, None)  # mimics file-no-longer-exists
        monkeypatch.setenv("D4EXTRACT_TACT_KEYS", str(env_file))

        assert RustyDemonCLI.find_tact_keys() == env_file.resolve()

    def test_qsettings_lookup_import_error_does_not_crash(
        self, monkeypatch, tmp_path: Path,
    ):
        """A broken PySide6 install must not take the CLI down with it.

        Headless / CI environments often lack PySide6 entirely; the
        env-var fallback has to keep working in that case.
        """
        env_file = tmp_path / "env_keys.txt"
        env_file.touch()

        import d4extract.config as cfg

        def _raise():
            raise ImportError("PySide6 not available")

        monkeypatch.setattr(cfg, "get_tact_keys_path", _raise)
        monkeypatch.setenv("D4EXTRACT_TACT_KEYS", str(env_file))

        assert RustyDemonCLI.find_tact_keys() == env_file.resolve()
