"""Tests for ExportWorker's synthetic rest-pose insertion.

The rest-pose Action is inserted at animation index 0 — but only when
it's useful: real animations are also being exported, the model is
skinned, and skin export is enabled. Index 0 matters because Blender's
glTF importer applies ``animation[0]`` as the default pose on import; a
cinematic clip with root motion there would offset the model from world
origin (see docs/diagnostic-origin-offset.md).
``_prepend_rest_pose_animation`` is exercised here in isolation: the
worker is built via ``__new__`` so ``QThread.__init__`` (and therefore a
QApplication / event loop) is never needed.
"""

from __future__ import annotations

from types import SimpleNamespace

from d4extract.formats.app_parser import Bone, BoneTransform, Skeleton
from d4extract.gui.workers.export_worker import ExportWorker


_REST = {0xAAA: ((0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))}


def _skeleton() -> Skeleton:
    trs = BoneTransform(q=(0.0, 0.0, 0.0, 1.0), wp=(0.0, 0.0, 0.0),
                        scale=(1.0, 1.0, 1.0))
    return Skeleton(
        bones=[
            Bone(index=0, parent_index=-1, name_hash=0xAAA, flags=0, lod=0,
                 local_trs=trs, inv_bind_trs=trs),
            Bone(index=1, parent_index=0, name_hash=0xBBB, flags=0, lod=0,
                 local_trs=trs, inv_bind_trs=trs),
        ],
        base_bone_count=2, cloth_bone_count=0, template_id=1,
    )


def _worker(*, skeleton=None, rest_pose_map=None, options=None) -> ExportWorker:
    """Build an ExportWorker without running QThread.__init__.

    The gating only reads plain attributes, so bypassing __init__ keeps
    the test free of a QApplication.
    """
    worker = ExportWorker.__new__(ExportWorker)
    worker._mesh = SimpleNamespace(name="m", skeleton=skeleton)
    worker._rest_pose_map = rest_pose_map or {}
    worker._options = options or {}
    worker._animations = []
    return worker


class TestRestPoseGating:
    def test_inserted_when_real_animations_decoded(self) -> None:
        worker = _worker(skeleton=_skeleton(), rest_pose_map=_REST)
        worker._prepend_rest_pose_animation(real_anim_count=3)

        assert len(worker._animations) == 1
        rest = worker._animations[0]
        assert rest.name == "rest_pose"
        assert rest.force_static_channels is True
        assert len(rest.bone_animations) == 2

    def test_not_inserted_without_real_animations(self) -> None:
        """No real animations → nothing to switch back from → no rest pose."""
        worker = _worker(skeleton=_skeleton(), rest_pose_map=_REST)
        worker._prepend_rest_pose_animation(real_anim_count=0)
        assert worker._animations == []

    def test_not_inserted_without_rest_pose_map(self) -> None:
        """Empty rest-pose map means an unskinned export — no rig to pose."""
        worker = _worker(skeleton=_skeleton(), rest_pose_map={})
        worker._prepend_rest_pose_animation(real_anim_count=3)
        assert worker._animations == []

    def test_not_inserted_without_skeleton(self) -> None:
        worker = _worker(skeleton=None, rest_pose_map=_REST)
        worker._prepend_rest_pose_animation(real_anim_count=3)
        assert worker._animations == []

    def test_not_inserted_when_skin_disabled(self) -> None:
        worker = _worker(
            skeleton=_skeleton(), rest_pose_map=_REST,
            options={"export_skin": False},
        )
        worker._prepend_rest_pose_animation(real_anim_count=3)
        assert worker._animations == []


class TestRestPoseOrdering:
    """Index-0 placement — the world-origin-offset regression.

    Blender applies animation[0] as the default import pose, so the
    synthetic rest_pose must land at index 0, ahead of any cinematic
    clip with baked-in root translation. See
    docs/diagnostic-origin-offset.md.
    """

    def test_rest_pose_inserted_at_index_zero(self) -> None:
        """rest_pose lands at index 0, ahead of already-decoded clips,
        and the existing clips keep their relative order."""
        worker = _worker(skeleton=_skeleton(), rest_pose_map=_REST)
        worker._animations = [
            SimpleNamespace(name="cinematic_with_root"),
            SimpleNamespace(name="gameplay_attack"),
        ]
        worker._prepend_rest_pose_animation(real_anim_count=2)

        assert len(worker._animations) == 3
        assert worker._animations[0].name == "rest_pose"
        assert worker._animations[1].name == "cinematic_with_root"
        assert worker._animations[2].name == "gameplay_attack"

    def test_rest_pose_skip_leaves_existing_animations_untouched(self) -> None:
        """When the rest pose is gated off (here: no rest-pose map), a
        pre-decoded animation list is returned unmodified — index 0 stays
        whatever was decoded first."""
        worker = _worker(skeleton=_skeleton(), rest_pose_map={})
        worker._animations = [SimpleNamespace(name="some_anim")]
        worker._prepend_rest_pose_animation(real_anim_count=3)

        assert len(worker._animations) == 1
        assert worker._animations[0].name == "some_anim"
