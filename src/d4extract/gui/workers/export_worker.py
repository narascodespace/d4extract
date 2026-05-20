"""Background worker that runs ``GltfExporter.export`` off the UI thread."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths, QThread, Signal

log = logging.getLogger(__name__)


# Constructor kwargs accepted by GltfExporter. Anything else in the
# options dict (``format``, ``d4data_path``, etc.) is consumed by the
# worker itself and stripped before instantiation.
_EXPORTER_KWARGS = {
    "coordinate_transform",
    "export_normals",
    "export_tangents",
    "export_uvs",
    "export_colors",
    "export_skin",
    "prune_bones",
    "write_materials_sidecar",
    "texture_dir",
    "embed_textures",
    "include_cloth",
}


def _texture_cache_root() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
    return Path(base) / "d4extract" / "textures"


# Decoder errors for unsupported curve formats embed the offending
# compression mode as ``flCompression=N``; pulling N out lets the
# failure dump summarise which mode is blocking the most animations.
_FL_COMPRESSION_RE = re.compile(r"flCompression=(\d+)")


def _extract_compression_mode(error_message: str) -> int | None:
    """Pull the ``flCompression=N`` mode out of a decoder error string.

    Returns the int mode, or ``None`` when the message carries none
    (e.g. a missing-payload or crash failure rather than an
    unsupported-compression failure).
    """
    m = _FL_COMPRESSION_RE.search(error_message)
    return int(m.group(1)) if m else None


def _classify_failure(error_message: str) -> str:
    """Bucket a decode-failure message into a coarse category.

    Long-form (u16-timestamp) misses are a frame-count issue, not a
    compression-mode one — they carry no ``flCompression=N`` substring,
    so the per-mode summary lumps them under ``unknown``. This category
    split tells them apart from genuinely unsupported modes at a glance.
    """
    if "long-form" in error_message:
        return "long_form_timestamps"
    if "unsupported flCompression" in error_message:
        return "unsupported_compression"
    return "other"


def _dump_decode_failures(
    failures: list[tuple[str, str, str, int | None]],
    export_path: Path,
) -> Path | None:
    """Write the full list of decode failures next to the export output.

    ``failures`` is ``(name, appearance_name, error_message,
    compression_mode_or_None)``. The user-facing dialog only previews
    the first few names; this sibling file is the comprehensive record
    used to diagnose which compression modes are still unsupported.
    Returns the dump file path, or ``None`` when there were no
    failures.
    """
    if not failures:
        return None

    dump_path = export_path.with_suffix(
        export_path.suffix + ".decode_failures.txt"
    )
    lines = [
        f"# Decode failures from {export_path.name}",
        f"# Total: {len(failures)} animation(s)",
        "",
    ]

    # Per-compression-mode summary — tells us whether implementing one
    # more mode would unlock most of the misses.
    by_mode: dict[int | None, int] = {}
    for _, _, _, mode in failures:
        by_mode[mode] = by_mode.get(mode, 0) + 1
    lines.append("# By compression mode:")
    for mode, count in sorted(
        by_mode.items(),
        key=lambda kv: (-kv[1], kv[0] if kv[0] is not None else -1),
    ):
        label = f"flCompression={mode}" if mode is not None else "unknown"
        lines.append(f"#   {label}: {count}")
    lines.append("")

    # Per-category summary — separates long-form (u16-timestamp) misses,
    # which are a frame-count issue rather than a compression-mode one,
    # from genuinely unsupported modes and other errors.
    by_category: dict[str, int] = {}
    for _, _, error, _ in failures:
        cat = _classify_failure(error)
        by_category[cat] = by_category.get(cat, 0) + 1
    lines.append("# By failure category:")
    for cat, count in sorted(by_category.items(), key=lambda kv: -kv[1]):
        lines.append(f"#   {cat}: {count}")
    lines.append("")

    # Full per-animation list, grouped by appearance then name.
    lines.append("# name\tappearance_name\tflCompression\terror")
    for name, appearance, error, mode in sorted(
        failures, key=lambda f: (f[1] or "", f[0])
    ):
        mode_str = str(mode) if mode is not None else ""
        # Collapse internal whitespace so each row stays on one line.
        error_one_line = " ".join(error.split())
        lines.append(f"{name}\t{appearance}\t{mode_str}\t{error_one_line}")

    dump_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dump_path


class ExportWorker(QThread):
    """Run a single ``GltfExporter.export`` call on a background thread.

    The worker also handles the upstream concerns the GUI doesn't want
    to do on the main thread: optional material resolution from a
    d4data path, and optional texture extraction from CASC when
    ``embed_textures`` is requested. Everything else is forwarded to
    GltfExporter as-is.
    """

    finished = Signal(str)
    progress = Signal(str)
    # ``warning`` carries a non-fatal issue that the export proceeded
    # despite (e.g. textures requested but couldn't be embedded). Emitted
    # *in addition to* ``finished`` so the caller can surface it
    # prominently — silent fallbacks are exactly what hid this bug.
    warning = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        mesh_data: Any = None,
        output_path: Path | None = None,
        options: dict | None = None,
        *,
        game_dir: Path | None = None,
        d4data_path: Path | None = None,
        texture_dir: Path | None = None,
        submesh_filter: set[int] | None = None,
        extra_meshes: list[tuple[Any, set[int] | None]] | None = None,
        skinned_pieces: list[tuple[Any, set[int] | None]] | None = None,
        canonical_skeleton: Any | None = None,
        animations: list[Any] | None = None,
        anim_infos: list[Any] | None = None,
        rest_pose_map: dict[int, tuple] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._mesh = mesh_data
        self._output_path = Path(output_path)
        self._options = dict(options or {})
        self._game_dir = Path(game_dir) if game_dir is not None else None
        self._d4data_path = (
            Path(d4data_path) if d4data_path is not None else None
        )
        # User-supplied pre-extracted textures root. When set, the worker
        # uses it directly instead of re-extracting via rustydemon — same
        # workflow as the CLI's ``--texture-dir``.
        self._user_texture_dir = (
            Path(texture_dir) if texture_dir is not None else None
        )
        self._submesh_filter = (
            set(submesh_filter) if submesh_filter is not None else None
        )
        # Static (unskinned) meshes to ship as separate Mesh nodes
        # alongside the primary skinned assembly. Used by the Character
        # Builder so weapons export as standalone objects.
        self._extra_meshes: list[tuple[Any, set[int] | None]] = list(
            extra_meshes or [],
        )
        # Per-piece export path (Character Builder): when set, each
        # ``(MeshData, submesh_filter)`` is written as its own glTF mesh
        # node, all sharing ``canonical_skeleton`` as a single skin.
        # ``None`` keeps the legacy single-``MeshData`` path (Model
        # Browser, and weapons-only builder exports). An empty list is
        # treated the same as ``None``.
        self._skinned_pieces: list[tuple[Any, set[int] | None]] | None = (
            list(skinned_pieces) if skinned_pieces else None
        )
        self._canonical_skeleton = canonical_skeleton
        # Decoded animations to embed as glTF animation entries. Each
        # is a ``DecodedAnimation`` (forward-typed to avoid an import
        # in workers/ that's only needed by callers that already have
        # one). The exporter silently drops any animation whose mesh
        # has no skeleton or when ``export_skin=False``.
        self._animations: list[Any] = list(animations or [])
        # Animations to fetch + decode inside the worker, keyed by
        # ``AnimationInfo`` objects from the GUI's discovery cache.
        # Decoding all of them on the main thread would freeze the UI
        # for big appearances (npc_crow has ~20 anims; bosses can have
        # hundreds), so the heavy lifting happens here in run(). Each
        # success is appended to ``self._animations``; failures are
        # logged + skipped so one bad anim doesn't tank the export.
        self._anim_infos: list[Any] = list(anim_infos or [])
        self._rest_pose_map: dict[int, tuple] = rest_pose_map or {}
        # Customization variants (skin tone, makeup, markings) discovered
        # for the sidecar's ``variants`` block. Filled by
        # ``_discover_variants`` when a sidecar is being written and a
        # d4data path is configured; ``None`` otherwise.
        self._variants: dict | None = None

    # ------------------------------------------------------------------
    # QThread.run
    # ------------------------------------------------------------------

    def run(self) -> None:  # noqa: D401 — Qt API
        try:
            self._run_inner()
        except Exception as exc:
            log.exception("ExportWorker crashed")
            self.error.emit(f"{type(exc).__name__}: {exc}")

    def _run_inner(self) -> None:
        self.progress.emit("Preparing export…")

        # Strip non-exporter keys from the options dict.
        opts = {k: v for k, v in self._options.items() if k in _EXPORTER_KWARGS}

        embed_textures = bool(opts.get("embed_textures"))
        sidecar = bool(opts.get("write_materials_sidecar"))
        needs_materials = embed_textures or sidecar

        # Track what couldn't be done so we can surface it AFTER the
        # export completes — silently dropping these flags is exactly
        # how the "no textures embedded" regression slipped through.
        skipped_reason: str | None = None

        # Materials are looked up from the d4data JSON tree. Both
        # texture embedding and the materials sidecar require them.
        if needs_materials:
            if self._d4data_path is None:
                # Caller asked for textures/sidecar but didn't provide a
                # d4data path. Strip the dependent flags rather than
                # failing — the main thread is responsible for prompting.
                if embed_textures:
                    opts["embed_textures"] = False
                    embed_textures = False
                    skipped_reason = (
                        "D4Data path is not configured "
                        "(File → Set D4Data Path…) — skin-tone and "
                        "variant dropdowns will also be empty"
                    )
                if sidecar:
                    opts["write_materials_sidecar"] = False
                    sidecar = False
                    if skipped_reason is None:
                        skipped_reason = (
                            "D4Data path is not configured "
                            "(File → Set D4Data Path…) — the materials "
                            "sidecar was skipped, so skin-tone and "
                            "variant dropdowns will be empty"
                        )
            else:
                # Resolve materials for every mesh this export writes —
                # the primary / per-piece meshes AND the static extras —
                # so the exporter can emit per-mesh textures and PBR
                # factors. Failures are non-fatal: a mesh with no
                # resolved materials falls back to the Material_<idx>
                # stub.
                for mesh in self._export_meshes():
                    self._resolve_materials_for(mesh)
                # Discover customization variants for the sidecar's
                # ``variants`` block (skin tone, makeup, markings).
                # Cached on self._variants *before* the texture step so
                # _wanted_texture_paths() can pre-extract variant
                # textures too. Only worth doing when the sidecar is
                # being written.
                if sidecar:
                    self._discover_variants()
                if embed_textures and not any(
                    getattr(m, "materials", None)
                    for m in self._export_meshes()
                ):
                    # _resolve_materials_for emits its own progress;
                    # capture the user-visible reason for the warning.
                    skipped_reason = (
                        "material resolution failed for "
                        f"{self._export_stem()!r} — check the D4Data path"
                    )

        # Texture embedding additionally needs a texture_dir holding the
        # decoded .tex payloads. Resolution order:
        #   1. User-supplied pre-extracted textures dir (CLI parity).
        #   2. Auto-extract from CASC into a per-stem cache.
        # In both cases we VERIFY at least one referenced .tex landed on
        # disk before handing the path to GltfExporter; otherwise the
        # exporter would build a TexturePack pointing at nothing and
        # silently produce a textureless GLB.
        any_materials = any(
            getattr(m, "materials", None) for m in self._export_meshes()
        )
        if embed_textures and any_materials:
            wanted = self._wanted_texture_paths()
            texture_dir, tex_reason = self._resolve_texture_dir(wanted)
            if texture_dir is not None:
                opts["texture_dir"] = texture_dir
            else:
                opts["embed_textures"] = False
                opts.pop("texture_dir", None)
                embed_textures = False
                skipped_reason = tex_reason or skipped_reason

        # Bulk-decode discovered animations on the worker thread. Each
        # AnimationInfo turns into a CASC extract → parse → decode chain
        # the same way ``AnimLoadWorker`` does for a single anim — only
        # batched here so a single export can ship every animation the
        # GUI's discovery cache found. Permutation 0 only; multi-perm
        # variants are rare and would balloon big-boss exports.
        anim_skip_reason: str | None = None
        if self._anim_infos:
            self._decode_anim_infos()
            if not self._animations:
                anim_skip_reason = (
                    "every discovered animation failed to decode — "
                    "check the d4data path and CASC extraction"
                )

        self.progress.emit("Writing GLB…")

        try:
            from d4extract.export.gltf_export import (
                GltfExporter,
                GltfExportError,
            )
        except Exception as exc:
            self.error.emit(f"Failed to import exporter: {exc}")
            return

        try:
            exporter = GltfExporter(**opts)
        except (TypeError, ValueError) as exc:
            self.error.emit(f"Invalid export options: {exc}")
            return

        try:
            if self._skinned_pieces is not None:
                # Character Builder per-piece path: one glTF mesh node
                # per equipped piece, all sharing one skin.
                out = exporter.export_assembly(
                    self._skinned_pieces,
                    self._canonical_skeleton,
                    self._output_path,
                    static_extras=self._extra_meshes or None,
                    animations=self._animations or None,
                    variants=self._variants,
                )
            else:
                # Legacy single-MeshData path (Model Browser, and
                # weapons-only builder exports).
                out = exporter.export(
                    self._mesh,
                    self._output_path,
                    submesh_filter=self._submesh_filter,
                    extra_meshes=self._extra_meshes or None,
                    animations=self._animations or None,
                    variants=self._variants,
                )
        except GltfExportError as exc:
            self.error.emit(f"Export error: {exc}")
            return
        except Exception as exc:
            log.exception("export() raised")
            self.error.emit(f"{type(exc).__name__}: {exc}")
            return

        try:
            size = out.stat().st_size
        except OSError:
            size = 0

        size_mb = size / (1024 * 1024) if size else 0.0
        verts = exporter.last_vertex_count
        tris = exporter.last_triangle_count

        msg = (
            f"Exported {out.name} "
            f"({verts:,} verts, {tris:,} tris, {size_mb:.1f} MB)"
        )
        if exporter.last_image_count:
            embed_mb = exporter.last_embedded_bytes / (1024 * 1024)
            msg += (
                f" · {exporter.last_image_count} textures, "
                f"{embed_mb:.1f} MB embedded"
            )
        if self._animations:
            msg += f" · {len(self._animations)} animation(s)"

        # Texture embedding was requested but the exporter ended up
        # with zero images — still a silent failure (e.g. all referenced
        # .tex files were missing or undecodable). Promote it to a
        # warning so the user knows.
        requested_textures = bool(self._options.get("embed_textures"))
        if requested_textures and not exporter.last_image_count and skipped_reason is None:
            skipped_reason = (
                "no referenced textures could be resolved from the "
                "configured directory — check the texture_dir contents"
            )

        # If the user loaded an animation but disabled --include-skin
        # (export_skin=False), the exporter silently dropped it. Surface
        # that as a warning so the user can re-enable skin and re-export.
        if self._animations and not bool(self._options.get("export_skin", True)):
            warn = (
                "Animations passed in but Include Skeleton is disabled — "
                "no animation channels were written. Re-enable Include "
                "Skeleton in the export menu to embed animations."
            )
            log.warning(warn)
            self.warning.emit(warn)

        if skipped_reason:
            warn = f"Textures not embedded: {skipped_reason}"
            log.warning(warn)
            self.warning.emit(warn)
            msg += " — textures skipped"

        self.finished.emit(msg)

    # ------------------------------------------------------------------
    # Material / texture preparation
    # ------------------------------------------------------------------

    def _export_meshes(self) -> list[Any]:
        """Every ``MeshData`` this export writes, for material/texture prep.

        Per-piece path: the skinned pieces + the static extras. Legacy
        path: the primary mesh + the static extras.
        """
        if self._skinned_pieces is not None:
            return [
                *(m for m, _ in self._skinned_pieces),
                *(m for m, _ in self._extra_meshes),
            ]
        return [self._mesh, *(m for m, _ in self._extra_meshes)]

    def _export_skeleton(self) -> Any:
        """The skeleton this export skins against.

        Per-piece path: the canonical skeleton shared by every piece.
        Legacy path: the primary mesh's own skeleton.
        """
        if getattr(self, "_skinned_pieces", None) is not None:
            return getattr(self, "_canonical_skeleton", None)
        return getattr(self._mesh, "skeleton", None)

    def _export_stem(self) -> str:
        """A representative name for caches / user-facing messages."""
        if self._skinned_pieces:
            first = self._skinned_pieces[0][0]
            return getattr(first, "name", None) or "character"
        return getattr(self._mesh, "name", None) or "model"

    def _resolve_materials_for(self, mesh: Any) -> None:
        """Populate ``mesh.materials`` from d4data, keyed by ``mesh.name``.

        No-op when materials are already present on the mesh — the
        Character Builder's per-piece texture worker often pre-populates
        them, in which case we'd just be re-doing the lookup.
        """
        if self._d4data_path is None:
            return
        if getattr(mesh, "materials", None):
            return
        try:
            from d4extract.formats.material_parser import (
                MaterialResolutionError,
                load_materials,
            )
        except Exception as exc:
            log.warning("material_parser import failed: %s", exc)
            return

        stem = getattr(mesh, "name", None) or ""
        if not stem:
            return

        self.progress.emit(f"Resolving materials for {stem}…")
        try:
            materials = load_materials(stem, self._d4data_path)
        except MaterialResolutionError as exc:
            log.warning("Material resolution failed for %s: %s", stem, exc)
            self.progress.emit(f"Material resolution skipped: {exc}")
            return
        mesh.materials = materials

    def _discover_variants(self) -> None:
        """Discover the sidecar ``variants`` block (skin tone, makeup …).

        Result is cached on ``self._variants``. Discovery runs against
        the **full concatenated materials list** the sidecar will
        contain — primary mesh + extras for a single-mesh export, or
        every skinned piece + static extras for a Character Builder
        assembly export.

        Using the same combined list as the sidecar's ``materials[]``
        array keeps each variant block's ``applies_to_materials``
        indices aligned. Discovering against only the primary/first
        piece (the prior behaviour) silently dropped any variant target
        living on a non-primary assembly piece — e.g. an equipped hair
        piece's ``hero_hair`` material — so the dropdown populated but
        Apply Variants found no targets to tint.

        Non-fatal: on any failure (e.g. an incomplete d4data tree for a
        particular class) the export proceeds with no variants block
        rather than crashing.
        """
        if self._d4data_path is None:
            return
        try:
            from d4extract.export.gltf_export import (
                combined_materials_for_export,
            )
            from d4extract.formats.variants import discover_variants

            if self._skinned_pieces:
                combined = combined_materials_for_export(
                    skinned_pieces=self._skinned_pieces,
                    static_extras=self._extra_meshes or None,
                )
            else:
                combined = combined_materials_for_export(
                    primary_mesh=self._mesh,
                    extra_meshes=self._extra_meshes or None,
                )
            if not combined:
                return
            self._variants = discover_variants(
                self._export_stem(), self._d4data_path, combined,
            )
        except Exception as exc:
            # Variant discovery is non-fatal — log and continue.
            log.warning("variant discovery failed: %s", exc)
            self._variants = None

    # ------------------------------------------------------------------
    # Animation decode (batched, off-thread)
    # ------------------------------------------------------------------

    def _decode_anim_infos(self) -> None:
        """Extract + parse + decode every queued ``AnimationInfo``.

        CASC extraction is the slow part: each rustydemon call re-opens
        the archive and re-parses its TVFS tree (~5-8s). To pay that once
        instead of once-per-animation, every anim not already in the
        on-disk cache is fetched in a single batched call via
        :meth:`RustyDemonCLI.extract_anim_pair_batch`; anims already
        cached skip CASC entirely. Parse + decode then runs sequentially
        per anim — successes append to ``self._animations``, failures are
        logged + skipped so one broken anim doesn't sink the export.
        Pre-conditions (game dir + d4data) are checked upfront: without
        them no anim can be decoded, so we bail with a single warning
        instead of N identical ones.
        """
        if self._game_dir is None:
            log.warning(
                "Animation export skipped: no game directory configured",
            )
            return
        if self._d4data_path is None:
            log.warning(
                "Animation export skipped: no d4data path configured",
            )
            return

        try:
            from d4extract.casc.rustydemon import (
                CASCExtractionError,
                RustyDemonCLI,
            )
            from d4extract.formats.anim_parser import (
                AnimFormatError,
                decode_permutation,
                parse_anim,
            )
            # The batched extractor lays anims out as
            # ``base/{meta,payload}/Anim/<name>.ani`` under one shared
            # cache root, so a re-export of the same model skips CASC for
            # every anim still on disk.
            from d4extract.gui.workers.anim_worker import _anim_cache_root
        except Exception as exc:
            log.exception("Animation imports failed")
            self.warning.emit(
                f"Animation export skipped: import failure ({exc})"
            )
            return

        try:
            rd = RustyDemonCLI(self._game_dir)
        except CASCExtractionError as exc:
            log.warning("RustyDemonCLI unavailable for anim batch: %s", exc)
            self.warning.emit(
                f"Animation export skipped: CASC unavailable ({exc})"
            )
            return

        total = len(self._anim_infos)
        # Structured decode/parse failures: (name, appearance_name,
        # error, flCompression-or-None). Dumped in full to a sibling
        # file at the end; the dialog only previews the first few.
        failures: list[tuple[str, str, str, int | None]] = []
        # name → appearance lookup so a failure recorded deep in the
        # decode loop (where only the bare name is in scope) can still
        # carry which actor bucket discovery pulled it from.
        appearance_by_name: dict[str, str] = {
            nm: (getattr(info, "appearance_name", "") or "")
            for info in self._anim_infos
            if (nm := (getattr(info, "name", "") or ""))
        }

        def _record(name: str, error: str) -> None:
            """Append a structured failure record (see ``failures``)."""
            failures.append((
                name,
                appearance_by_name.get(name, ""),
                error,
                _extract_compression_mode(error),
            ))

        # Pre-flight: the parser reads each anim's d4data .ani.json meta
        # (not the CASC-side .ani meta), so an info without it can't be
        # decoded regardless of what CASC returns. Drop those up front.
        pending: list[tuple[str, Path]] = []
        for i, info in enumerate(self._anim_infos, start=1):
            anim_name = getattr(info, "name", "") or f"anim_{i}"
            meta_json = getattr(info, "ani_meta_path", None)
            if meta_json is None or not Path(meta_json).is_file():
                log.warning("%s: missing meta JSON", anim_name)
                _record(anim_name, "missing meta JSON")
                continue
            pending.append((anim_name, Path(meta_json)))

        # Partition pending anims by whether their meta + payload .ani
        # are already in the shared cache. Cached ones skip CASC; the
        # rest are handed to the batched extractor in one call.
        cache_root = _anim_cache_root()
        cached_meta_dir = cache_root / "base" / "meta" / "Anim"
        cached_payload_dir = cache_root / "base" / "payload" / "Anim"

        payload_by_name: dict[str, Path] = {}
        needs_fetch: list[str] = []
        for anim_name, _meta_json in pending:
            meta_ani = cached_meta_dir / f"{anim_name}.ani"
            payload_ani = cached_payload_dir / f"{anim_name}.ani"
            if meta_ani.is_file() and payload_ani.is_file():
                payload_by_name[anim_name] = payload_ani
            else:
                needs_fetch.append(anim_name)

        if needs_fetch:
            if self.isInterruptionRequested():
                return
            self.progress.emit(
                f"Extracting {len(needs_fetch)} animation(s) from CASC…"
            )
            try:
                batch = rd.extract_anim_pair_batch(
                    needs_fetch, cache_root,
                    d4data_path=self._d4data_path,
                )
            except CASCExtractionError as exc:
                log.warning("Batched anim extraction failed: %s", exc)
                self.warning.emit(
                    f"Animation export skipped: CASC extraction "
                    f"failed ({exc})"
                )
                return
            except Exception as exc:
                log.exception("Batched anim extraction crashed")
                self.warning.emit(
                    f"Animation export skipped: {type(exc).__name__}: {exc}"
                )
                return
            for anim_name, (_meta, payload, _shared) in batch.items():
                payload_by_name[anim_name] = payload

        # Decode each anim in order. CASC extraction is done; this stage
        # is pure parse + decode and stays sequential.
        decoded_count = 0
        for i, (anim_name, meta_json) in enumerate(pending, start=1):
            if self.isInterruptionRequested():
                return
            self.progress.emit(
                f"Decoding animation {i}/{total}: {anim_name}…"
            )

            payload_path = payload_by_name.get(anim_name)
            if payload_path is None:
                error = "CASC extraction returned no payload"
                log.warning("%s: %s", anim_name, error)
                _record(anim_name, error)
                continue

            try:
                # Permutation 0 is the canonical first variant; covers
                # almost every appearance. Multi-permutation exports
                # would need a per-anim selector and aren't a feature
                # the GUI exposes today.
                perm = parse_anim(
                    meta_json, payload_path, permutation_index=0,
                )
            except AnimFormatError as exc:
                error = f"parse error ({exc})"
                log.warning("%s: %s", anim_name, error)
                _record(anim_name, error)
                continue
            except Exception as exc:
                log.exception("parse_anim crashed for %s", anim_name)
                _record(anim_name, f"{type(exc).__name__}: {exc}")
                continue

            try:
                decoded = decode_permutation(
                    perm, rest_pose=self._rest_pose_map,
                )
            except AnimFormatError as exc:
                error = f"decode error ({exc})"
                log.warning("%s: %s", anim_name, error)
                _record(anim_name, error)
                continue
            except Exception as exc:
                log.exception("decode_permutation crashed for %s", anim_name)
                _record(anim_name, f"{type(exc).__name__}: {exc}")
                continue

            self._animations.append(decoded)
            decoded_count += 1

        log.info(
            "Animation batch: decoded %d/%d (%d failed)",
            decoded_count, total, len(failures),
        )

        # Put a clickable, offset-free rest pose at animation index 0 —
        # Blender applies animation[0] as the default import pose.
        self._prepend_rest_pose_animation(real_anim_count=decoded_count)

        if failures:
            # Surface the count + first few failures so the user knows
            # something was dropped without flooding the dialog.
            preview = "; ".join(
                f"{name}: {error}"
                for name, _appe, error, _mode in failures[:3]
            )
            more = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
            self.warning.emit(
                f"{len(failures)} of {total} animation(s) failed to "
                f"decode: {preview}{more}"
            )

        # Persist the FULL failure list next to the export output — the
        # dialog above only previews the first few. This file is the
        # source of truth for which compression modes are still
        # unsupported. Best-effort: a dump write failure must not sink
        # an export that has otherwise succeeded.
        try:
            dump_path = _dump_decode_failures(failures, self._output_path)
        except OSError as exc:
            log.warning("Could not write decode-failures dump: %s", exc)
            dump_path = None
        if dump_path is not None:
            log.warning(
                "Decode failures: %d animation(s) could not be decoded. "
                "Full list written to %s", len(failures), dump_path,
            )
            # Also emit each line at DEBUG so a single log capture has
            # everything without needing to open the file.
            for name, appearance, error, mode in failures:
                log.debug(
                    "  decode failure: %s (appearance=%s, "
                    "flCompression=%s): %s",
                    name, appearance, mode if mode is not None else "?",
                    error,
                )

    def _prepend_rest_pose_animation(self, *, real_anim_count: int) -> None:
        """Insert a synthetic "rest_pose" Action at animation index 0.

        Blender's glTF importer applies ``animation[0]`` as the static
        pose shown on import. Cinematic clips (``Conv_*``, ``IGC_*``)
        carry a constant non-zero root translation; if one lands at
        index 0 the whole model imports offset from world origin.
        ``rest_pose`` has identity TRS on every bone, so placing it at
        index 0 guarantees the model imports at origin regardless of
        which other animations are present — and still gives Blender
        users a clickable Action to snap the rig back to bind. See
        docs/diagnostic-origin-offset.md.

        Skipped unless real animations are also being exported (nothing
        to switch back from on a static export), a skinned skeleton is
        present, and skin export is on.
        """
        if real_anim_count <= 0:
            return
        if not self._rest_pose_map:
            return
        if not bool(self._options.get("export_skin", True)):
            return
        skeleton = self._export_skeleton()
        if skeleton is None or not getattr(skeleton, "bones", None):
            return

        from d4extract.formats.anim_parser import build_rest_pose_animation

        rest_anim = build_rest_pose_animation(skeleton)
        rest_anim.force_static_channels = True
        self._animations.insert(0, rest_anim)

    def _wanted_texture_paths(self) -> list[str]:
        """Collect every CASC payload texture path this export references.

        Covers both the per-material textures and the customization
        variant textures (makeup / markings) discovered for the sidecar,
        so variant textures are pre-extracted alongside the rest.
        """
        wanted: set[str] = set()
        for mesh in self._export_meshes():
            for mat in getattr(mesh, "materials", None) or ():
                for tex in getattr(mat, "textures", []):
                    payload = tex.path.replace(
                        "base/meta/Texture/", "base/payload/Texture/", 1,
                    )
                    wanted.add(payload)
        if self._variants:
            from d4extract.formats.variants import all_variant_textures

            for tref in all_variant_textures(self._variants):
                payload = tref.path.replace(
                    "base/meta/Texture/", "base/payload/Texture/", 1,
                )
                wanted.add(payload)
        return sorted(wanted)

    def _count_landed(self, root: Path, wanted: list[str]) -> int:
        """How many of ``wanted`` actually live under ``root``."""
        if not wanted:
            return 0
        return sum(1 for p in wanted if (root / p).is_file())

    def _resolve_texture_dir(
        self, wanted: list[str],
    ) -> tuple[Path | None, str | None]:
        """Pick a texture root and verify referenced files exist there.

        Returns ``(path, None)`` on success, or ``(None, reason)`` when
        the caller should disable ``embed_textures`` and surface the
        reason as a warning. Tries the user-supplied dir first (CLI
        parity); falls back to auto-extraction from CASC.
        """
        if not wanted:
            return None, "no textures are referenced by this model"

        # 1) User-supplied pre-extracted root.
        if self._user_texture_dir is not None:
            if not self._user_texture_dir.is_dir():
                return None, (
                    f"configured texture directory does not exist: "
                    f"{self._user_texture_dir}"
                )
            landed = self._count_landed(self._user_texture_dir, wanted)
            if landed > 0:
                self.progress.emit(
                    f"Using pre-extracted textures "
                    f"({landed}/{len(wanted)} found in "
                    f"{self._user_texture_dir})"
                )
                return self._user_texture_dir, None
            # Configured but empty — try auto-extract as a fallback so
            # the user isn't stuck after typing the wrong path.
            log.info(
                "texture_dir %s contains 0 of %d referenced textures; "
                "falling back to auto-extract",
                self._user_texture_dir, len(wanted),
            )

        # 2) Auto-extract from CASC.
        if self._game_dir is None:
            return None, (
                "game directory is not configured (cannot auto-extract "
                "textures from CASC)"
            )
        try:
            cache_root = self._auto_extract_textures(wanted)
        except _TextureExtractError as exc:
            return None, str(exc)

        landed = self._count_landed(cache_root, wanted)
        if landed == 0:
            return None, (
                f"auto-extraction landed 0/{len(wanted)} referenced "
                f"textures on disk (rustydemon may not have access to "
                f"these payloads)"
            )
        if landed < len(wanted):
            log.info(
                "auto-extract: %d of %d textures landed under %s",
                landed, len(wanted), cache_root,
            )
        return cache_root, None

    def _auto_extract_textures(self, wanted: list[str]) -> Path:
        """Extract ``wanted`` payloads into a per-stem cache root.

        On extraction failure this still **returns the cache root** so
        the caller can use whatever was already cached from a previous
        run; the caller's ``_count_landed`` check decides whether the
        result is usable. Only pre-conditions that make extraction
        impossible (no game dir, no rustydemon, unwritable cache) raise
        ``_TextureExtractError``.
        """
        if self._game_dir is None:
            raise _TextureExtractError("game directory not configured")

        stem = self._export_stem()
        cache_root = _texture_cache_root() / stem
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _TextureExtractError(f"cache directory unwritable: {exc}")

        missing = [p for p in wanted if not (cache_root / p).exists()]
        if not missing:
            return cache_root

        try:
            from d4extract.casc.rustydemon import (
                CASCExtractionError,
                RustyDemonCLI,
            )
        except Exception as exc:
            raise _TextureExtractError(f"rustydemon unavailable: {exc}")

        self.progress.emit(f"Extracting {len(missing)} texture(s) from CASC…")

        try:
            rd = RustyDemonCLI(self._game_dir)
        except CASCExtractionError as exc:
            # No binary, or the game dir doesn't pass validation. The
            # cache is still usable if a previous run populated it, so
            # we surface the error as a non-fatal log line and let
            # _count_landed decide. (If the user's first export hits
            # this, they'll see "0 textures landed" and a clear warning.)
            log.warning("RustyDemonCLI unavailable: %s", exc)
            return cache_root

        try:
            rd.extract_many(missing, cache_root, group_size=80, workers=4)
        except CASCExtractionError as exc:
            # Partial failure: extraction couldn't fetch every missing
            # tex (e.g. one path isn't in this CASC build, or rustydemon
            # bailed midway). Whatever already landed in cache_root is
            # still usable — log the error for visibility but don't
            # discard the partial result.
            log.warning("CASC extraction partial failure: %s", exc)

        return cache_root


class _TextureExtractError(RuntimeError):
    """Internal signal that auto-extraction failed but the export can continue."""
