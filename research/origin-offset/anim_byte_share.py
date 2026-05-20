"""How many bytes the glTF animations occupy in an exported glb.

Animation sampler accessors get their own bufferViews (one per accessor
via _push_accessor). Summing those bufferView byteLengths gives the
payload that an animation-free export (Include Animations unchecked)
would omit.
"""

from __future__ import annotations

import os
import sys

from pygltflib import GLTF2

for f in sys.argv[1:]:
    g = GLTF2.load(f)
    total = os.path.getsize(f)
    anim_accs: set[int] = set()
    for a in g.animations or []:
        for s in a.samplers:
            anim_accs.add(s.input)
            anim_accs.add(s.output)
    anim_bytes = sum(
        g.bufferViews[g.accessors[i].bufferView].byteLength
        for i in anim_accs
    )
    m = 1024 * 1024
    pct = 100 * anim_bytes / total if total else 0
    print(f"{os.path.basename(f):24} total={total/m:8.1f} MB  "
          f"anim payload={anim_bytes/m:8.1f} MB ({pct:4.1f}%)  "
          f"without-anims ~ {(total-anim_bytes)/m:.1f} MB")
