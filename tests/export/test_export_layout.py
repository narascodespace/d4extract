"""Tests for the per-model export folder layout + loose-texture dump.

Both the GUI worker and the CLI go through
``gltf_export.resolve_export_paths`` to pick the final on-disk layout
(``<root>/<stem>/<stem>.<fmt>`` plus a sibling ``textures/`` for
parallel-output PNGs). These tests exercise:

* the helper itself — tuple contents + idempotent directory creation
  + format validation,
* :class:`TexturePack`'s parallel PNG dump — happy path, name-collision
  suffix, and the off-by-default branch,
* an end-to-end ``GltfExporter.export`` call to verify the on-disk
  shape matches the helper's contract for both ``.glb`` and ``.gltf``
  formats.

No CASC / decoded-texture data is needed — the loose-dump path is
exercised directly through :class:`TexturePack._embed_png`, which is
the choke point every per-role embed funnels through.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from d4extract.export.gltf_export import (
    GltfExporter,
    TexturePack,
    resolve_export_paths,
)
from d4extract.formats.app_parser import MeshData


# ── helpers ──────────────────────────────────────────────────────────


def _png_bytes(color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """A tiny solid-color PNG suitable for round-tripping through embed."""
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, "PNG")
    return buf.getvalue()


def _empty_pack(loose_dir: Path | None) -> TexturePack:
    """Construct a TexturePack with empty backing lists.

    The pack only needs the binary / buffer-view / image / texture /
    sampler lists for ``_embed_png`` to mutate; it doesn't care about
    the texture_dir contents because we never call ``_decode``.
    """
    return TexturePack(
        texture_dir=Path("."),         # unused — we don't call _decode
        binary=bytearray(),
        buffer_views=[],
        images=[],
        textures=[],
        samplers=[],
        loose_textures_dir=loose_dir,
    )


def _triangle_model(name: str = "triangle") -> MeshData:
    """Minimal model — geometry only, no materials so no textures."""
    return MeshData(
        name=name,
        vertex_count=3,
        index_count=3,
        submesh_count=0,
        positions=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        indices=[0, 1, 2],
    )


# ── resolve_export_paths ─────────────────────────────────────────────


class TestResolveExportPaths:
    def test_glb_layout(self, tmp_path: Path) -> None:
        model_dir, model_file, textures_dir = resolve_export_paths(
            tmp_path, "lilith_scaled", "glb",
        )
        assert model_dir == tmp_path / "lilith_scaled"
        assert model_file == tmp_path / "lilith_scaled" / "lilith_scaled.glb"
        assert textures_dir == tmp_path / "lilith_scaled" / "textures"
        assert model_dir.is_dir()
        assert textures_dir.is_dir()
        # The file itself is *not* created — only the directories.
        assert not model_file.exists()

    def test_gltf_layout(self, tmp_path: Path) -> None:
        _, model_file, textures_dir = resolve_export_paths(
            tmp_path, "necF_helm", "gltf",
        )
        assert model_file.name == "necF_helm.gltf"
        assert textures_dir.name == "textures"
        assert textures_dir.parent == model_file.parent

    def test_accepts_dot_prefixed_fmt(self, tmp_path: Path) -> None:
        """Callers sometimes have ``.glb`` (with the dot) on hand — accept
        it as a convenience instead of forcing a strip on every site."""
        _, model_file, _ = resolve_export_paths(tmp_path, "m", ".glb")
        assert model_file.suffix == ".glb"

    def test_rejects_unknown_fmt(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="fmt must be"):
            resolve_export_paths(tmp_path, "m", "obj")

    def test_idempotent_directory_creation(self, tmp_path: Path) -> None:
        """Calling twice with the same args is a no-op on the second
        round — the second export into the same folder reuses the dirs
        rather than failing on the existence check."""
        resolve_export_paths(tmp_path, "m", "glb")
        # Pre-populate the textures dir with something to make sure the
        # second call doesn't wipe / recreate it.
        (tmp_path / "m" / "textures" / "marker.txt").write_text("keep")
        resolve_export_paths(tmp_path, "m", "glb")
        assert (tmp_path / "m" / "textures" / "marker.txt").read_text() == "keep"


# ── loose-texture dump (TexturePack._embed_png) ───────────────────────


class TestLooseTextureDump:
    def test_writes_png_to_disk(self, tmp_path: Path) -> None:
        pack = _empty_pack(tmp_path)
        pack._embed_png(_png_bytes(), "BASE_COLOR_42", loose_stem="body_color")
        assert (tmp_path / "body_color.png").is_file()
        # Sanity-check that the bytes round-trip — should be the same
        # PNG payload we passed in.
        png_on_disk = (tmp_path / "body_color.png").read_bytes()
        assert png_on_disk[:8] == b"\x89PNG\r\n\x1a\n"

    def test_collision_appends_numeric_suffix(self, tmp_path: Path) -> None:
        """Two distinct textures share a basename → second/third get
        ``_1`` / ``_2`` suffixes rather than silently overwriting."""
        pack = _empty_pack(tmp_path)
        pack._embed_png(
            _png_bytes((255, 0, 0)), "BASE_COLOR_1", loose_stem="shared",
        )
        pack._embed_png(
            _png_bytes((0, 255, 0)), "BASE_COLOR_2", loose_stem="shared",
        )
        pack._embed_png(
            _png_bytes((0, 0, 255)), "BASE_COLOR_3", loose_stem="shared",
        )
        files = sorted(p.name for p in tmp_path.glob("*.png"))
        assert files == ["shared.png", "shared_1.png", "shared_2.png"]
        # And the bytes must actually differ — not three copies of the
        # last one through accidental overwriting.
        first = (tmp_path / "shared.png").read_bytes()
        second = (tmp_path / "shared_1.png").read_bytes()
        third = (tmp_path / "shared_2.png").read_bytes()
        assert {first, second, third} == {first, second, third}  # all distinct
        assert len({first, second, third}) == 3

    def test_strips_directory_components_from_stem(self, tmp_path: Path) -> None:
        """A caller passing a full ``base/meta/Texture/foo.tex`` path by
        accident shouldn't escape ``loose_textures_dir`` — only the
        basename's stem ever lands as the filename."""
        pack = _empty_pack(tmp_path)
        pack._embed_png(
            _png_bytes(), "x",
            loose_stem="base/meta/Texture/foo.tex",
        )
        assert (tmp_path / "foo.png").is_file()
        # No subdirectory was created.
        assert not (tmp_path / "base").exists()

    def test_no_dump_when_dir_unset(self, tmp_path: Path) -> None:
        """The pre-existing embedding path is unaffected when callers
        haven't opted into the loose dump — no PNGs land anywhere."""
        pack = _empty_pack(loose_dir=None)
        pack._embed_png(_png_bytes(), "BASE_COLOR_42")
        # tmp_path stayed empty — the test only fails if a PNG leaked
        # somewhere it shouldn't.
        assert list(tmp_path.iterdir()) == []
        # The in-glb embedding still happened.
        assert len(pack.images) == 1


