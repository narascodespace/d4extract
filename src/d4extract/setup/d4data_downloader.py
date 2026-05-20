"""Fetch and install the d4data community metadata repo.

The d4data repo (https://github.com/blizzhackers/d4data) is updated by
the community on every Diablo IV patch. For the packaged .exe build we
do not want users to install git or hunt down the repo manually, so we
fetch the latest snapshot as a GitHub zipball and unpack it under
``%LOCALAPPDATA%\\d4extract\\d4data\\``.

Design notes:

* Headless — no Qt imports. The GUI wraps this with a QThread that
  forwards progress callbacks to a QProgressBar.
* Atomic — extraction goes to a staging directory and is swapped into
  place only on success. A partial extract never replaces a working
  install.
* Resumable enough — on Ctrl+C or network errors the staging directory
  is cleaned up and the existing install is left intact.
* Verifiable — the unpacked tree is sanity-checked against
  :func:`is_d4data_dir` before being promoted.

The default source is the ``master`` branch zipball, but the
:class:`D4DataSource` dataclass lets callers point at a fork, a specific
tag, or a local zip for testing.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)


# Path that uniquely identifies a d4data checkout. Inside the repo this
# is ``json/base/`` (the parsed-JSON tree the codebase consumes). When
# the user points at the ``json/`` subdir directly, it shows up as
# ``base/`` instead. ``is_d4data_dir`` accepts either.
_SIGNATURE_TOP = Path("json") / "base"
_SIGNATURE_JSON_SUBDIR = Path("base")


# Public canonical source. Pinning to ``master`` rather than a tag means
# users get the freshest metadata on every redownload. If we ever need
# reproducibility we can switch to a tagged release.
_DEFAULT_OWNER = "blizzhackers"
_DEFAULT_REPO = "d4data"
_DEFAULT_REF = "master"


# Progress callback signature:
#   ``cb(bytes_done, bytes_total_or_None, stage_label)``
# ``bytes_total`` is ``None`` when the server does not send a
# Content-Length header. ``stage_label`` is a short human-readable label
# like ``"downloading"`` or ``"extracting"``.
ProgressCallback = Callable[[int, Optional[int], str], None]


class D4DataInstallError(RuntimeError):
    """Raised when a d4data install fails for any reason."""


@dataclass(frozen=True)
class D4DataSource:
    """Where to fetch a d4data archive from.

    The default points at the canonical community repo on GitHub. Tests
    pass a ``file://`` URL pointing at a fixture zip.
    """

    owner: str = _DEFAULT_OWNER
    repo: str = _DEFAULT_REPO
    ref: str = _DEFAULT_REF
    explicit_url: str | None = None

    @property
    def url(self) -> str:
        if self.explicit_url is not None:
            return self.explicit_url
        # GitHub serves a redirectable zipball at this stable URL.
        return (
            f"https://github.com/{self.owner}/{self.repo}"
            f"/archive/refs/heads/{self.ref}.zip"
        )

    @property
    def display_name(self) -> str:
        return f"{self.owner}/{self.repo}@{self.ref}"


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


def default_d4data_dir() -> Path:
    """Return the canonical install directory for the managed copy.

    On Windows this is ``%LOCALAPPDATA%\\d4extract\\d4data``. On other
    platforms we fall back to ``~/.local/share/d4extract/d4data`` so the
    headless module stays testable on Linux/macOS CI.
    """
    if os.name == "nt":
        base_str = os.environ.get("LOCALAPPDATA")
        if base_str:
            base = Path(base_str)
        else:
            base = Path.home() / "AppData" / "Local"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "d4extract" / "d4data"


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


# ---------------------------------------------------------------------------
# Download + extract
# ---------------------------------------------------------------------------


_DOWNLOAD_CHUNK = 1 << 16  # 64 KiB — large enough to amortize syscalls,
# small enough that progress updates feel live.


def _download_to_file(
    url: str,
    dest: Path,
    progress: ProgressCallback | None,
) -> None:
    """Stream ``url`` to ``dest``, calling ``progress`` as bytes accumulate."""
    log.info("downloading %s", url)
    try:
        # Set a user-agent so GitHub does not 403 us on some networks.
        req = urllib.request.Request(
            url, headers={"User-Agent": "d4extract-setup/0.1"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            total_header = resp.headers.get("Content-Length")
            total: int | None
            try:
                total = int(total_header) if total_header else None
            except ValueError:
                total = None

            done = 0
            with dest.open("wb") as out:
                while True:
                    chunk = resp.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total, "downloading")
    except urllib.error.URLError as exc:
        raise D4DataInstallError(
            f"network error while downloading {url}: {exc.reason}"
        ) from exc
    except OSError as exc:
        raise D4DataInstallError(
            f"could not write download to {dest}: {exc}"
        ) from exc


def _extract_zip(
    zip_path: Path,
    staging_dir: Path,
    progress: ProgressCallback | None,
) -> Path:
    """Extract ``zip_path`` into ``staging_dir`` and return the root path.

    GitHub zipballs wrap the repo in a top-level folder named
    ``<repo>-<ref>/``. We return the path to that inner directory so the
    caller can promote it directly — we never want the user's d4data dir
    to itself contain a stray ``d4data-master/`` folder.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.infolist()
            if not members:
                raise D4DataInstallError("downloaded archive is empty")

            total_bytes = sum(m.file_size for m in members) or None
            done_bytes = 0

            staging_dir.mkdir(parents=True, exist_ok=True)
            for member in members:
                zf.extract(member, staging_dir)
                done_bytes += member.file_size
                if progress is not None:
                    progress(done_bytes, total_bytes, "extracting")

            # Discover the wrapper directory. GitHub guarantees a single
            # top-level entry, but we tolerate edge cases by picking the
            # first directory that looks like d4data.
            top_level: set[str] = set()
            for member in members:
                head = member.filename.split("/", 1)[0]
                if head:
                    top_level.add(head)

            for candidate_name in top_level:
                candidate = staging_dir / candidate_name
                if is_d4data_dir(candidate):
                    return candidate

            # Fall back to the first top-level dir even if the heuristic
            # check failed — let the caller surface a meaningful error.
            if len(top_level) == 1:
                only = next(iter(top_level))
                return staging_dir / only

            raise D4DataInstallError(
                "could not identify the d4data root inside the archive "
                f"(top-level entries: {sorted(top_level)})"
            )
    except zipfile.BadZipFile as exc:
        raise D4DataInstallError(
            f"downloaded file is not a valid zip archive: {exc}"
        ) from exc


