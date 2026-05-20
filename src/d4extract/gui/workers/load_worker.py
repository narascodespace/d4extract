"""Background worker that extracts + parses a single .app model."""

from __future__ import annotations

import logging
import math
import tempfile
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThread, Signal

log = logging.getLogger(__name__)


def _extracted_cache_root() -> Path:
    """Per-user dir where extracted .app pairs are cached between clicks."""
    base = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
    return Path(base) / "d4extract" / "extracted"


def _stem_from_sno(sno_path: str) -> str:
    """Extract the model stem from a CASC SNO path.

    ``base/meta/Appearance/foo_bar.app`` → ``foo_bar``.
    """
    name = sno_path.rsplit("/", 1)[-1]
    if name.endswith(".app"):
        name = name[:-4]
    return name


def _build_uv_sphere(
    center: tuple[float, float, float],
    radius: float,
    stacks: int,
    slices: int,
    base_index: int,
):
    """Generate a UV-sphere chunk in D4-native (LH Z-up) coordinates.

    Returns ``(positions, normals, indices)`` where every index is offset
    by ``base_index`` so the chunk can be concatenated into a larger
    vertex stream without colliding.
    """
    positions: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float, float]] = []
    indices: list[int] = []

    cx, cy, cz = center
    for i in range(stacks + 1):
        v = i / stacks
        phi = math.pi * v
        sin_p = math.sin(phi)
        cos_p = math.cos(phi)
        for j in range(slices + 1):
            u = j / slices
            theta = 2.0 * math.pi * u
            nx = sin_p * math.cos(theta)
            ny = sin_p * math.sin(theta)
            nz = cos_p
            positions.append((cx + radius * nx, cy + radius * ny, cz + radius * nz))
            normals.append((nx, ny, nz, 1.0))

    def vid(i: int, j: int) -> int:
        return base_index + i * (slices + 1) + j

    for i in range(stacks):
        for j in range(slices):
            a = vid(i, j)
            b = vid(i, j + 1)
            c = vid(i + 1, j)
            d = vid(i + 1, j + 1)
            indices.extend([a, c, b, b, c, d])

    return positions, normals, indices


def build_synthetic_mesh(name: str):
    """Create a two-sphere ``MeshData`` for mock-mode rendering.

    Two side-by-side UV spheres, each owning a disjoint slice of the
    vertex and index streams. This makes per-submesh coloring visible in
    the viewport AND lets the GltfExporter localize indices cleanly
    (each submesh's indices stay within its own vertex range).

    Positions are D4-native (LH Z-up) so the viewport's coordinate
    conversion path is exercised end-to-end.
    """
    from d4extract.formats.app_parser import MeshData, Submesh

    stacks, slices = 12, 18

    pos_a, nrm_a, idx_a = _build_uv_sphere(
        center=(-1.1, 0.0, 0.0), radius=0.9,
        stacks=stacks, slices=slices, base_index=0,
    )
    n_verts_a = len(pos_a)
    n_idx_a = len(idx_a)

    pos_b, nrm_b, idx_b = _build_uv_sphere(
        center=(1.1, 0.0, 0.0), radius=0.9,
        stacks=stacks, slices=slices, base_index=n_verts_a,
    )
    n_verts_b = len(pos_b)
    n_idx_b = len(idx_b)

    positions = pos_a + pos_b
    normals = nrm_a + nrm_b
    indices = idx_a + idx_b

    sub_a = Submesh(
        vertex_offset=0,
        vertex_count=n_verts_a,
        index_offset=0,
        index_count=n_idx_a,
        material_index=0,
    )
    sub_b = Submesh(
        vertex_offset=n_verts_a,
        vertex_count=n_verts_b,
        index_offset=n_idx_a,
        index_count=n_idx_b,
        material_index=1,
    )

    return MeshData(
        name=name,
        vertex_count=n_verts_a + n_verts_b,
        index_count=n_idx_a + n_idx_b,
        submesh_count=2,
        positions=positions,
        normals=normals,
        indices=indices,
        submeshes=[sub_a, sub_b],
    )


class LoadModelWorker(QThread):
    """Extract a model pair from CASC and parse it into ``MeshData``.

    ``finished`` carries the parsed ``MeshData`` (typed as ``object`` to
    keep the signal annotation stable across model versions). ``progress``
    emits user-facing status strings; ``error`` emits a single failure
    message — the worker exits after either ``finished`` or ``error``.
    """

    finished = Signal(object)
    progress = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        game_dir: Path | None,
        sno_path: str,
        *,
        mock: bool = False,
        d4data_path: Path | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._game_dir = Path(game_dir) if game_dir is not None else None
        self._d4data_path = (
            Path(d4data_path) if d4data_path is not None else None
        )
        # Public so the GUI can key its in-memory cache off the same path
        # the worker was started for, without depending on identity of
        # ``current_sno_path`` (which can have moved on by the time the
        # finished signal fires).
        self.sno_path = sno_path
        self._mock = mock

    # ------------------------------------------------------------------
    # QThread.run
    # ------------------------------------------------------------------

    def run(self) -> None:  # noqa: D401 — Qt API
        try:
            if self._mock:
                self._run_mock()
            else:
                self._run_real()
        except Exception as exc:  # last-resort safety net
            log.exception("LoadModelWorker crashed")
            self.error.emit(f"{type(exc).__name__}: {exc}")

    def _run_mock(self) -> None:
        self.progress.emit("Generating synthetic mesh…")
        stem = _stem_from_sno(self.sno_path)
        mesh = build_synthetic_mesh(stem)
        self.finished.emit(mesh)

    def _run_real(self) -> None:
        if self._game_dir is None:
            self.error.emit("No game directory configured.")
            return

        stem = _stem_from_sno(self.sno_path)

        self.progress.emit("Extracting from CASC…")

        # Imported lazily — keeps a missing rustydemon binary out of the
        # main-thread import path until the user actually clicks a model.
        try:
            from d4extract.casc.rustydemon import (
                CASCExtractionError,
                RustyDemonCLI,
            )
            from d4extract.formats.app_parser import (
                AppFormatError,
                parse_app,
            )
        except Exception as exc:
            self.error.emit(f"Failed to import extraction modules: {exc}")
            return

        # Per-stem cache dir — re-clicking the same model reuses extracted
        # files. Falling back to a temp dir keeps the worker working even
        # if the cache location is unwritable.
        try:
            cache_root = _extracted_cache_root() / stem
            cache_root.mkdir(parents=True, exist_ok=True)
            output_dir = cache_root
        except OSError:
            output_dir = Path(tempfile.mkdtemp(prefix="d4export_"))

        try:
            rd = RustyDemonCLI(self._game_dir)
            meta_path, data_path, is_shared = rd.extract_model_pair(
                stem, output_dir, d4data_path=self._d4data_path,
            )
        except CASCExtractionError as exc:
            self.error.emit(str(exc))
            return

        if self.isInterruptionRequested():
            return

        self.progress.emit("Parsing model…")
        try:
            mesh = parse_app(
                meta_path, data_path, allow_link_mismatch=is_shared,
            )
        except AppFormatError as exc:
            self.error.emit(f"Parse error: {exc}")
            return

        if self.isInterruptionRequested():
            return

        self.finished.emit(mesh)
