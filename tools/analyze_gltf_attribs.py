"""Parse all .gltf files in ./exports/ and report vertex attribute layouts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pygltflib import GLTF2

# glTF accessor componentType values
COMPONENT_TYPES = {
    5120: ("BYTE", 1),
    5121: ("UNSIGNED_BYTE", 1),
    5122: ("SHORT", 2),
    5123: ("UNSIGNED_SHORT", 2),
    5125: ("UNSIGNED_INT", 4),
    5126: ("FLOAT", 4),
}

TYPE_COMPONENT_COUNT = {
    "SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
    "MAT2": 4, "MAT3": 9, "MAT4": 16,
}


def attr_size(accessor) -> tuple[str, int, int]:
    ctype_name, ctype_bytes = COMPONENT_TYPES[accessor.componentType]
    n = TYPE_COMPONENT_COUNT[accessor.type]
    return ctype_name, ctype_bytes * n, n


def analyze(path: Path):
    gltf = GLTF2().load(str(path))
    rows = []
    summary_attrs: dict[str, tuple[str, int]] = {}

    for mi, mesh in enumerate(gltf.meshes or []):
        for pi, prim in enumerate(mesh.primitives):
            attrs = prim.attributes
            attr_dict = {k: v for k, v in attrs.__dict__.items() if v is not None}
            stride = 0
            attr_lines = []
            for name, acc_idx in sorted(attr_dict.items()):
                acc = gltf.accessors[acc_idx]
                ctype, size, ncomp = attr_size(acc)
                stride += size
                attr_lines.append((name, acc.type, ctype, ncomp, size))
                summary_attrs[name] = (f"{acc.type}/{ctype}", size)

            pos_acc = gltf.accessors[attr_dict["POSITION"]] if "POSITION" in attr_dict else None
            vcount = pos_acc.count if pos_acc else 0
            icount = gltf.accessors[prim.indices].count if prim.indices is not None else 0

            rows.append({
                "mesh": mi,
                "mesh_name": mesh.name or "",
                "prim": pi,
                "attrs": attr_lines,
                "stride": stride,
                "vcount": vcount,
                "icount": icount,
            })

    return rows, summary_attrs


def fmt_table(name: str, rows):
    print(f"\n{'='*90}")
    print(f"FILE: {name}")
    print('='*90)
    for r in rows:
        print(f"\n  Mesh {r['mesh']} '{r['mesh_name']}' / Primitive {r['prim']}")
        print(f"  Vertices: {r['vcount']}   Indices: {r['icount']}   Stride: {r['stride']} bytes")
        print(f"  {'Attribute':<14} {'Type':<8} {'Component':<18} {'NComp':<6} {'Bytes':<6}")
        print(f"  {'-'*60}")
        for (an, atype, ctype, nc, sz) in r['attrs']:
            print(f"  {an:<14} {atype:<8} {ctype:<18} {nc:<6} {sz:<6}")


def main():
    exports = Path(__file__).parent.parent / "exports"
    files = sorted(exports.glob("*.gltf"))

    all_summaries: dict[str, dict[str, tuple[str, int]]] = {}
    all_rows: dict[str, list] = {}

    for p in files:
        rows, summary = analyze(p)
        fmt_table(p.name, rows)
        all_summaries[p.name] = summary
        all_rows[p.name] = rows

    # Cross-model comparison
    print(f"\n{'='*90}\nCROSS-MODEL ATTRIBUTE COMPARISON\n{'='*90}")
    all_attrs = set()
    for s in all_summaries.values():
        all_attrs.update(s.keys())
    all_attrs = sorted(all_attrs)

    header = "Attribute".ljust(16) + "".join(f.split("_(")[0][:18].ljust(20) for f in all_summaries)
    print(header)
    print("-" * len(header))
    for a in all_attrs:
        line = a.ljust(16)
        for fn, summ in all_summaries.items():
            if a in summ:
                t, sz = summ[a]
                line += f"{t}({sz}b)".ljust(20)
            else:
                line += "-".ljust(20)
        print(line)

    # Differences
    print(f"\nATTRIBUTES THAT DIFFER:")
    for a in all_attrs:
        present = [fn for fn, s in all_summaries.items() if a in s]
        absent = [fn for fn, s in all_summaries.items() if a not in s]
        types = {fn: s[a] for fn, s in all_summaries.items() if a in s}
        unique_types = set(types.values())
        if absent:
            print(f"  {a}: missing in {len(absent)}/{len(all_summaries)} files")
            for fn in absent:
                print(f"      - {fn}")
        if len(unique_types) > 1:
            print(f"  {a}: type mismatch — {unique_types}")

    # Save JSON for cross-reference
    out = {
        "files": {fn: [
            {
                "mesh": r["mesh"], "mesh_name": r["mesh_name"], "prim": r["prim"],
                "vcount": r["vcount"], "icount": r["icount"], "stride": r["stride"],
                "attrs": [{"name": n, "type": t, "ctype": c, "ncomp": nc, "bytes": sz}
                          for (n, t, c, nc, sz) in r["attrs"]],
            } for r in rows
        ] for fn, rows in all_rows.items()},
    }
    out_path = Path(__file__).parent / "gltf_attribs.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nSaved JSON to {out_path}")


if __name__ == "__main__":
    main()
