"""diag_ecotone_seam.py — S93c gate for the cross-tile ecotone dither fix.

Compares PRE vs POST SURF_DUMP_DIR dumps (gc_* ground cover + plc_*
schematic placements + bg_* biome grid from the POST dump) for a pair of
tiles sharing a vertical seam (B east of A).

Gates (the fix's legitimate blast radius):
  1. Every gc diff pixel lies within `width`+slack of a BIOME BOUNDARY or
     a TILE EDGE. (Swap geometry only changes near seams; swap CONTENT
     re-rolls once inside existing ecotone bands — both hug boundaries.
     A diff on pure single-biome interior = a leak = FAIL.)
  2. Placement totals drift < 6% (positions re-jiggle via the exclusion-
     grid cascade — same accepted class as the S91 re-roll — but density
     must hold).
  3. Reports seam-band gc activity per side (post should show activity on
     BOTH sides of a cross-seam boundary).

Usage:
  py tools/diag_ecotone_seam.py <pre_dir> <post_dir> <txA> <tzA> <txB> <tzB> [--width 100]
Exit 1 on any gate failure.
"""
import sys

import numpy as np
from scipy.ndimage import distance_transform_edt

W = 512


def load_gc(d, tx, tz):
    return np.load(f"{d}/gc_{tx}_{tz}.npy", allow_pickle=True)


def load_plc(d, tx, tz):
    arr = np.load(f"{d}/plc_{tx}_{tz}.npy", allow_pickle=True)
    return [(int(r[0]), int(r[1]), str(r[5])) for r in arr]


def boundary_dist(bg):
    """Distance to the nearest different-biome pixel (4-neighbour test)."""
    b = np.zeros(bg.shape, dtype=bool)
    b[:-1, :] |= bg[:-1, :] != bg[1:, :]
    b[1:, :]  |= bg[:-1, :] != bg[1:, :]
    b[:, :-1] |= bg[:, :-1] != bg[:, 1:]
    b[:, 1:]  |= bg[:, :-1] != bg[:, 1:]
    if not b.any():
        return np.full(bg.shape, 1e9, dtype=np.float32)
    return distance_transform_edt(~b).astype(np.float32)


def edge_dist_grid():
    rr, cc = np.mgrid[0:W, 0:W]
    return np.minimum.reduce([rr, W - 1 - rr, cc, W - 1 - cc])


def main():
    pre, post = sys.argv[1], sys.argv[2]
    txA, tzA, txB, tzB = map(int, sys.argv[3:7])
    width = (int(sys.argv[sys.argv.index("--width") + 1])
             if "--width" in sys.argv else 100)
    slack = 6
    fail = []
    ed = edge_dist_grid()
    for (tx, tz), tag in (((txA, tzA), "A"), ((txB, tzB), "B")):
        g0 = load_gc(pre, tx, tz)
        g1 = load_gc(post, tx, tz)
        bg = np.load(f"{post}/bg_{tx}_{tz}.npy", allow_pickle=True)
        bd = boundary_dist(bg)
        diff = g0 != g1
        n = int(diff.sum())
        if n:
            ok = (bd <= width + slack) | (ed <= width + slack)
            leak = int((diff & ~ok).sum())
            print(f"[gc {tag}({tx},{tz})] diffs={n}  "
                  f"leaks(>={width + slack}px from boundary AND edge)={leak}")
            if leak:
                rr, cc = np.where(diff & ~ok)
                print(f"    first leaks: {list(zip(rr[:5].tolist(), cc[:5].tolist()))}")
                fail.append(f"gc {tag}: {leak} pure-interior diffs")
        else:
            print(f"[gc {tag}({tx},{tz})] diffs=0")

        p0 = load_plc(pre, tx, tz)
        p1 = load_plc(post, tx, tz)
        drift = abs(len(p1) - len(p0)) / max(1, len(p0))
        s0 = set(p[2].rsplit("/", 1)[-1] for p in p0)
        s1 = set(p[2].rsplit("/", 1)[-1] for p in p1)
        print(f"[plc {tag}({tx},{tz})] pre={len(p0)} post={len(p1)} "
              f"drift={100 * drift:.1f}%  species pre={len(s0)} post={len(s1)} "
              f"new={sorted(s1 - s0)[:6]}")
        if drift > 0.06:
            fail.append(f"plc {tag} count drift {100 * drift:.1f}%")

    for (tx, tz), side, sel in (((txA, tzA), "west(A)", np.s_[:, -64:]),
                                ((txB, tzB), "east(B)", np.s_[:, :64])):
        g0 = load_gc(pre, tx, tz)[sel]
        g1 = load_gc(post, tx, tz)[sel]
        print(f"seam 64px band {side}: gc-changes={int((g0 != g1).sum())}")

    print("VERDICT:", "FAIL — " + "; ".join(fail) if fail else
          "PASS (diffs hug boundaries/edges; density holds)")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
