"""Background worker that resolves base-color textures for the viewport.

Mirrors a slice of the export-worker logic: walks ``d4data`` JSON for the
loaded model's materials, extracts the BASE_COLOR ``.tex`` payloads from
CASC via :class:`RustyDemonCLI`, decodes them through
:mod:`d4extract.formats.texture_parser`, and emits a per-submesh
``{idx: numpy_uint8_rgb_array}`` map back to the GUI thread.

The viewport uses these arrays to attach VTK textures to each submesh's
PolyData actor. Anything that fails to resolve, extract, or decode is
silently dropped so the viewport falls back to flat per-submesh
colouring for those primitives — a single bad texture must not poison
the whole batch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QStandardPaths, QThread, Signal

log = logging.getLogger(__name__)


def _texture_cache_root() -> Path:
    """Same on-disk cache the export worker writes to.

    Sharing the cache means the second click on a model (load → texture
    decode → user picks Export GLB) doesn't re-extract the same
    payloads.
    """
    base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
    return Path(base) / "d4extract" / "textures"


class TextureWorker(QThread):
    """Pull base-color textures off-thread and hand them to the viewport.

    ``finished`` carries ``(mesh_token, submesh_textures)`` where
    ``mesh_token`` is the same opaque object passed at construction
    (the GUI uses it to ignore stale results from prior selections) and
    ``submesh_textures`` maps a submesh index to a contiguous
    ``uint8`` ``(H, W, 3)`` numpy array. Submeshes whose texture failed
    are simply absent from the dict.
    """

    # Both payloads pass through as raw ``PyObject *`` — declaring the
    # second arg as ``dict`` would coerce it to ``QVariantMap`` on emit,
    # and QVariantMap requires string keys plus QVariant-compatible
    # values. Our payload is ``dict[int, numpy.ndarray]`` (integer
    # submesh indices keying RGB image arrays), neither of which
    # survive the conversion: the previous version of this file
    # silently dropped every entry on emit so the viewport always
    # rendered with its flat-color fallback.
    finished = Signal(object, object)
    progress = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        mesh_data: Any,
        *,
        game_dir: Path,
        d4data_path: Path,
        mesh_token: object,
        sno_path: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._mesh = mesh_data
        self._game_dir = Path(game_dir)
        self._d4data_path = Path(d4data_path)
        self._mesh_token = mesh_token
        # Public so the GUI cache can attribute the emitted texture dict
        # back to the SNO path the worker was started for.
        self.sno_path = sno_path

    # ------------------------------------------------------------------
    # QThread.run
    # ------------------------------------------------------------------

    def run(self) -> None:  # noqa: D401 — Qt API
        try:
            self._run_inner()
        except Exception as exc:
            log.exception("TextureWorker crashed")
            self.error.emit(f"{type(exc).__name__}: {exc}")

    def _run_inner(self) -> None:
        from d4extract.formats.material_parser import (
            MaterialResolutionError,
            load_materials,
        )
        from d4extract.formats.texture_parser import (
            TextureDecodeError,
            decode_tex,
            texture_payload_path,
        )

        stem = getattr(self._mesh, "name", None) or ""
        submeshes = list(getattr(self._mesh, "submeshes", []) or [])
        if not stem or not submeshes:
            log.info(
                "TextureWorker bail: stem=%r submeshes=%d",
                stem, len(submeshes),
            )
            self.finished.emit(self._mesh_token, {})
            return
        log.info(
            "TextureWorker start: stem=%r submeshes=%d", stem, len(submeshes),
        )

        # 1. Resolve materials. Reuse anything already attached to the
        # MeshData (e.g. a prior export run resolved them) so we don't
        # walk the d4data tree twice for the same model.
        materials = getattr(self._mesh, "materials", None)
        if not materials:
            self.progress.emit("Resolving materials…")
            try:
                materials = load_materials(stem, self._d4data_path)
            except MaterialResolutionError as exc:
                log.info("Material resolution skipped: %s", exc)
                self.finished.emit(self._mesh_token, {})
                return
            except Exception as exc:
                log.warning("Material resolution crashed: %s", exc)
                self.finished.emit(self._mesh_token, {})
                return
            # Cache on the MeshData so a follow-up export doesn't redo
            # the work. Same write the export worker does.
            try:
                self._mesh.materials = materials
            except AttributeError:
                pass

        if self.isInterruptionRequested():
            return
        log.info(
            "TextureWorker materials resolved: count=%d", len(materials),
        )

        # 2. First BASE_COLOR ref per material slot. SLOT_ROLES tags
        # both the canonical creature/character slots (1/19) and the
        # layered-terrain-shader slots (11/13) as BASE_COLOR, so a
        # plain role match handles environment statics too. Materials
        # with no albedo binding (cloth-only / textureless eye shaders)
        # are skipped — the viewport keeps the flat-color fallback for
        # those primitives.
        base_refs: dict[int, Any] = {}
        for idx, mat in enumerate(materials):
            for tex in getattr(mat, "textures", []) or []:
                if tex.role == "BASE_COLOR" and tex.path:
                    base_refs[idx] = tex
                    break
        log.info(
            "TextureWorker BASE_COLOR refs: %d/%d material slots",
            len(base_refs), len(materials),
        )
        if not base_refs:
            self.finished.emit(self._mesh_token, {})
            return

        # 3. Extract any payloads not already on disk.
        wanted = sorted({
            ref.path.replace("base/meta/Texture/", "base/payload/Texture/", 1)
            for ref in base_refs.values()
        })
        cache_root = _texture_cache_root() / stem
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("texture cache unwritable: %s", exc)
            self.finished.emit(self._mesh_token, {})
            return

        missing = [p for p in wanted if not (cache_root / p).is_file()]
        if missing:
            self.progress.emit(
                f"Extracting {len(missing)} viewport texture(s)…"
            )
            try:
                from d4extract.casc.rustydemon import (
                    CASCExtractionError,
                    RustyDemonCLI,
                )
                rd = RustyDemonCLI(self._game_dir)
                rd.extract_many(missing, cache_root, group_size=80, workers=4)
            except CASCExtractionError as exc:
                # Partial-failure tolerance: whatever already landed in
                # cache (from this run or a prior one) is still usable;
                # textures that genuinely couldn't be fetched will simply
                # fall through to flat-color rendering below.
                log.info("Viewport texture extraction partial failure: %s", exc)
            except Exception as exc:
                log.warning("Viewport texture extraction crashed: %s", exc)

        if self.isInterruptionRequested():
            return

        # 4. Decode each unique payload once, keyed by absolute path so
        # two materials sharing the same source texture share the array.
        self.progress.emit("Decoding textures…")
        decoded_by_path: dict[Path, "np.ndarray | None"] = {}
        for ref in base_refs.values():
            payload = texture_payload_path(cache_root, ref.path)
            if payload in decoded_by_path:
                continue
            if not payload.is_file():
                decoded_by_path[payload] = None
                continue
            try:
                img = decode_tex(payload, ref.width, ref.height, ref.format)
            except TextureDecodeError as exc:
                log.info("texture decode skipped: %s", exc)
                decoded_by_path[payload] = None
                continue
            except Exception as exc:
                log.warning("texture decode crashed (%s): %s", payload.name, exc)
                decoded_by_path[payload] = None
                continue
            if img.mode != "RGB":
                img = img.convert("RGB")
            arr = np.asarray(img, dtype=np.uint8)
            # D4 stores UVs in DirectX convention (V=0 at the top) and
            # the parser passes them through verbatim — same as the glTF
            # exporter does. VTK's pv.Texture samples with V=0 at the
            # bottom (OpenGL convention), so flipping the image rows
            # here lands the texture right-side-up in the viewport
            # without having to touch every UV.
            decoded_by_path[payload] = np.ascontiguousarray(arr[::-1])

        # 5. Map each submesh to its decoded array (if any).
        submesh_textures: dict[int, np.ndarray] = {}
        for sm_idx, sm in enumerate(submeshes):
            ref = base_refs.get(sm.material_index)
            if ref is None:
                continue
            payload = texture_payload_path(cache_root, ref.path)
            arr = decoded_by_path.get(payload)
            if arr is not None:
                submesh_textures[sm_idx] = arr

        if self.isInterruptionRequested():
            return
        log.info(
            "TextureWorker emit: %d submeshes textured (keys=%s)",
            len(submesh_textures), sorted(submesh_textures.keys()),
        )
        self.finished.emit(self._mesh_token, submesh_textures)
