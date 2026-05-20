"""Tests for the Model Browser's composed-row plumbing.

``ModelListWidget`` is a ``QWidget``; instantiating it would need a
``QApplication``. The plumbing under test (row composition, the SNO /
label split, the category proxy's filter rule, favorites keyed on SNO
paths, click → SNO emit) only touches plain attributes, so the widget
is built via ``__new__`` with stubbed model/proxy/delegate to keep the
suite QApplication-free — same pattern :mod:`test_builder_export` uses.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from d4extract.gui.widgets.model_list import (
    _ROW_SEP,
    ModelListWidget,
    _CategoryProxyModel,
    _row_label,
    _row_sno,
    _stem_no_ext,
)


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────────────


def test_row_helpers_split_on_separator() -> None:
    row = f"Foo Pretty\x1fbase/meta/Appearance/foo.app"
    assert _row_label(row) == "Foo Pretty"
    assert _row_sno(row) == "base/meta/Appearance/foo.app"


def test_row_sno_tolerates_bare_path() -> None:
    """A row without the separator (transitional state) maps to itself."""
    assert _row_sno("base/meta/Appearance/foo.app") == "base/meta/Appearance/foo.app"


def test_stem_no_ext_strips_directory_and_extension() -> None:
    assert _stem_no_ext("base/meta/Appearance/foo.app") == "foo"
    assert _stem_no_ext("FOO.APP") == "FOO"
    assert _stem_no_ext("no_extension") == "no_extension"


# ──────────────────────────────────────────────────────────────────────────────
# Category proxy: filter inspects only the SNO path half
# ──────────────────────────────────────────────────────────────────────────────


class _FakeStringListModel:
    """Minimal stand-in for ``QStringListModel`` used by the proxy.

    Only ``index`` + ``data`` are needed; ``filterAcceptsRow`` calls
    both, then nothing else.
    """

    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def index(self, row: int, _col: int, _parent) -> SimpleNamespace:
        return SimpleNamespace(_row=row)

    def data(self, idx, _role) -> str | None:
        try:
            return self._rows[idx._row]
        except (AttributeError, IndexError):
            return None


def _proxy_accepts(rows: list[str], pattern: str | None) -> list[bool]:
    """Drive the proxy's filterAcceptsRow against each row."""
    proxy = _CategoryProxyModel.__new__(_CategoryProxyModel)
    # __init__ would need a Qt parent context; skip it and set the
    # regex via the public setter, which is plain Python.
    proxy._category_regex = None
    if pattern is not None:
        import re as _re
        proxy._category_regex = _re.compile(pattern, _re.IGNORECASE)
    src = _FakeStringListModel(rows)
    proxy.sourceModel = lambda: src  # type: ignore[method-assign]
    return [proxy.filterAcceptsRow(i, None) for i in range(len(rows))]


def test_category_regex_matches_sno_path_only() -> None:
    """A row whose *label* contains the regex token must not be accepted.

    Concretely: a piece named "monstrous helmet" must not appear under
    the MON pill if its SNO path is in ``item_``. The category axis is
    SNO-path-only.
    """
    rows = [
        f"Monstrous Helmet{_ROW_SEP}base/meta/Appearance/Helm_Unique_X.app",
        f"Foe{_ROW_SEP}base/meta/monster_Goatman.app",
    ]
    accepted = _proxy_accepts(rows, r"monster_")
    # The "Monstrous Helmet" row is rejected — its label has "monster"
    # in it but the SNO path doesn't.
    assert accepted == [False, True]


def test_category_regex_none_accepts_everything() -> None:
    rows = [f"x{_ROW_SEP}a", f"y{_ROW_SEP}b"]
    assert _proxy_accepts(rows, None) == [True, True]


# ──────────────────────────────────────────────────────────────────────────────
# Widget plumbing via __new__
# ──────────────────────────────────────────────────────────────────────────────


