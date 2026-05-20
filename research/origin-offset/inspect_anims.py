"""Origin-offset diagnostic: root/hips translation in glTF animations.

Blender applies one imported Action by default after a glTF import. If
that Action drives the root or hips bone with a non-zero translation
(a locomotion / cutscene / death clip with root motion), the character
appears displaced from world origin even though the rest pose is clean.

This dumps, per glb: every animation's name + index, and for the first
few animations the root-bone and hips-bone translation channels with
their frame-0 and frame-last values.
"""

from __future__ import annotations

import struct
import sys

from pygltflib import GLTF2


def _decode_vec3(g: GLTF2, blob: bytes, acc_idx: int) -> list[tuple]:
    acc = g.accessors[acc_idx]
    bv = g.bufferViews[acc.bufferView]
    base = (bv.byteOffset or 0) + (acc.byteOffset or 0)
    out = []
    for i in range(acc.count):
        off = base + i * 12
        out.append(struct.unpack("<3f", blob[off:off + 12]))
    return out


def inspect(path: str) -> None:
    g = GLTF2.load(path)
    blob = g.binary_blob()
    print("=" * 72)
    print(f"FILE: {path}")

    skin = g.skins[0]
    root_node = skin.joints[0]
    hips_node = skin.joints[1] if len(skin.joints) > 1 else None
    watch = {root_node: "ROOT", hips_node: "HIPS"}
    print(f"  root bone node={root_node} ({g.nodes[root_node].name!r}), "
          f"hips bone node={hips_node} ({g.nodes[hips_node].name!r})")

    anims = g.animations or []
    print(f"  {len(anims)} animations")

    # Find the synthetic rest_pose index, if present.
    for ai, a in enumerate(anims):
        if (a.name or "").lower().startswith("rest"):
            print(f"  rest-pose animation at index {ai}: {a.name!r}")

    print(f"\n  ALL ANIMATION NAMES (index: name):")
    for ai, a in enumerate(anims):
        print(f"    [{ai:3}] {a.name!r}")

    # Inspect root/hips translation channels for the first 3 + the last
    # (the synthetic rest pose usually lands last).
    probe = list(range(min(3, len(anims))))
    if len(anims) - 1 not in probe and anims:
        probe.append(len(anims) - 1)

    for ai in probe:
        a = anims[ai]
        print(f"\n  --- animation[{ai}] {a.name!r}  "
              f"({len(a.channels)} channels) ---")
        hit = False
        for ch in a.channels:
            tgt = ch.target.node
            if tgt not in watch:
                continue
            if ch.target.path != "translation":
                continue
            hit = True
            samp = a.samplers[ch.sampler]
            vals = _decode_vec3(g, blob, samp.output)
            xs = [v[0] for v in vals]
            ys = [v[1] for v in vals]
            zs = [v[2] for v in vals]
            print(f"    {watch[tgt]} translation: {len(vals)} frames")
            print(f"      frame[0]   =({vals[0][0]:+9.4f},{vals[0][1]:+9.4f},{vals[0][2]:+9.4f})")
            print(f"      frame[last]=({vals[-1][0]:+9.4f},{vals[-1][1]:+9.4f},{vals[-1][2]:+9.4f})")
            print(f"      X range=[{min(xs):+8.4f},{max(xs):+8.4f}]  "
                  f"Y range=[{min(ys):+8.4f},{max(ys):+8.4f}]  "
                  f"Z range=[{min(zs):+8.4f},{max(zs):+8.4f}]")
        if not hit:
            print(f"    (no root/hips translation channel — "
                  f"root stays at rest)")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        inspect(p)
        print()
