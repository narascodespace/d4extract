"""Tests for d4extract.config (user-supplied TACT keys).

The suite stays Qt-free: QSettings is intercepted by replacing
``sys.modules['PySide6.QtCore']`` with a fake namespace that exposes
the small surface ``d4extract.config`` actually uses (``QSettings``
with ``value``/``setValue``/``remove``/``sync``). See
``feedback_qt_free_tests`` for why we avoid importing real Qt in
tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------
# In-memory QSettings stand-in (no Qt required)
# ---------------------------------------------------------------


class _FakeQSettings:
    """Process-wide in-memory store, keyed by (organisation, application).

    Only the methods ``d4extract.config`` calls are implemented:
    ``value`` (with optional default + type coercion for ``bool``),
    ``setValue``, ``remove``, ``sync``. State is reset between tests
    by the autouse fixture below.
    """

    _stores: dict[tuple[str, str], dict[str, object]] = {}

    def __init__(self, org: str, app: str) -> None:
        self._key = (org, app)
        self._stores.setdefault(self._key, {})

    def value(self, key: str, default=None, type=None):
        v = self._stores[self._key].get(key, default)
        if type is bool:
            if isinstance(v, str):
                return v.lower() in ("true", "1", "yes", "on")
            return bool(v)
        return v

    def setValue(self, key: str, value: object) -> None:
        self._stores[self._key][key] = value

    def remove(self, key: str) -> None:
        self._stores[self._key].pop(key, None)

    def sync(self) -> None:  # noqa: D401 — mirrors the QSettings API
        pass

    @classmethod
    def reset(cls) -> None:
        cls._stores.clear()


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path: Path):
    """Sandbox the per-test environment.

    - ``platformdirs.user_data_dir`` is redirected to ``tmp_path`` so
      the cached TACT keys file never touches the developer's real
      app-data directory.
    - ``sys.modules['PySide6.QtCore']`` is replaced with a stub that
      yields :class:`_FakeQSettings` from ``from PySide6.QtCore import
      QSettings``. Each test starts with a clean store.
    """
    import platformdirs

    user_dir = tmp_path / "appdata"
    monkeypatch.setattr(
        platformdirs, "user_data_dir",
        lambda appname, appauthor=None: str(user_dir),
    )

    _FakeQSettings.reset()
    monkeypatch.setitem(
        sys.modules, "PySide6.QtCore",
        SimpleNamespace(QSettings=_FakeQSettings),
    )
    yield


# ---------------------------------------------------------------
# count_valid_keys
# ---------------------------------------------------------------


class TestCountValidKeys:
    def test_empty_file(self, tmp_path: Path):
        from d4extract.config import count_valid_keys

        empty = tmp_path / "empty.txt"
        empty.write_text("", encoding="utf-8")
        assert count_valid_keys(empty) == 0

    def test_space_and_semicolon_separators_both_accepted(self, tmp_path: Path):
        from d4extract.config import count_valid_keys

        f = tmp_path / "mixed.txt"
        f.write_text(
            "FB680CB6A8BF81F3 62D90EFA7F36D71C398AE2F1FE37BDB9\n"
            "402CD9D8D6BFED98;AEB0EADEA47612FE6C041A03958DF241\n",
            encoding="utf-8",
        )
        assert count_valid_keys(f) == 2

    def test_skips_comments_and_blanks(self, tmp_path: Path):
        from d4extract.config import count_valid_keys

        f = tmp_path / "annotated.txt"
        f.write_text(
            "# header comment\n"
            "\n"
            "FB680CB6A8BF81F3 62D90EFA7F36D71C398AE2F1FE37BDB9\n"
            "   \n"
            "# trailing comment\n",
            encoding="utf-8",
        )
        assert count_valid_keys(f) == 1

    def test_rejects_wrong_length_hex(self, tmp_path: Path):
        from d4extract.config import count_valid_keys

        f = tmp_path / "malformed.txt"
        # 14 hex KEYID instead of 16, and a 30-hex KEY — both invalid.
        f.write_text(
            "FB680CB6A8BF81 62D90EFA7F36D71C398AE2F1FE37BDB9\n"
            "402CD9D8D6BFED98 AEB0EADEA47612FE6C041A03958DF\n",
            encoding="utf-8",
        )
        assert count_valid_keys(f) == 0

    def test_rejects_non_hex_chars(self, tmp_path: Path):
        from d4extract.config import count_valid_keys

        f = tmp_path / "nothex.txt"
        # Has a 'G' (non-hex) in the KEYID.
        f.write_text(
            "FB680CB6A8BF81FG 62D90EFA7F36D71C398AE2F1FE37BDB9\n",
            encoding="utf-8",
        )
        assert count_valid_keys(f) == 0

    def test_nonexistent_file_returns_zero(self, tmp_path: Path):
        from d4extract.config import count_valid_keys

        assert count_valid_keys(tmp_path / "missing.txt") == 0


# ---------------------------------------------------------------
# set_tact_keys_path
# ---------------------------------------------------------------


class TestSetTactKeysPath:
    def test_valid_file_is_copied_and_path_persisted(self, tmp_path: Path):
        from d4extract.config import (
            get_tact_keys_path, set_tact_keys_path,
        )

        src = tmp_path / "user_keys.txt"
        src.write_text(
            "FB680CB6A8BF81F3 62D90EFA7F36D71C398AE2F1FE37BDB9\n",
            encoding="utf-8",
        )

        cached = set_tact_keys_path(src)
        assert cached.is_file()
        # Cached path lives under the sandboxed user data dir.
        assert (tmp_path / "appdata") in cached.parents
        # Source still untouched.
        assert src.is_file()
        # Subsequent get returns the cached path.
        assert get_tact_keys_path() == cached

    def test_zero_valid_keys_raises_value_error(self, tmp_path: Path):
        from d4extract.config import set_tact_keys_path

        src = tmp_path / "bad.txt"
        # A random PDF-ish payload that ``count_valid_keys`` rejects.
        src.write_text("%PDF-1.4 not a key file\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no valid TACT key"):
            set_tact_keys_path(src)

    def test_missing_source_file_raises(self, tmp_path: Path):
        from d4extract.config import set_tact_keys_path

        with pytest.raises(ValueError, match="not a file"):
            set_tact_keys_path(tmp_path / "ghost.txt")

    def test_rejected_file_does_not_overwrite_existing_cache(
        self, tmp_path: Path,
    ):
        """Validation gates the copy — a bad second file mustn't trash
        the previously-loaded good one."""
        from d4extract.config import set_tact_keys_path

        good = tmp_path / "good.txt"
        good.write_text(
            "FB680CB6A8BF81F3 62D90EFA7F36D71C398AE2F1FE37BDB9\n",
            encoding="utf-8",
        )
        first = set_tact_keys_path(good)
        first_contents = first.read_text(encoding="utf-8")

        bad = tmp_path / "bad.txt"
        bad.write_text("not keys\n", encoding="utf-8")
        with pytest.raises(ValueError):
            set_tact_keys_path(bad)

        assert first.read_text(encoding="utf-8") == first_contents


# ---------------------------------------------------------------
# get_tact_keys_path / clear_tact_keys
# ---------------------------------------------------------------


class TestGetClearTactKeys:
    def test_get_returns_none_when_unset(self):
        from d4extract.config import get_tact_keys_path

        assert get_tact_keys_path() is None

    def test_get_returns_none_when_cached_file_deleted(self, tmp_path: Path):
        from d4extract.config import get_tact_keys_path, set_tact_keys_path

        src = tmp_path / "user_keys.txt"
        src.write_text(
            "FB680CB6A8BF81F3 62D90EFA7F36D71C398AE2F1FE37BDB9\n",
            encoding="utf-8",
        )
        cached = set_tact_keys_path(src)
        cached.unlink()
        # QSettings still holds the path but the file is gone; the
        # accessor must treat that as "not loaded".
        assert get_tact_keys_path() is None

    def test_clear_removes_cached_file_and_qsettings_entry(
        self, tmp_path: Path,
    ):
        from d4extract.config import (
            clear_tact_keys, get_tact_keys_path, set_tact_keys_path,
        )

        src = tmp_path / "user_keys.txt"
        src.write_text(
            "FB680CB6A8BF81F3 62D90EFA7F36D71C398AE2F1FE37BDB9\n",
            encoding="utf-8",
        )
        cached = set_tact_keys_path(src)
        assert cached.is_file()

        clear_tact_keys()
        assert not cached.exists()
        assert get_tact_keys_path() is None

    def test_clear_is_idempotent(self):
        from d4extract.config import clear_tact_keys

        # Two clears in a row must not raise.
        clear_tact_keys()
        clear_tact_keys()