def _widget(
    entries: list[str],
    resolver: dict[str, str] | None = None,
    favorites: set[str] | None = None,
) -> ModelListWidget:
    """Build a ``ModelListWidget`` via ``__new__`` with stubbed collaborators.

    ``resolver`` is an optional ``sno_path → label`` dict; ``None``
    leaves the widget without a resolver (rows fall back to stems).
    ``favorites`` is the set the widget should own.
    """
    w = ModelListWidget.__new__(ModelListWidget)
    w._settings = None
    w._favorites = set(favorites or ())
    w._all_entries = list(entries)
    w._category_filtered = list(entries)
    w._category_pattern = None
    w._favs_active = False
    w._name_resolver = resolver.get if resolver is not None else None
    w._fuzzy_cache = {}
    w._current_query = ""
    w._pending_query = ""
    w._filter_worker = None

    # MagicMock satisfies the delegate / model / proxy / list view /
    # status surfaces that the tested methods touch without requiring
    # Qt to spin up.
    w._delegate = MagicMock()
    w._source_model = MagicMock()
    w._proxy = MagicMock()
    w._proxy.rowCount.return_value = 0
    w._list_view = MagicMock()
    w._list_stack = MagicMock()
    w._overlay = MagicMock()
    w._status_label = MagicMock()
    w._filter_input = MagicMock()
    w._debounce_timer = MagicMock()

    # The Qt Signal can't be invoked on a __new__'d QWidget without a
    # backing C++ object — patch with a MagicMock so emit() is a no-op
    # the test can introspect.
    w.model_selected = MagicMock()

    return w


def test_compose_row_uses_resolver_when_present() -> None:
    w = _widget(
        entries=["base/meta/Appearance/foo.app"],
        resolver={"base/meta/Appearance/foo.app": "Foo Pretty Name"},
    )
    row = w._compose_row("base/meta/Appearance/foo.app")
    assert _row_label(row) == "Foo Pretty Name"
    assert _row_sno(row) == "base/meta/Appearance/foo.app"


def test_compose_row_falls_back_to_stem_without_resolver() -> None:
    w = _widget(entries=["base/meta/Appearance/foo.app"], resolver=None)
    row = w._compose_row("base/meta/Appearance/foo.app")
    assert _row_label(row) == "foo"
    assert _row_sno(row) == "base/meta/Appearance/foo.app"


def test_compose_row_falls_back_when_resolver_returns_empty() -> None:
    """A resolver returning ``""`` is treated as "no label available"."""
    w = _widget(
        entries=["base/meta/Appearance/foo.app"],
        resolver={"base/meta/Appearance/foo.app": ""},
    )
    row = w._compose_row("base/meta/Appearance/foo.app")
    assert _row_label(row) == "foo"


def test_compose_row_falls_back_when_resolver_raises() -> None:
    """Labelling must never break the list — a raising resolver is swallowed."""
    def _boom(_):
        raise RuntimeError("name lookup blew up")

    w = _widget(entries=["base/meta/Appearance/foo.app"])
    w._name_resolver = _boom
    row = w._compose_row("base/meta/Appearance/foo.app")
    assert _row_label(row) == "foo"


def test_on_list_clicked_emits_sno_path() -> None:
    """Selection emits the SNO path, never the composed row string."""
    w = _widget(
        entries=["base/meta/Appearance/foo.app"],
        resolver={"base/meta/Appearance/foo.app": "Foo Pretty"},
    )
    composed = w._compose_row("base/meta/Appearance/foo.app")
    proxy_index = MagicMock()
    proxy_index.isValid.return_value = True
    w._proxy.data.return_value = composed

    w._on_list_clicked(proxy_index)

    w.model_selected.emit.assert_called_once_with(
        "base/meta/Appearance/foo.app",
    )


def test_toggle_favorite_stores_sno_path() -> None:
    """``_favorites`` holds SNO paths, never composed rows."""
    w = _widget(
        entries=["base/meta/Appearance/foo.app"],
        resolver={"base/meta/Appearance/foo.app": "Foo Pretty"},
    )

    w._toggle_favorite("base/meta/Appearance/foo.app")
    assert w._favorites == {"base/meta/Appearance/foo.app"}

    # Toggling again removes it.
    w._toggle_favorite("base/meta/Appearance/foo.app")
    assert w._favorites == set()