# ── end-to-end through GltfExporter.export ────────────────────────────


class TestGltfExporterLayout:
    def test_glb_lands_in_model_dir(self, tmp_path: Path) -> None:
        """``GltfExporter.export`` writes the .glb to the resolved
        ``model_file`` path — no auto-rewriting of the user-provided
        output path that could land the file elsewhere."""
        _, model_file, textures_dir = resolve_export_paths(
            tmp_path, "triangle", "glb",
        )
        exporter = GltfExporter(
            export_normals=False, export_tangents=False,
            export_uvs=False, export_colors=False, export_skin=False,
            write_materials_sidecar=False,
        )
        out = exporter.export(_triangle_model(), model_file)
        assert out == model_file
        assert model_file.is_file()
        # No materials → no textures embedded → textures/ remains empty.
        # (Empty dir survives; the worker leaves it created.)
        assert textures_dir.is_dir()
        assert list(textures_dir.iterdir()) == []

    def test_gltf_writes_bin_sibling_in_model_dir(self, tmp_path: Path) -> None:
        """The split-file format puts the buffer next to the .gltf, both
        inside the model_dir — confirming pygltflib's relative-uri
        scheme stays within the per-model folder."""
        _, model_file, _ = resolve_export_paths(tmp_path, "triangle", "gltf")
        exporter = GltfExporter(
            export_normals=False, export_tangents=False,
            export_uvs=False, export_colors=False, export_skin=False,
            write_materials_sidecar=False,
        )
        out = exporter.export(_triangle_model(), model_file)
        assert out == model_file
        assert model_file.is_file()
        # pygltflib writes ``<stem>.bin`` next to the .gltf — verify it
        # didn't escape the model folder.
        bin_path = model_file.with_suffix(".bin")
        assert bin_path.is_file()
        assert bin_path.parent == model_file.parent

    def test_repeated_export_overwrites_in_place(self, tmp_path: Path) -> None:
        """Two exports with the same stem land at the same file path —
        the second overwrites the first rather than colliding or
        creating an ``<stem>_1`` sibling. Cross-export collisions are
        the user's intent (re-exporting); intra-export name conflicts
        in the textures/ folder are what the suffix logic guards
        against."""
        _, model_file, _ = resolve_export_paths(tmp_path, "triangle", "glb")
        exporter = GltfExporter(
            export_normals=False, export_tangents=False,
            export_uvs=False, export_colors=False, export_skin=False,
            write_materials_sidecar=False,
        )
        exporter.export(_triangle_model(), model_file)
        first_size = model_file.stat().st_size
        # Re-export — same path, no error.
        exporter.export(_triangle_model("triangle"), model_file)
        # Still exactly one .glb in the folder, with the same name.
        glbs = list(model_file.parent.glob("*.glb"))
        assert glbs == [model_file]
        # The on-disk file is freshly written (size matches a clean export).
        assert model_file.stat().st_size == first_size

    def test_sidecar_lands_next_to_glb(self, tmp_path: Path) -> None:
        """When materials are present the ``.materials.json`` sidecar
        lands in the same model_dir as the .glb — Blender's addon reads
        relative to the .glb so this co-location is what makes the
        addon import work without extra configuration."""
        from d4extract.formats.material_parser import Material as D4Material

        _, model_file, _ = resolve_export_paths(tmp_path, "triangle", "glb")
        model = _triangle_model()
        # One minimal resolved material is enough to trigger sidecar
        # writing — content is opaque to this test, only the file's
        # presence + location matters.
        model.materials = [
            D4Material(
                sno_id=1, name="Material_0", textures=[],
                shader_map="basic",
                base_color_factor=(1.0, 1.0, 1.0, 1.0),
                emissive_factor=(0.0, 0.0, 0.0),
                is_cloth_only=False,
            ),
        ]
        exporter = GltfExporter(
            export_normals=False, export_tangents=False,
            export_uvs=False, export_colors=False, export_skin=False,
            write_materials_sidecar=True,
        )
        exporter.export(model, model_file)
        sidecar = model_file.with_suffix(".materials.json")
        assert sidecar.is_file()
        assert sidecar.parent == model_file.parent
