"""Origin-offset diagnostic: dump the skinned scene graph of a .glb.

Reads an exported .glb and prints, for the diagnostic report:
  * scene-root nodes + TRS
  * the skin's joints / skeleton fields
  * mesh nodes (mesh idx, skin idx, TRS) + their parent
  * the bone hierarchy root + its TRS
  * world-space rest transform of the first ~6 bones (parent-chain walk)
  * inverse-bind-matrix translations for bone 0 and the 'hips' bone

Pure read-only analysis — no exporter or GUI code is touched.
"""

from __future__ import annotations

import struct
import sys

import numpy as np
from pygltflib import GLTF2


def _node_matrix(node) -> np.ndarray:
    """Local 4x4 matrix for a glTF node (TRS compose, or explicit matrix)."""
    if node.matrix is not None:
        # glTF stores matrices column-major.
        return np.array(node.matrix, dtype=np.float64).reshape(4, 4).T
    t = node.translation or [0.0, 0.0, 0.0]
    q = node.rotation or [0.0, 0.0, 0.0, 1.0]
    s = node.scale or [1.0, 1.0, 1.0]
    qx, qy, qz, qw = q
    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz
    r = np.array([
        [1 - 2 * (yy + zz), 2 * (xy - wz),     2 * (xz + wy)],
        [2 * (xy + wz),     1 - 2 * (xx + zz), 2 * (yz - wx)],
        [2 * (xz - wy),     2 * (yz + wx),     1 - 2 * (xx + yy)],
    ], dtype=np.float64)
    m = np.eye(4)
    m[:3, :3] = r * np.array(s)
    m[:3, 3] = t
    return m


def _accessor_mat4(g: GLTF2, blob: bytes, acc_idx: int) -> list[np.ndarray]:
    """Decode a MAT4 FLOAT accessor into a list of 4x4 numpy matrices."""
    acc = g.accessors[acc_idx]
    bv = g.bufferViews[acc.bufferView]
    base = (bv.byteOffset or 0) + (acc.byteOffset or 0)
    out = []
    for i in range(acc.count):
        off = base + i * 64  # 16 float32
        floats = struct.unpack("<16f", blob[off:off + 64])
        # glTF MAT4 is column-major.
        out.append(np.array(floats, dtype=np.float64).reshape(4, 4).T)
    return out


def _fmt_vec(v, p=4) -> str:
    return "(" + ", ".join(f"{x:+.{p}f}" for x in v) + ")"


def inspect(path: str) -> None:
    g = GLTF2.load(path)
    blob = g.binary_blob()
    print("=" * 72)
    print(f"FILE: {path}")
    print(f"  nodes={len(g.nodes)} meshes={len(g.meshes)} "
          f"skins={len(g.skins or [])} animations={len(g.animations or [])}")

    # parent map
    parent: dict[int, int] = {}
    for ni, node in enumerate(g.nodes):
        for c in (node.children or []):
            parent[c] = ni

    scene = g.scenes[g.scene or 0]
    print(f"\n  SCENE ROOTS ({len(scene.nodes)}):")
    for ni in scene.nodes:
        node = g.nodes[ni]
        print(f"    node[{ni}] name={node.name!r} mesh={node.mesh} "
              f"skin={node.skin} children={len(node.children or [])}")
        print(f"      T={node.translation} R={node.rotation} S={node.scale} "
              f"matrix={'set' if node.matrix else 'None'}")

    # skin
    for si, skin in enumerate(g.skins or []):
        print(f"\n  SKIN[{si}]: joints={len(skin.joints)} "
              f"skeleton_field={skin.skeleton} ibm_accessor={skin.inverseBindMatrices}")
        root_joint = skin.joints[0]
        print(f"    joints[0]=node[{root_joint}] "
              f"name={g.nodes[root_joint].name!r} "
              f"parent={parent.get(root_joint, 'SCENE-ROOT')}")
        if skin.skeleton is not None:
            sk = g.nodes[skin.skeleton]
            print(f"    skeleton node[{skin.skeleton}] name={sk.name!r} "
                  f"T={sk.translation} parent={parent.get(skin.skeleton,'SCENE-ROOT')}")

        # mesh nodes using this skin
        print(f"\n  MESH NODES (skin={si}):")
        for ni, node in enumerate(g.nodes):
            if node.mesh is None:
                continue
            print(f"    node[{ni}] name={node.name!r} mesh={node.mesh} "
                  f"skin={node.skin} parent={parent.get(ni,'SCENE-ROOT')} "
                  f"T={node.translation} R={node.rotation} S={node.scale}")

        # Bone hierarchy: walk from joints[0] world transform
        ibms = _accessor_mat4(g, blob, skin.inverseBindMatrices)
        # Build world transforms for all joint nodes via parent chain
        # within the joint set.
        joint_set = set(skin.joints)
        node_local = {jn: _node_matrix(g.nodes[jn]) for jn in skin.joints}

        def world(jn: int) -> np.ndarray:
            chain = []
            cur = jn
            while cur is not None and cur in joint_set:
                chain.append(cur)
                cur = parent.get(cur)
            m = np.eye(4)
            for c in reversed(chain):
                m = m @ node_local[c]
            return m

        print(f"\n  FIRST 6 JOINTS — local TRS + world translation:")
        for j in range(min(6, len(skin.joints))):
            jn = skin.joints[j]
            node = g.nodes[jn]
            w = world(jn)
            par = parent.get(jn)
            par_str = (f"joint#{skin.joints.index(par)}"
                       if par in joint_set else f"node{par}/SCENE-ROOT")
            print(f"    joint#{j} node[{jn}] name={node.name!r} parent={par_str}")
            print(f"      local T={_fmt_vec(node.translation or [0,0,0])} "
                  f"S={_fmt_vec(node.scale or [1,1,1])}")
            print(f"      WORLD translation={_fmt_vec(w[:3,3])}")
            ibm = ibms[j]
            print(f"      IBM translation={_fmt_vec(ibm[:3,3])}")
            # Consistency: world(joint) @ IBM should be identity at rest.
            net = w @ ibm
            print(f"      world@IBM translation={_fmt_vec(net[:3,3])} "
                  f"(should be ~0 if rest-consistent)")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        inspect(p)
        print()
