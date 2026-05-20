"""Empirically search for the correct eFormat-8 normal decode.

D4Analyzer's reference normals.bin is missing on disk so we can't do
a direct value-by-value comparison. Instead we use a robust topology-
derived ground truth: for each vertex, the average of its incident
*face* normals (computed from positions + indices) approximates the
expected smooth normal. A correctly decoded vertex normal must have
a high positive dot product with that average — wrong formulas,
swapped components, sign flips, and missing renormalisation all
show up as systematic deviations.

We try every reasonable combination of:

  - 4 SNORM formulae (signed-byte, AppToOBJ unsigned, simple
    255-scale, DX10 SNORM "(2*b+1)/255 - 1")
  - 4 ways to pick which 3 of the 4 packed bytes are XYZ (the
    remaining byte is W = handedness/padding)
  - 6 permutations of those 3 bytes
  - 8 sign flips (one per axis)
  - 2 normalisation modes (raw vs renormalised)

= 4 × 4 × 6 × 8 × 2 = 1536 candidate decoders. For each candidate we
score the mean dot product against the topology-derived ground
truth across every Goatman vertex and pick the highest.

Run from the repo root:
    .venv/Scripts/python.exe tools/test_normal_decode.py
"""
from __future__ import annotations

import math
import struct
import sys
from itertools import permutations
from pathlib import Path

# Ensure src/ is importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from d4extract.formats.app_parser import (
    parse_app, FMT_PACKED_SNORM4, SEM_NORMAL,
)


# ─── Decode formulae ─────────────────────────────────────────────────

FORMULAE = {
    "AppToOBJ:    b/127.5 - 1":        lambda b: b / 127.5 - 1.0,
    "signed byte: int8(b)/127":        lambda b: ((b - 256) if b >= 128 else b) / 127.0,
    "simple:      b/255*2 - 1":        lambda b: b / 255.0 * 2.0 - 1.0,
    "DX10 SNORM:  (2b+1)/255 - 1":     lambda b: (2.0 * b + 1.0) / 255.0 - 1.0,
}


# ─── Geometry ───────────────────────────────────────────────────────

def _norm(v):
    L = math.sqrt(v[0]*v[0] + v[1]*v[1] + v[2]*v[2])
    if L < 1e-9:
        return (0.0, 0.0, 0.0)
    return (v[0]/L, v[1]/L, v[2]/L)


def compute_incident_normals(positions, indices):
    """Per-vertex average of incident face normals (raw, not normalised)."""
    n = [(0.0, 0.0, 0.0)] * len(positions)
    n = [list(t) for t in n]
    for i in range(0, len(indices), 3):
        a, b, c = indices[i], indices[i+1], indices[i+2]
        pa, pb, pc = positions[a], positions[b], positions[c]
        ux, uy, uz = pb[0]-pa[0], pb[1]-pa[1], pb[2]-pa[2]
        vx, vy, vz = pc[0]-pa[0], pc[1]-pa[1], pc[2]-pa[2]
        # cross(u, v)
        cx = uy*vz - uz*vy
        cy = uz*vx - ux*vz
        cz = ux*vy - uy*vx
        for vi in (a, b, c):
            n[vi][0] += cx
            n[vi][1] += cy
            n[vi][2] += cz
    return [_norm(t) for t in n]


# ─── Raw byte extraction ────────────────────────────────────────────

def collect_normal_bytes(meta_path: Path, payload_path: Path):
    """Reconstruct raw normal bytes from the parser's already-decoded values.

    The current decoder is an invertible affine map ``b -> b/127.5 - 1``,
    so ``b = round((n + 1) * 127.5)`` recovers the original 8-bit value
    exactly. This sidesteps the complication of reaching into the
    parser's internal vertex-buffer scanner — we only need the raw
    bytes for re-decoding under alternative formulas, and reconstructing
    them from the float values is bit-exact.
    """
    mesh = parse_app(meta_path, payload_path)
    if not mesh.normals:
        raise RuntimeError("parser returned no normals — cannot test")

    raw_bytes_per_vertex = []
    for n in mesh.normals:
        # Parser returns 4-tuples for packed-SNORM4 (NORMAL has W=0 padding).
        bs = []
        for c in n:
            b = round((c + 1.0) * 127.5)
            b = max(0, min(255, b))
            bs.append(b)
        raw_bytes_per_vertex.append(tuple(bs))
    return mesh, raw_bytes_per_vertex


# ─── Decoder enumeration ────────────────────────────────────────────

