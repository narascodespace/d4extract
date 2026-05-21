"""Validate and normalise a user-selected d4data path.

The d4data community metadata repo (https://github.com/blizzhackers/d4data)
is no longer fetched by d4extract — users obtain it themselves (``git
clone`` or the GitHub ZIP) and point the app at the folder via the
``D4DataCard`` or ``File → Set d4data Folder…``. This module is the
thin layer that:

* Recognises whether a chosen directory actually is a d4data checkout —
  :func:`is_d4data_dir` accepts either the **top-level repo** (has
  ``json/base/`` inside) or the **``json/`` subdirectory** directly.
* Normalises either form to the ``json/`` subpath the rest of the
  codebase consumes (``payload_resolver`` and friends look for
  ``base/`` as a direct child of the stored path) —
  :func:`resolve_d4data_json_path`.

Both helpers are headless (no Qt imports) so they can be reused from
GUI code paths and unit tests interchangeably.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


# Path that uniquely identifies a d4data checkout. Inside the repo this
# is ``json/base/`` (the parsed-JSON tree the codebase consumes). When
# the user points at the ``json/`` subdir directly, it shows up as
# ``base/`` instead. ``is_d4data_dir`` accepts either.
_SIGNATURE_TOP = Path("json") / "base"
_SIGNATURE_JSON_SUBDIR = Path("base")


class D4DataInstallError(RuntimeError):
    """Raised when a d4data path is rejected as invalid."""


def is_d4data_dir(path: Path) -> bool:
    """Heuristic check that ``path`` looks like a d4data location.

    Accepts either form the rest of the codebase encounters:

    * The **top-level d4data repo** (has ``json/base/`` inside it).
    * The **``json/`` subdirectory** of that repo (has ``base/`` directly).

    Callers that need the form the rest of the code consumes should pass
    the result through :func:`resolve_d4data_json_path`.
    """
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    if (path / _SIGNATURE_TOP).is_dir():
        return True
    if (path / _SIGNATURE_JSON_SUBDIR).is_dir():
        return True
    return False


def resolve_d4data_json_path(path: Path) -> Path:
    """Normalise ``path`` to the ``json/`` subdirectory the codebase consumes.

    ``payload_resolver`` and friends expect the path to be ``d4data/json/``
    (where ``base/`` lives). If the caller passes the top-level repo,
    we append ``json/`` for them.

    Raises :class:`D4DataInstallError` if ``path`` is not a valid d4data
    directory in either form.
    """
    if (path / _SIGNATURE_TOP).is_dir():
        return path / "json"
    if (path / _SIGNATURE_JSON_SUBDIR).is_dir():
        return path
    raise D4DataInstallError(
        f"{path} does not look like a d4data directory (no base/ or "
        "json/base/ found)"
    )
