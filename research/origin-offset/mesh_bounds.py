"""Origin-offset diagnostic: per-mesh POSITION bounding boxes.

For a skinned glTF whose skeleton root is at the origin and whose IBMs
are rest-consistent, the POSITION accessor values ARE the rest-pose
rendered positions. So the POSITION bounding box tells us exactly where
the geometry sits relative to world origin.

Reads accessor.min / accessor.max (the exporter writes these for every
POSITION accessor) — no binary decode needed.
"""

from __future__ import annotations

import sys

from pygltflib import GLTF2


def bounds(path: str) -> None:
    g = GLTF2.load(path)
    print("=" * 72)
    print(f"FILE: {path}")

    overall_min = [float("inf")] * 3
    overall_max = [float("-inf")] * 3

    for mi, mesh in enumerate(g.meshes):
        m_min = [float("inf")] * 3
        m_max = [float("-inf")] * 3
        nprim = 0
        for prim in mesh.primitives:
            pos_acc_idx = prim.attributes.POSITION
            if pos_acc_idx is None:
                continue
            acc = g.accessors[pos_acc_idx]
            if acc.min is None or acc.max is None:
                continue
            nprim += 1
            for k in range(3):
                m_min[k] = min(m_min[k], acc.min[k])
                m_max[k] = max(m_max[k], acc.max[k])
        if nprim == 0:
            continue
        center = [(m_min[k] + m_max[k]) / 2 for k in range(3)]
        size = [m_max[k] - m_min[k] for k in range(3)]
        for k in range(3):
            overall_min[k] = min(overall_min[k], m_min[k])
            overall_max[k] = max(overall_max[k], m_max[k])
        print(f"  mesh[{mi}] {mesh.name!r}  ({nprim} prim)")
        print(f"    min   =({m_min[0]:+9.4f},{m_min[1]:+9.4f},{m_min[2]:+9.4f})")
        print(f"    max   =({m_max[0]:+9.4f},{m_max[1]:+9.4f},{m_max[2]:+9.4f})")
        print(f"    center=({center[0]:+9.4f},{center[1]:+9.4f},{center[2]:+9.4f})")
        print(f"    size  =({size[0]:+9.4f},{size[1]:+9.4f},{size[2]:+9.4f})")

    o_center = [(overall_min[k] + overall_max[k]) / 2 for k in range(3)]
    o_size = [overall_max[k] - overall_min[k] for k in range(3)]
    print(f"  --- OVERALL ---")
    print(f"    min   =({overall_min[0]:+9.4f},{overall_min[1]:+9.4f},{overall_min[2]:+9.4f})")
    print(f"    max   =({overall_max[0]:+9.4f},{overall_max[1]:+9.4f},{overall_max[2]:+9.4f})")
    print(f"    center=({o_center[0]:+9.4f},{o_center[1]:+9.4f},{o_center[2]:+9.4f})")
    print(f"    size  =({o_size[0]:+9.4f},{o_size[1]:+9.4f},{o_size[2]:+9.4f})")
    print()


if __name__ == "__main__":
    for p in sys.argv[1:]:
        bounds(p)