def make_decoder(formula, byte_indices, signs, renormalise):
    """Return a function that turns 4 raw bytes into a (x, y, z) float3."""
    def decode(b4):
        bytes_xyz = [b4[i] for i in byte_indices]
        x = formula(bytes_xyz[0]) * signs[0]
        y = formula(bytes_xyz[1]) * signs[1]
        z = formula(bytes_xyz[2]) * signs[2]
        if renormalise:
            return _norm((x, y, z))
        return (x, y, z)
    return decode


def enumerate_candidates():
    """Yield (label, decoder) pairs covering the 1536-candidate space."""
    for f_name, f in FORMULAE.items():
        # 4 ways to pick the W byte (and thus which 3 are XYZ)
        for w_idx in range(4):
            xyz_pool = [i for i in range(4) if i != w_idx]
            for perm in permutations(xyz_pool):
                # 8 sign combinations
                for sx in (1, -1):
                    for sy in (1, -1):
                        for sz in (1, -1):
                            for renorm in (False, True):
                                lbl = (
                                    f"{f_name:30s} | "
                                    f"xyz=b{perm[0]}b{perm[1]}b{perm[2]} "
                                    f"w=b{w_idx} | "
                                    f"signs=({'+-'[sx<0]}{'+-'[sy<0]}{'+-'[sz<0]}) "
                                    f"| renorm={'Y' if renorm else 'N'}"
                                )
                                yield lbl, make_decoder(f, perm, (sx, sy, sz), renorm)


# ─── Scoring ────────────────────────────────────────────────────────

def score_decoder(decoder, raw_bytes, gt_normals, sample_n=2000):
    """Mean dot product between decoded and ground-truth normals.

    Sampling caps work to 2 000 random vertices for speed; results are
    representative across a 1 800-vertex Goatman dataset.
    """
    import random
    rng = random.Random(0xD4)
    indices = list(range(len(raw_bytes)))
    if len(indices) > sample_n:
        indices = rng.sample(indices, sample_n)

    dot_sum = 0.0
    mag_sum = 0.0
    n = 0
    for i in indices:
        gt = gt_normals[i]
        if gt == (0.0, 0.0, 0.0):
            continue  # vertex with no incident faces; skip
        nx, ny, nz = decoder(raw_bytes[i])
        L = math.sqrt(nx*nx + ny*ny + nz*nz)
        if L < 1e-9:
            continue
        # Compare in unit space — the score is about direction, not length.
        d = (nx*gt[0] + ny*gt[1] + nz*gt[2]) / L
        dot_sum += d
        mag_sum += L
        n += 1
    if n == 0:
        return -1.0, 0.0, 0
    return dot_sum / n, mag_sum / n, n


# ─── Main ───────────────────────────────────────────────────────────

def main():
    meta = Path("samples/base/meta/Appearance/Goatman_BossTrophy.app")
    payload = Path("samples/base/payload/Appearance/Goatman_BossTrophy.app")

    print(f"Decoding raw normal bytes from {meta.name} payload...")
    mesh, raw = collect_normal_bytes(meta, payload)
    print(f"  vertices: {mesh.vertex_count}, indices: {mesh.index_count}, "
          f"stride: {mesh.layout.stride if mesh.layout else '?'}")

    print("Computing geometric face-normal ground truth...")
    gt = compute_incident_normals(mesh.positions, mesh.indices)
    n_real = sum(1 for g in gt if g != (0.0, 0.0, 0.0))
    print(f"  vertices with incident faces: {n_real}/{len(gt)}")

    print("Scoring all candidate decoders...\n")
    results = []
    for lbl, dec in enumerate_candidates():
        mean_dot, mean_mag, n_used = score_decoder(dec, raw, gt)
        results.append((mean_dot, mean_mag, n_used, lbl))
    results.sort(key=lambda r: -r[0])

    print(f"=== TOP 12 candidates by mean dot product ===\n")
    print(f"{'mean_dot':>8} {'mean_|n|':>8} {'n':>6}  description")
    print("-" * 100)
    for mean_dot, mean_mag, n, lbl in results[:12]:
        print(f"{mean_dot:8.4f} {mean_mag:8.4f} {n:6d}  {lbl}")

    print(f"\n=== Worst 4 (sanity: should be near -1 for inverted) ===\n")
    for mean_dot, mean_mag, n, lbl in results[-4:]:
        print(f"{mean_dot:8.4f} {mean_mag:8.4f} {n:6d}  {lbl}")

    print(f"\n=== Current parser's decoder for reference ===")
    cur = make_decoder(
        FORMULAE["AppToOBJ:    b/127.5 - 1"],
        byte_indices=(0, 1, 2),
        signs=(1, 1, 1),
        renormalise=False,
    )
    md, mm, n = score_decoder(cur, raw, gt)
    print(f"  mean_dot={md:.4f} mean_|n|={mm:.4f} n={n}")


if __name__ == "__main__":
    main()
