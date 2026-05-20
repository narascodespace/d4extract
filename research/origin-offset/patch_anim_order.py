"""Origin-offset hypothesis test: move 'rest_pose' to animation index 0.

Blender applies the animation at index 0 as the default Action on glTF
import. The broken exports have a cutscene (with baked-in root-bone
translation) at index 0. This patches the OUTPUT glb only — it reorders
the ``animations`` array so the synthetic in-place ``rest_pose`` Action
is index 0, leaving every accessor / sampler / channel untouched (each
Animation is self-contained; reordering the list is index-safe).

If importing the patched glb shows the character at world origin, the
"wrong default action" hypothesis is confirmed.

Usage:  patch_anim_order.py <in.glb> <out.glb>
"""

from __future__ import annotations

import sys

from pygltflib import GLTF2


def patch(in_path: str, out_path: str) -> None:
    g = GLTF2.load(in_path)
    anims = g.animations or []
    rest_idx = next(
        (i for i, a in enumerate(anims)
         if (a.name or "").lower().startswith("rest")),
        None,
    )
    if rest_idx is None:
        print(f"  no 'rest_pose' animation found in {in_path}")
        return
    print(f"  {in_path}: 'rest_pose' was at index {rest_idx} "
          f"({len(anims)} anims); index 0 was {anims[0].name!r}")
    rest = anims.pop(rest_idx)
    anims.insert(0, rest)
    g.animations = anims
    g.save(out_path)
    print(f"  wrote {out_path}: index 0 is now {g.animations[0].name!r}")


if __name__ == "__main__":
    patch(sys.argv[1], sys.argv[2])
