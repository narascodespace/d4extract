"""Wrapper around rustydemon-cli for CASC archive extraction."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from d4extract.casc.archive import CASCEntry

log = logging.getLogger(__name__)

# CREATE_NO_WINDOW suppresses the per-child console flash that Windows
# would otherwise pop up when a --windowed PyInstaller .exe spawns a
# console child (rustydemon-cli). The flag is Windows-only; the
# attribute does not exist on POSIX, so we look it up defensively via
# getattr so source runs on Linux / macOS still work for tests.
_NO_WINDOW_FLAG = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SUBPROCESS_KWARGS: dict = (
    {"creationflags": _NO_WINDOW_FLAG} if os.name == "nt" else {}
)

# Markers that identify a Diablo IV install directory.
_D4_MARKERS = ("Data", ".build.info")

# Filename characters that would be misinterpreted as glob metacharacters
# when assembling a brace-expansion pattern in :meth:`RustyDemonCLI.extract_many`.
_GLOB_META_CHARS = frozenset("{},*?[]")


def _has_glob_meta(name: str) -> bool:
    """True if ``name`` contains any character that would corrupt a glob."""
    return any(c in _GLOB_META_CHARS for c in name)


def _sanitize_dirname(name: str) -> str:
    """Rewrite glob-metacharacters so ``name`` is safe as a path segment.

    Real D4 animation names are already filesystem-safe, so this is the
    identity for every genuine input; it only ever touches the defensive
    glob-unsafe names that :func:`_has_glob_meta` flags, ensuring the
    per-name retry directory in
    :meth:`RustyDemonCLI.extract_anim_pair_batch` can still be created on
    Windows (where ``*`` and ``?`` are forbidden in filenames).
    """
    return "".join("_" if c in _GLOB_META_CHARS else c for c in name)


def _resolve_anim_file(anim_dir: Path, name: str) -> Path | None:
    """Return the ``.ani`` file in *anim_dir* whose stem matches *name*.

    rustydemon mirrors the CASC tree, so every extracted animation lands
    flat in ``base/meta/Anim`` or ``base/payload/Anim`` under its own
    stem. Resolving the result by stem — rather than taking whatever a
    directory walk happens to surface first — is what lets a single
    ``output_dir`` be reused across :meth:`RustyDemonCLI.extract_anim_pair`
    calls without one animation's files masking another's. An exact,
    case-insensitive stem match wins; a substring match is the fallback
    for the partial/glob ``name`` the public API documents.
    """
    if not anim_dir.is_dir():
        return None
    candidates = sorted(
        p for p in anim_dir.iterdir()
        if p.is_file() and p.suffix == ".ani"
    )
    target = name.lower()
    for path in candidates:
        if path.stem.lower() == target:
            return path
    for path in candidates:
        if target in path.stem.lower():
            return path
    return None


class CASCExtractionError(Exception):
    """Raised when a rustydemon-cli operation fails."""

    def __init__(self, message: str, cmd: list[str] | None = None, stderr: str = ""):
        self.cmd = cmd
        self.stderr = stderr
        super().__init__(message)


class RustyDemonCLI:
    """High-level interface to rustydemon-cli for CASC archive operations.

    Wraps the ``rustydemon-cli`` binary (v0.3.x) which provides:
    - ``export`` — extract files from a CASC archive
    - ``export --dry-run`` — list matching files without extracting
    - ``probe`` — smoke-test an archive

    Args:
        game_dir: Path to the Diablo IV installation root.
        binary: Explicit path to the rustydemon-cli binary.
            If *None*, :meth:`find_binary` is used to locate it.
        tact_keys_path: Explicit path to a TACT key file
            (semicolon- or space-separated hex format). If *None*,
            :meth:`find_tact_keys` attempts auto-discovery.
    """

    def __init__(
        self,
        game_dir: Path,
        binary: Path | None = None,
        tact_keys_path: Path | None = None,
    ) -> None:
        self.game_dir = Path(game_dir).resolve()
        self._validate_game_dir()
        self.binary = binary or self.find_binary()
        if self.binary is None:
            raise CASCExtractionError(
                "rustydemon-cli not found. Install it from "
                "https://github.com/HoldMyBeer-gg/rustydemon and ensure the "
                "binary is on your PATH, or set the D4EXTRACT_RUSTYDEMON "
                "environment variable to its location."
            )
        self.tact_keys_path = tact_keys_path or self.find_tact_keys()
        if self.tact_keys_path:
            log.info("TACT keys: %s", self.tact_keys_path)
        else:
            log.debug("No TACT key file found; encrypted content will be skipped")

    # ------------------------------------------------------------------
    # Binary discovery
    # ------------------------------------------------------------------

    @classmethod
    def find_binary(cls) -> Path | None:
        """Locate the rustydemon-cli binary.

        Search order:
        1. ``D4EXTRACT_RUSTYDEMON`` environment variable
        2. System PATH
        3. ``./rustydemon/target/release/rustydemon-cli``
        4. ``~/.cargo/bin/rustydemon-cli``

        Returns:
            Resolved :class:`Path` to the binary, or *None* if not found.
        """
        # 1. Env var override
        env_path = os.environ.get("D4EXTRACT_RUSTYDEMON")
        if env_path:
            p = Path(env_path)
            if p.is_file():
                log.debug("rustydemon-cli found via D4EXTRACT_RUSTYDEMON: %s", p)
                return p.resolve()
            log.warning(
                "D4EXTRACT_RUSTYDEMON is set to %s but that file does not exist", p
            )

        # 2. System PATH
        found = shutil.which("rustydemon-cli")
        if found:
            log.debug("rustydemon-cli found on PATH: %s", found)
            return Path(found).resolve()

        # 2.5 Next to the executable. PyInstaller-frozen builds ship
        # rustydemon-cli in a sibling ``rustydemon/`` folder (see
        # packaging/build.ps1). This needs an absolute path because the
        # user may launch the .exe from any working directory.
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            for candidate in (
                exe_dir / "rustydemon" / "rustydemon-cli",
                exe_dir / "rustydemon" / "rustydemon-cli.exe",
            ):
                if candidate.is_file():
                    log.debug(
                        "rustydemon-cli found next to executable: %s", candidate
                    )
                    return candidate.resolve()

        # 3. Local build directory
        local = Path("rustydemon/target/release/rustydemon-cli")
        # Try with .exe on Windows
        for candidate in (local, local.with_suffix(".exe")):
            if candidate.is_file():
                log.debug("rustydemon-cli found locally: %s", candidate)
                return candidate.resolve()

        # 4. Cargo bin
        cargo_bin = Path.home() / ".cargo" / "bin" / "rustydemon-cli"
        for candidate in (cargo_bin, cargo_bin.with_suffix(".exe")):
            if candidate.is_file():
                log.debug("rustydemon-cli found in cargo bin: %s", candidate)
                return candidate.resolve()

        return None

    @classmethod
    def find_tact_keys(cls) -> Path | None:
        """Locate a TACT key file for decrypting encrypted CASC content.

        Search order:
        1. User-supplied path saved via the GUI (``File → Load TACT
           Keys…``) — read from QSettings through
           :func:`d4extract.config.get_tact_keys_path`.
        2. ``D4EXTRACT_TACT_KEYS`` environment variable — kept for
           headless / CI workflows that don't run the GUI at all.

        We deliberately no longer probe for bundled key files in the
        package tree: distributing TACT keys carries DMCA risk, so the
        repo and release artifacts ship without them and users are
        expected to supply their own.

        Returns:
            Resolved :class:`Path` to the key file, or *None* if not found.
        """
        # 1. QSettings (GUI-saved). Lazy import inside the function body
        #    so CLI invocations that don't need Qt don't pay the import
        #    cost — and so a missing PySide6 install in headless
        #    environments degrades gracefully to the env var fallback.
        try:
            from d4extract.config import get_tact_keys_path

            cached = get_tact_keys_path()
        except ImportError as exc:
            log.debug(
                "QSettings TACT keys lookup unavailable (%s); "
                "falling back to env var",
                exc,
            )
            cached = None
        if cached is not None:
            log.debug("TACT keys found via QSettings: %s", cached)
            return cached

        # 2. Env var override (headless / CI).
        env_path = os.environ.get("D4EXTRACT_TACT_KEYS")
        if env_path:
            p = Path(env_path)
            if p.is_file():
                log.debug("TACT keys found via D4EXTRACT_TACT_KEYS: %s", p)
                return p.resolve()
            log.warning(
                "D4EXTRACT_TACT_KEYS is set to %s but that file does not exist", p
            )

        return None

    # ------------------------------------------------------------------
    # Archive operations
    # ------------------------------------------------------------------

    def version(self) -> str:
        """Return the rustydemon-cli version string."""
        result = self._run(["--version"])
        return result.stdout.strip()

    def list_files(self, path_filter: str = "") -> list[CASCEntry]:
        """List files in the CASC archive using ``export --dry-run``.

        Args:
            path_filter: Glob pattern to filter results (e.g.
                ``base/meta/Appearance/*.app``). If empty, lists everything
                under ``base/``.

        Returns:
            List of :class:`CASCEntry` for each matching file.
        """
        cmd = [
            "export",
            "--archive", str(self.game_dir),
            "--output", ".",  # required but ignored with --dry-run
            "--dry-run",
        ]
        if self.tact_keys_path:
            cmd.extend(["--tact-keys", str(self.tact_keys_path)])
        if path_filter:
            cmd.extend(["--path", path_filter])
        else:
            cmd.extend(["--path", "base/*"])

        result = self._run(cmd)
        return self._parse_file_listing(result.stdout)

    def extract(
        self,
        patterns: list[str],
        output_dir: Path,
        *,
        flatten: bool = False,
        workers: int | None = None,
        overwrite: bool = True,
    ) -> list[Path]:
        """Extract files from the CASC archive.

        Args:
            patterns: Path/glob patterns to extract (passed to ``--path``).
            output_dir: Destination directory for extracted files.
            flatten: If *True*, drop files directly into output_dir.
            workers: Number of parallel worker threads. *None* = CPU count.
            overwrite: If *True* (default), pass ``--overwrite`` so
                rustydemon re-decodes and rewrites files that already
                exist in ``output_dir``. Pass *False* to let it skip
                extant files — much cheaper when extracting into a warm
                cache, since the BLTE decode is then avoided entirely.

        Returns:
            List of paths to extracted files.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "export",
            "--archive", str(self.game_dir),
            "--output", str(output_dir),
        ]
        if overwrite:
            cmd.append("--overwrite")
        if self.tact_keys_path:
            cmd.extend(["--tact-keys", str(self.tact_keys_path)])
        if flatten:
            cmd.append("--flat")
        if workers is not None:
            cmd.extend(["--parallel", str(workers)])
        for pattern in patterns:
            cmd.extend(["--path", pattern])

        self._run_streaming(cmd)

        # Collect extracted files
        extracted: list[Path] = []
        for root, _dirs, files in os.walk(output_dir):
            for f in files:
                extracted.append(Path(root) / f)

        return sorted(extracted)

    def extract_many(
        self,
        paths: list[str],
        output_dir: Path,
        *,
        group_size: int = 80,
        workers: int | None = None,
        overwrite: bool = True,
    ) -> list[Path]:
        """Extract many specific files in as few rustydemon calls as possible.

        Each ``rustydemon-cli export`` invocation re-opens the CASC
        archive and re-parses its index — that's the dominant cost for
        small file extractions (~5-8s on a typical D4 install). When
        we need to pull a list of N specific texture/material files,
        running N invocations is N× slower than necessary.

        rustydemon-cli's ``--path`` accepts a single string but supports
        **brace-expansion globs** (``{a,b,c}``) within that string. We
        group paths by their parent directory and build one
        ``<dir>/{file1,file2,…}.tex``-style glob per group, sending one
        rustydemon call per group. With ``group_size=80`` a 200-texture
        bulk extract collapses from ~200 calls to 3 calls.

        Args:
            paths: Exact CASC virtual paths (e.g.
                ``"base/payload/Texture/foo.tex"``). Globs in the
                input are NOT expanded — pass literal paths only.
            output_dir: Destination directory for extracted files.
            group_size: Max paths combined into one glob. Keeps the
                resulting command line under Windows' ~32K limit.
            workers: Forwarded to ``--parallel``.
            overwrite: Forwarded to :meth:`extract`. *True* (default)
                rewrites files already on disk; *False* skips them,
                which makes a re-run against a warm cache much cheaper.

        Returns:
            Sorted list of files in ``output_dir`` after extraction
            (the extractor walks the dir, so the result includes any
            pre-existing files too — same shape as :meth:`extract`).

        Notes:
            Paths whose **filename** contains glob metacharacters
            (``{``, ``}``, ``,``, ``*``, ``?``, ``[``, ``]``) skip the
            brace-expansion path and get extracted one-per-call to
            avoid corrupting the glob. In practice no D4 paths use
            those characters; the fallback is purely defensive.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Bucket paths by parent directory so each glob shares a prefix.
        groups: dict[str, list[str]] = {}
        unsafe: list[str] = []
        for p in paths:
            p_norm = p.replace("\\", "/")
            parent, _, fname = p_norm.rpartition("/")
            if not fname or _has_glob_meta(fname):
                unsafe.append(p_norm)
                continue
            groups.setdefault(parent, []).append(fname)

        # For each (parent, files) bucket, emit one or more globs of up
        # to ``group_size`` filenames each.
        for parent in sorted(groups):
            files = sorted(groups[parent])
            for i in range(0, len(files), group_size):
                chunk = files[i:i + group_size]
                if len(chunk) == 1:
                    pattern = f"{parent}/{chunk[0]}"
                else:
                    pattern = f"{parent}/{{{','.join(chunk)}}}"
                self.extract(
                    [pattern], output_dir, workers=workers,
                    overwrite=overwrite,
                )

        # Defensive fallback for any paths we couldn't safely batch.
        for p in unsafe:
            self.extract(
                [p], output_dir, workers=workers, overwrite=overwrite,
            )

        # Snapshot what's now on disk (matches :meth:`extract`).
        extracted: list[Path] = []
        for root, _dirs, files in os.walk(output_dir):
            for f in files:
                extracted.append(Path(root) / f)
        return sorted(extracted)

    def extract_model_pair(
        self, model_name: str, output_dir: Path,
        *, d4data_path: Path | None = None,
    ) -> tuple[Path, Path, bool]:
        """Extract both meta and payload .app files for a model.

        Searches ``base/meta/Appearance/`` and ``base/payload/Appearance/``
        for files matching *model_name*. About 15% of D4 appearances ship
        with a payload under a different filename ("shared payloads"); when
        the same-name lookup misses and ``d4data_path`` is supplied, we
        consult ``CoreTOCSharedPayloadsMapping.dat.json`` and re-extract
        using the aliased path before giving up.

        Args:
            model_name: Model name or glob pattern (e.g. ``Sorc_FemaleA``).
            output_dir: Destination directory for extracted files.
            d4data_path: Optional path to the d4data ``json/`` directory.
                Required to resolve shared/aliased payloads.

        Returns:
            ``(meta_path, data_path, is_shared_payload)`` — the third
            value is *True* when the payload was sourced via a shared
            payload override, signalling that the meta and payload come
            from distinct SNO entries (so callers should expect their
            link IDs to differ).

        Raises:
            CASCExtractionError: If either the meta or payload file cannot
                be found or extracted.
        """
        meta_pattern = f"base/meta/Appearance/*{model_name}*"
        payload_pattern = f"base/payload/Appearance/*{model_name}*"

        output_dir = Path(output_dir)
        # rustydemon-cli's ``--path`` is a singular ``Option<String>``
        # (clap rejects multiple ``--path`` args with "cannot be used
        # multiple times"), so we run two sequential one-pattern
        # extractions instead of bundling them into one call.
        extracted_meta = self.extract([meta_pattern], output_dir, workers=2)
        extracted_payload = self.extract(
            [payload_pattern], output_dir, workers=2,
        )
        # Combine results; ``extract`` already walks output_dir, so the
        # second list is the more complete snapshot.
        extracted = extracted_payload or extracted_meta

        # Classify by the CASC-internal path (relative to output_dir) so that
        # the output directory name itself doesn't cause false matches.
        def _rel(p: Path) -> str:
            try:
                return str(p.relative_to(output_dir)).replace("\\", "/")
            except ValueError:
                return str(p).replace("\\", "/")

        meta_files = [p for p in extracted if "base/meta/" in _rel(p)]
        payload_files = [p for p in extracted if "base/payload/" in _rel(p)]

        if not meta_files:
            raise CASCExtractionError(
                f"No meta .app file found for model '{model_name}'. "
                f"Searched pattern: {meta_pattern}\n"
                f"Try running: d4extract list <game_dir> --filter 'base/meta/Appearance/*' "
                f"to see available models."
            )

        override_path: str | None = None
        if not payload_files and d4data_path is not None:
            from d4extract.casc.payload_resolver import resolve_payload_path

            override_path = resolve_payload_path(model_name, d4data_path)
            if override_path is not None:
                log.debug(
                    "Resolved shared payload override for '%s': %s",
                    model_name, override_path,
                )
                extracted = self.extract(
                    [override_path], output_dir, workers=2,
                )
                payload_files = [
                    p for p in extracted if "base/payload/" in _rel(p)
                ]

        if not payload_files:
            tried_override = (
                f"\nAlso tried shared-payload override: {override_path}"
                if override_path is not None else ""
            )
            hint = (
                "\nIf d4data is not configured, this model may use a shared "
                "payload — set the d4data path so overrides can be resolved."
                if d4data_path is None else ""
            )
            raise CASCExtractionError(
                f"No payload/data .app file found for model '{model_name}'. "
                f"Searched pattern: {payload_pattern}{tried_override}{hint}\n"
                f"The model may only have metadata, or the payload path convention "
                f"may differ. Check with: d4extract list <game_dir> --filter "
                f"'base/payload/Appearance/*{model_name}*'"
            )

        # Detect shared payloads by stem comparison rather than by which
        # branch placed the file. ``self.extract()`` snapshots the whole
        # output_dir via os.walk, so a payload extracted by a previous
        # session via the resolver would otherwise show up in
        # ``payload_files`` on the same-name branch and bypass the
        # is_shared flag.
        is_shared_payload = payload_files[0].stem != meta_files[0].stem

        return meta_files[0], payload_files[0], is_shared_payload

    def extract_anim_pair(
        self, anim_name: str, output_dir: Path,
        *, d4data_path: Path | None = None,
    ) -> tuple[Path, Path, bool]:
        """Extract both meta and payload .ani files for an animation.

        Searches ``base/meta/Anim/`` and ``base/payload/Anim/`` for files
        matching *anim_name*. About 7,500 of D4's ~45,000 animations
        ship with a payload under a different filename ("shared
        payloads"); when the same-name lookup misses and ``d4data_path``
        is supplied, we consult ``CoreTOCSharedPayloadsMapping.dat.json``
        and re-extract using the aliased path before giving up. The
        returned pair is stem-matched to *anim_name*, so a single
        ``output_dir`` may be reused safely across calls.

        Args:
            anim_name: Animation name or glob pattern (e.g.
                ``barM_HTH_nav_idle``).
            output_dir: Destination directory for extracted files.
            d4data_path: Optional path to the d4data ``json/`` directory.
                Required to resolve shared/aliased animation payloads.

        Returns:
            ``(meta_path, payload_path, is_shared_payload)`` — the third
            value is *True* when the payload was sourced via a shared
            payload override, signalling that the meta and payload come
            from distinct SNO entries.

        Raises:
            CASCExtractionError: If either the meta or payload file
                cannot be found or extracted.
        """
        output_dir = Path(output_dir)

        meta_pattern = f"base/meta/Anim/*{anim_name}*"
        payload_pattern = f"base/payload/Anim/*{anim_name}*"

        # rustydemon-cli's ``--path`` is a singular ``Option<String>``
        # — same constraint as :meth:`extract_model_pair`, so issue two
        # one-pattern extractions instead of bundling them.
        self.extract([meta_pattern], output_dir, workers=2)
        self.extract([payload_pattern], output_dir, workers=2)

        # Resolve the result by stem-matching *anim_name* against the two
        # known Anim directories. ``extract()`` snapshots the *whole*
        # output_dir, so picking from its return value would surface
        # whatever sorts first — another animation's file whenever
        # output_dir is reused across calls.
        meta_dir = output_dir / "base" / "meta" / "Anim"
        payload_dir = output_dir / "base" / "payload" / "Anim"

        meta = _resolve_anim_file(meta_dir, anim_name)
        if meta is None:
            raise CASCExtractionError(
                f"No meta .ani file found for animation '{anim_name}'. "
                f"Searched pattern: {meta_pattern}\n"
                f"Try running: d4extract list <game_dir> --filter "
                f"'base/meta/Anim/*' to see available animations."
            )

        override_path: str | None = None
        payload = _resolve_anim_file(payload_dir, anim_name)
        if payload is None and d4data_path is not None:
            from d4extract.casc.payload_resolver import resolve_payload_path

            override_path = resolve_payload_path(
                anim_name, d4data_path, sno_group="Anim",
            )
            if override_path is not None:
                log.debug(
                    "Resolved shared anim payload override for '%s': %s",
                    anim_name, override_path,
                )
                self.extract([override_path], output_dir, workers=2)
                # The override is a full CASC path; its payload lands
                # under the *aliased* animation's stem, not anim_name's.
                payload = _resolve_anim_file(
                    payload_dir, Path(override_path).stem,
                )

        if payload is None:
            tried_override = (
                f"\nAlso tried shared-payload override: {override_path}"
                if override_path is not None else ""
            )
            hint = (
                "\nIf d4data is not configured, this animation may use a "
                "shared payload — set the d4data path so overrides can be "
                "resolved."
                if d4data_path is None else ""
            )
            raise CASCExtractionError(
                f"No payload .ani file found for animation '{anim_name}'. "
                f"Searched pattern: {payload_pattern}{tried_override}{hint}\n"
                f"The animation may only have metadata, or the payload "
                f"path convention may differ. Check with: d4extract list "
                f"<game_dir> --filter 'base/payload/Anim/*{anim_name}*'"
            )

        # A shared payload resolves to a file under a different stem;
        # comparing stems is how the is_shared flag is reported.
        is_shared_payload = payload.stem != meta.stem

        return meta, payload, is_shared_payload

    def extract_anim_pair_batch(
        self,
        anim_names: list[str],
        output_dir: Path,
        *,
        d4data_path: Path | None = None,
        group_size: int = 80,
        workers: int | None = None,
    ) -> dict[str, tuple[Path, Path, bool]]:
        """Extract meta + payload .ani files for many animations at once.

        :meth:`extract_anim_pair` issues two ``rustydemon-cli`` calls per
        animation (one for the meta .ani, one for the payload), and every
        call re-opens the CASC archive and re-parses its TVFS tree —
        roughly 5-8s of fixed overhead. A 50-animation boss export
        therefore burns ~10 minutes on subprocess startup before any
        decoding begins.

        This batches the work the way :meth:`extract_many` does for
        textures. rustydemon's ``--path`` is a single string but supports
        brace-expansion globs, and globset expands several ``{...}``
        groups in one pattern independently — so a single pattern of the
        shape ``base/{meta,payload}/Anim/{name1.ani,name2.ani,...}`` pulls
        every meta and payload for a chunk of animations in one call.
        With ``group_size=80`` a 50-animation export collapses from ~100
        rustydemon invocations to one.

        Args:
            anim_names: Bare animation names — no extension, no directory
                (e.g. ``barM_HTH_nav_idle``). Duplicates are ignored.
            output_dir: Destination directory. Extracted files mirror
                their CASC paths beneath it, so the bulk results land at
                ``output_dir/base/{meta,payload}/Anim/<name>.ani``.
            d4data_path: Optional path to the d4data ``json/`` directory.
                Forwarded to the per-name :meth:`extract_anim_pair`
                fallback so shared/aliased payloads can still resolve.
            group_size: Max animation names combined into one glob. Keeps
                the command line under Windows' ~32K limit — same cap as
                :meth:`extract_many`.
            workers: Forwarded to ``--parallel``.

        Returns:
            A mapping ``{anim_name: (meta_path, payload_path, is_shared)}``.
            Shared-payload animations missed by the bulk pass are
            resolved in one extra batched call (see Notes); ``is_shared``
            is ``True`` for those. Anything still unresolved gets a
            one-off :meth:`extract_anim_pair` retry, and if that also
            fails the name is omitted from the result and logged at
            WARNING level.

        Notes:
            The bulk brace pattern can only ever pair same-name files,
            so the ~16% of animations that reuse another animation's
            payload ("shared payloads") miss the first pass. Those are
            resolved via ``CoreTOCSharedPayloadsMapping`` and pulled in
            a single additional batched call — not one extraction per
            name. Only names with no mapping entry (or whose mapped
            payload isn't in the archive) and glob-metacharacter names
            fall through to the slow per-name retry.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Dedupe, and split off names that would corrupt a brace glob.
        safe: list[str] = []
        unsafe: list[str] = []
        seen: set[str] = set()
        for name in anim_names:
            if not name or name in seen:
                continue
            seen.add(name)
            (unsafe if _has_glob_meta(name) else safe).append(name)

        # Bulk pass: one brace glob per chunk, covering the meta and the
        # payload tree at once. globset expands the two ``{...}`` groups
        # independently, so ``base/{meta,payload}/Anim/{a.ani,b.ani}``
        # matches all four files. overwrite=False lets a warm cache
        # short-circuit rustydemon's BLTE decode instead of rewriting.
        safe_sorted = sorted(safe)
        for i in range(0, len(safe_sorted), group_size):
            chunk = safe_sorted[i:i + group_size]
            if len(chunk) == 1:
                pattern = f"base/{{meta,payload}}/Anim/{chunk[0]}.ani"
            else:
                joined = ",".join(f"{n}.ani" for n in chunk)
                pattern = f"base/{{meta,payload}}/Anim/{{{joined}}}"
            self.extract(
                [pattern], output_dir, workers=workers, overwrite=False,
            )

        # Index both Anim directories once, by file stem. Reading the two
        # known directories directly (rather than os.walk over the whole
        # output tree) keeps the per-name fallback extractions below from
        # being mistaken for bulk results.
        def _index_dir(anim_dir: Path) -> dict[str, Path]:
            index: dict[str, Path] = {}
            if anim_dir.is_dir():
                for entry in anim_dir.iterdir():
                    if entry.is_file() and entry.suffix == ".ani":
                        index[entry.stem] = entry
            return index

        meta_by_stem = _index_dir(output_dir / "base" / "meta" / "Anim")
        payload_by_stem = _index_dir(
            output_dir / "base" / "payload" / "Anim"
        )

        result: dict[str, tuple[Path, Path, bool]] = {}
        missing: list[str] = []
        for name in safe:
            meta = meta_by_stem.get(name)
            payload = payload_by_stem.get(name)
            if meta is not None and payload is not None:
                # The bulk pattern only ever pairs same-name files, so a
                # bulk hit is never a shared payload.
                result[name] = (meta, payload, False)
            else:
                missing.append(name)

        # Bulk misses are overwhelmingly shared-payload animations:
        # their meta landed in the bulk pass but the payload lives under
        # a different filename, so the same-name brace glob never paired
        # it. Resolve those aliases against ``CoreTOCSharedPayloadsMapping``
        # and pull every real payload in ONE more bulk call, instead of
        # re-opening CASC once per animation (~6s of fixed overhead each).
        unresolved: list[str] = []
        if missing:
            from d4extract.casc.payload_resolver import resolve_payload_path

            # anim_name -> real payload CASC path (base/payload/Anim/<x>.ani).
            payload_aliases: dict[str, str] = {}
            for name in missing:
                real_path: str | None = None
                if d4data_path is not None:
                    try:
                        real_path = resolve_payload_path(
                            name, d4data_path, sno_group="Anim",
                        )
                    except Exception as exc:
                        log.debug(
                            "Shared-payload lookup failed for '%s': %s",
                            name, exc,
                        )
                if real_path is None:
                    unresolved.append(name)
                else:
                    payload_aliases[name] = real_path

            log.info(
                "Batched anim extraction: bulk pass missed %d payload(s); "
                "%d resolved via shared-payload mapping (batched), "
                "%d falling back to per-name retry",
                len(missing), len(payload_aliases),
                len(unresolved) + len(unsafe),
            )

            if payload_aliases:
                # One more bulk pass over the real payload paths.
                # extract_many buckets by parent dir and chunks at
                # group_size — every Anim payload shares
                # ``base/payload/Anim/``, so this is a single rustydemon
                # call in practice.
                self.extract_many(
                    sorted(set(payload_aliases.values())),
                    output_dir,
                    group_size=group_size,
                    workers=workers,
                    overwrite=False,
                )
                # The payload dir now also holds the aliased real-name
                # payloads — re-index so the lookups below see them.
                payload_by_stem = _index_dir(
                    output_dir / "base" / "payload" / "Anim"
                )
                for name, real_path in payload_aliases.items():
                    real_stem = real_path.rsplit("/", 1)[-1]
                    if real_stem.endswith(".ani"):
                        real_stem = real_stem[:-4]
                    real_payload = payload_by_stem.get(real_stem)
                    meta = meta_by_stem.get(name)
                    if real_payload is None or meta is None:
                        # Mapping pointed at a payload the archive doesn't
                        # hold (stale index / corruption), or the meta
                        # vanished — hand to the last-resort retry.
                        unresolved.append(name)
                        continue
                    # Downstream (the export worker's cache check and the
                    # decoder) looks for the payload at the ANIMATION's
                    # name, not the real payload's — materialise a copy
                    # there so both the return tuple and the on-disk
                    # cache convention hold.
                    expected = (
                        output_dir / "base" / "payload" / "Anim"
                        / f"{name}.ani"
                    )
                    if not expected.exists():
                        self._materialize_aliased_payload(
                            real_payload, expected,
                        )
                    result[name] = (meta, expected, True)

        # Last-resort per-name retry: names with no shared-payload entry,
        # names whose mapped payload wasn't in the archive, and the
        # glob-unsafe names. extract_anim_pair walks its whole output dir
        # and assumes a single animation lives there, so each retry gets
        # an isolated subdir kept clear of the bulk ``base/`` tree.
        for name in (*unresolved, *unsafe):
            retry_dir = (
                output_dir / "_anim_fallback" / _sanitize_dirname(name)
            )
            try:
                meta, payload, is_shared = self.extract_anim_pair(
                    name, retry_dir, d4data_path=d4data_path,
                )
            except Exception as exc:
                log.warning(
                    "Batched anim extraction: could not locate '%s' "
                    "after per-name retry: %s", name, exc,
                )
                continue
            result[name] = (meta, payload, is_shared)

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _materialize_aliased_payload(src: Path, dst: Path) -> None:
        """Copy a shared payload to the animation-named path.

        rustydemon writes a shared payload to disk under its *real*
        filename, but downstream code (the export worker's cache check
        and the decoder) looks for it under the *animation's* name. A
        plain copy is the portable choice — a hardlink or symlink would
        be faster but fails across volumes and, for symlinks on
        Windows, needs elevation.
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    def _validate_game_dir(self) -> None:
        """Check that game_dir looks like a Diablo IV install."""
        if not self.game_dir.is_dir():
            raise CASCExtractionError(
                f"Game directory does not exist: {self.game_dir}\n"
                f"Provide the path to your Diablo IV installation folder."
            )

        has_marker = any(
            (self.game_dir / marker).exists() for marker in _D4_MARKERS
        )
        if not has_marker:
            raise CASCExtractionError(
                f"'{self.game_dir}' does not look like a Diablo IV installation — "
                f"expected a 'Data/' subdirectory or '.build.info' file.\n"
                f"For Steam installs, this is typically under "
                f"steamapps/common/Diablo IV/.\n"
                f"For Battle.net installs, check the install location in "
                f"the Battle.net launcher settings."
            )

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run rustydemon-cli with the given arguments.

        Captures stdout/stderr. Raises :class:`CASCExtractionError` on
        non-zero exit.
        """
        cmd = [str(self.binary), *args]
        log.debug("Running: %s", " ".join(cmd))

        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                **_SUBPROCESS_KWARGS,
            )
        except subprocess.CalledProcessError as exc:
            raise CASCExtractionError(
                f"rustydemon-cli failed (exit {exc.returncode}):\n{exc.stderr}",
                cmd=cmd,
                stderr=exc.stderr,
            ) from exc

    def _run_streaming(self, args: list[str]) -> None:
        """Run rustydemon-cli, streaming stderr to the console in real time.

        Used for long-running extractions so the user sees progress.
        """
        cmd = [str(self.binary), *args]
        log.debug("Running (streaming): %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **_SUBPROCESS_KWARGS,
            )
            # Stream stderr lines for progress output, but also
            # accumulate them so we can report the real error on failure.
            assert proc.stderr is not None
            stderr_lines: list[str] = []
            for line in proc.stderr:
                stripped = line.rstrip()
                log.info("[rustydemon] %s", stripped)
                stderr_lines.append(stripped)

            proc.wait()
            if proc.returncode != 0:
                captured_stderr = "\n".join(stderr_lines)
                stdout = proc.stdout.read() if proc.stdout else ""
                combined = captured_stderr or stdout or "(no output)"
                raise CASCExtractionError(
                    f"rustydemon-cli failed (exit {proc.returncode}):\n{combined}",
                    cmd=cmd,
                    stderr=captured_stderr,
                )
        except FileNotFoundError:
            raise CASCExtractionError(
                f"Could not execute rustydemon-cli at {self.binary}. "
                f"Is the binary present and executable?"
            )

    @staticmethod
    def _parse_file_listing(stdout: str) -> list[CASCEntry]:
        """Parse rustydemon-cli dry-run output into CASCEntry objects.

        Handles tab-separated (path<TAB>size), space-separated, and
        plain path-per-line formats.
        """
        entries: list[CASCEntry] = []
        for line in stdout.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Try tab-separated: path\tsize
            parts = line.split("\t")
            if len(parts) >= 2:
                path = parts[0].strip()
                try:
                    size = int(parts[1].strip())
                except ValueError:
                    size = 0
                entries.append(CASCEntry(path=path, size=size))
                continue

            # Try space-separated with size at the end: path  12345
            match = re.match(r"^(.+?)\s+(\d+)\s*$", line)
            if match:
                entries.append(
                    CASCEntry(path=match.group(1).strip(), size=int(match.group(2)))
                )
                continue

            # Fallback: treat entire line as a path with unknown size
            entries.append(CASCEntry(path=line, size=0))

        return entries
