"""diag_fast_simplex_equiv.py — S101: prove core/fast_simplex.py is BITWISE
identical to opensimplex's pure-Python reference across seeds, scales, offsets,
negative coords, and non-uniform spacing. Run after ANY edit to fast_simplex.py
or an opensimplex version bump. Exit 0 = all bitwise-equal.
"""
import sys, time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import opensimplex
from opensimplex.internals import _noise2a as _ref_noise2a
from core.fast_simplex import _noise2a_vec

CASES = [
    # (name, xs, ys) — mirror real pipeline usage shapes/ranges
    ("tile_world_coords", (np.arange(64, dtype=np.float64) + 21 * 512) / 200.0,
                          (np.arange(64, dtype=np.float64) + 30 * 512) / 200.0),
    ("negative_coords",   (np.arange(64, dtype=np.float64) - 50000.0) / 173.3,
                          (np.arange(64, dtype=np.float64) - 8888.0) / 91.7),
    ("fine_scale",        np.arange(64, dtype=np.float64) * 0.001,
                          np.arange(64, dtype=np.float64) * 0.001 + 7.5),
    ("coarse_scale",      np.arange(64, dtype=np.float64) * 37.7,
                          np.arange(64, dtype=np.float64) * 41.3),
    ("non_uniform",       np.cumsum(np.linspace(0.01, 3.0, 64)),
                          np.cumsum(np.linspace(2.9, 0.02, 64)) - 300.0),
    ("lattice_edges",     np.arange(64, dtype=np.float64) * 0.5,   # hits integer skew sums
                          np.arange(64, dtype=np.float64) * 0.5),
    ("big_grid_256",      (np.arange(256, dtype=np.float64) + 11264) / 350.0,
                          (np.arange(256, dtype=np.float64) + 15616) / 350.0),
]
SEEDS = [3, 42001, 42003, 1337, 0x9C0DEC0 % (2**31), 0, -7]


def main():
    failures = 0
    t_ref_total = t_fast_total = 0.0
    for seed in SEEDS:
        gen = opensimplex.OpenSimplex(seed=seed)
        perm = gen._perm
        for name, xs, ys in CASES:
            t0 = time.perf_counter(); ref = _ref_noise2a(xs, ys, perm); t1 = time.perf_counter()
            fast = _noise2a_vec(xs, ys, perm); t2 = time.perf_counter()
            t_ref_total += t1 - t0; t_fast_total += t2 - t1
            # tobytes comparison = strict bitwise (catches -0.0 vs +0.0)
            if ref.shape != fast.shape or ref.tobytes() != fast.tobytes():
                bad = int(np.sum(ref != fast)) if ref.shape == fast.shape else -1
                mx = float(np.max(np.abs(ref - fast))) if ref.shape == fast.shape else float("nan")
                print(f"FAIL seed={seed} case={name}: {bad} px differ, max|d|={mx:.3e}")
                failures += 1
            else:
                print(f"ok   seed={seed} case={name}  bitwise identical ({ref.shape[0]}x{ref.shape[1]})")
    n_pts = sum(len(x) * len(y) for _, x, y in CASES) * len(SEEDS)
    print(f"\n{n_pts:,} points: reference {t_ref_total:.2f}s vs vectorized {t_fast_total:.3f}s "
          f"({t_ref_total / max(t_fast_total, 1e-9):.0f}x)")
    if failures:
        print(f"\n{failures} FAILURES — do NOT install fast_simplex"); return 1
    print("\nALL CASES BITWISE IDENTICAL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
