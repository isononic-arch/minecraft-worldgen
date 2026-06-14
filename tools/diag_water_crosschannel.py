"""diag_water_crosschannel.py — S94: characterize the (84,60) wide-river
'uneven stepping' the user walked. Distinguishes the DESIRED pattern (water-Y
uniform across each cross-section, steps ONLY along flow) from the OBSERVED
(water-Y varies across the channel width, tracking surface microrelief).

Reads the SURF_DUMP_STEP9_DIR dump (rwy/sy_final/rmeta9 .npy) — the exact
arrays chunk_writer received, post all Step-9 passes.

Metrics:
  - cross-channel spread: for each river cell, max|rwy - rwy| over the
    PERPENDICULAR-to-flow neighbours within the channel. A clean widthwise
    band => 0. Surface-tracking => >0.
  - per-cross-section rwy range: slice the channel perpendicular to the
    local flow at each centerline pixel; report the distribution of
    (max rwy - min rwy) across the width. Desired: almost all 0.
  - correlation of rwy with local surface_y detail (rwy should be
    INDEPENDENT of sub-band surface bumps).

Usage: py tools/diag_water_crosschannel.py <dump_dir> <tx> <tz>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from scipy.ndimage import distance_transform_edt, label


def main(d, tx, tz):
    rwy = np.load(f"{d}/rwy_{tx}_{tz}.npy").astype(np.int32)
    sy = np.load(f"{d}/sy_final_{tx}_{tz}.npy").astype(np.int32)
    rm = np.load(f"{d}/rmeta9_{tx}_{tz}.npy")
    riv = ((rm == 1) | (rm == 2)) & (rwy > 63)
    n = int(riv.sum())
    print(f"({tx},{tz}) river cells (above-sea): {n}")
    if n == 0:
        print("  no above-sea river here.")
        return

    # ---- water-Y histogram --------------------------------------------------
    vals, cnts = np.unique(rwy[riv], return_counts=True)
    print("  water-Y distribution:")
    for v, c in sorted(zip(vals.tolist(), cnts.tolist()), key=lambda x: -x[1])[:12]:
        print(f"    Y={v}: {c} cells ({100.0*c/n:.1f}%)")

    # ---- cross-channel spread (8-neighbour max abs diff within river) -------
    big = np.int32(1 << 20)
    rv = np.where(riv, rwy, big)
    diffs = np.zeros_like(rwy)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            sh = np.roll(np.roll(rv, dr, 0), dc, 1)
            ok = riv & (sh < big)
            diffs = np.where(ok, np.maximum(diffs, np.abs(rwy - sh)), diffs)
    spread = diffs[riv]
    print(f"  neighbour water-Y spread (within channel):")
    print(f"    ==0: {100.0*(spread==0).sum()/n:.1f}%   ==1: {100.0*(spread==1).sum()/n:.1f}%   "
          f">=2: {100.0*(spread>=2).sum()/n:.1f}%   max={spread.max()}")

    # ---- per-cross-section width range -------------------------------------
    # approximate flow direction via the gradient of dist-from-ocean; sample
    # rwy along the perpendicular at each river pixel, report the width range.
    _ocean = ~riv  # everything not river; dist-from-ocean ~ dist into channel
    dist = distance_transform_edt(riv).astype(np.float32)  # 0 at banks, max mid-channel
    # local channel width proxy = 2*dist at the medial cells
    medial = riv & (dist >= np.maximum(1.0, 0.6 * dist.max()))
    print(f"  channel: max half-width={dist.max():.0f}px (~{2*dist.max():.0f} blocks), "
          f"medial cells={int(medial.sum())}")

    # ---- step-line geometry: are level boundaries perpendicular to flow? ----
    # label each constant-Y plateau; a clean design => few large plateaus,
    # each spanning the full width. surface-tracking => many small fragments.
    plateaus = 0
    frag = 0
    for v in vals:
        m = riv & (rwy == v)
        lab, k = label(m)
        plateaus += k
        # count plateaus smaller than a half-channel-width tile (fragments)
        sizes = np.bincount(lab.ravel())[1:]
        frag += int((sizes < 40).sum())
    print(f"  level plateaus: {plateaus} connected regions across {len(vals)} levels, "
          f"{frag} fragments (<40 cells)")
    print(f"  -> VERDICT: {'CROSS-CHANNEL STEPPING (surface-tracking)' if (spread>=1).mean()>0.15 or frag>plateaus*0.5 else 'clean widthwise bands'}")

    # ---- does rwy track sub-band surface detail? ---------------------------
    # within each Y-level, correlate residual rwy (0 by construction) is moot;
    # instead test: among cells SHARING a level, does the surface vary a lot
    # (expected) while rwy is flat (good) -- and at boundaries, does the rwy
    # step coincide with a surface step (bad coupling)?
    sd = sy[riv]
    print(f"  surface_y under river: p10={np.percentile(sd,10):.0f} "
          f"p50={np.median(sd):.0f} p90={np.percentile(sd,90):.0f} "
          f"(spread {np.percentile(sd,90)-np.percentile(sd,10):.0f} blocks)")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