def _swap_install(extracted_root: Path, target_dir: Path) -> None:
    """Atomically replace ``target_dir`` with ``extracted_root``.

    Strategy:

    1. If ``target_dir`` exists, rename it to a ``.old`` sibling first.
    2. Move ``extracted_root`` into place at ``target_dir``.
    3. Best-effort delete the ``.old`` sibling.

    If step 2 fails we restore the ``.old`` sibling so the user is not
    left with no d4data at all.
    """
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if target_dir.exists():
        backup = target_dir.with_suffix(target_dir.suffix + ".old")
        # Clear any leftover backup from a previous failed run.
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        os.replace(target_dir, backup)

    try:
        os.replace(extracted_root, target_dir)
    except OSError as exc:
        # Roll back so the user is not left without d4data.
        if backup is not None and backup.exists():
            try:
                os.replace(backup, target_dir)
            except OSError:
                log.exception("failed to restore d4data backup at %s", backup)
        raise D4DataInstallError(
            f"could not move extracted d4data into place: {exc}"
        ) from exc

    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def download_and_install_d4data(
    target_dir: Path | None = None,
    source: D4DataSource | None = None,
    progress: ProgressCallback | None = None,
) -> Path:
    """Download d4data from ``source`` and install it at ``target_dir``.

    Returns the ``json/`` subpath that the rest of the codebase
    consumes — callers should persist this value directly via
    ``AppSettings.set_d4data_path`` rather than ``target_dir``, since
    ``payload_resolver`` (and friends) expect to find ``base/`` as a
    direct child of the stored path. Raises :class:`D4DataInstallError`
    on any failure. Cleanup of the staging directory is guaranteed
    regardless of outcome.
    """
    target = target_dir or default_d4data_dir()
    src = source or D4DataSource()

    log.info("installing d4data from %s into %s", src.display_name, target)

    # We do everything inside a single temp dir so that any failure
    # leaves no partial state on disk.
    with tempfile.TemporaryDirectory(prefix="d4extract-d4data-") as tmp_str:
        tmp = Path(tmp_str)
        zip_path = tmp / "d4data.zip"
        staging = tmp / "staging"

        _download_to_file(src.url, zip_path, progress)
        extracted_root = _extract_zip(zip_path, staging, progress)

        if not is_d4data_dir(extracted_root):
            raise D4DataInstallError(
                "extracted archive does not look like a valid d4data "
                "checkout (no json/base/ found)"
            )

        _swap_install(extracted_root, target)

    # Return the ``json/`` subpath because that is what the rest of the
    # codebase consumes (see payload_resolver._MAPPING_RELPATH).
    json_path = resolve_d4data_json_path(target)
    log.info("d4data installed at %s (json subpath: %s)", target, json_path)
    return json_path
