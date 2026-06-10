"""diag_lake_wall.py — S91 regression #5: reproduce the lakeshore
"retaining wall + trench" WITHOUT a render.

Mechanism under test (assessment finding):
  1. S90 `_carve_lakes_v2` waterline (warped sdf + terrain_follow) sits INSIDE
     the natural basin -> dry basin-floor BELOW lake level remains between
     waterline and rim.
  2. Step 9 escape-fix (run_pipeline.py:~1343) raises land below adjacent
     water_y up to water_y (5 iters -> ~5px wall at water level).
  3. Step 9 EDT berm (run_pipeline.py:~1354) ramps water_y-dist out to 8px.
  4. Beyond 8px the dry floor stays low = trench. water | wall | trench | slope.

This tool replicates: generate_columns surface -> carve_rivers (v2 lake carve)
-> Step 9 lake water levels + escape-fix + berm (verbatim re-implementation),
then renders shore-perpendicular cross-sections + a wall/trench metric.

  --fixed    : apply the candidate fix (flood natural basin: water mask UNION
               pad_basin & terrain<wl_flat) via env LAKE_FLOOD_BASIN=1 hook
               (no-op until the fix lands in river_carver_v2).
  LAKE_V2_OFF=1 env: confirm the wall vanishes under the old carve.

Usage: py tools/diag_lake_wall.py 33 33 [--out diag_lake/wall_33_33]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rasterio
from rasterio.windows import Window
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASKS = os.path.join(ROOT, "masks")
CFG = json.load(open(os.path.join(ROOT, "config", "thresholds.json")))
SEA_LEVEL = 63


def read_tile(name, tx, tz, pad=0, dtype=np.float32):
    with rasterio.open(os.path.join(MASKS, name)) as ds:
        x0 = max(tx * 512 - pad, 0)
        z0 = max(tz * 512 - pad, 0)
        x1 = min(tx * 512 + 512 + pad, ds.width)
        z1 = min(tz * 512 + 512 + pad, ds.height)
        a = ds.read(1, window=Window(x0, z0, x1 - x0, z1 - z0)).astype(dtype)
    return a


def main():
    tx, tz = int(sys.argv[1]), int(sys.argv[2])
    out = f"diag_lake/wall_{tx}_{tz}"
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    import core.column_generator as col_gen
    import core.river_carver_v2 as rc2

    # ── pre-carve surface (generate_columns: LUT + ocean EDT; no dunes here) ──
    height = read_tile("height.tif", tx, tz, dtype=np.uint16)
    biome = np.full((512, 512), "MIXED_FOREST", dtype=object)  # dune-irrelevant
    surface_y = col_gen.generate_columns(
        height, None, biome, None, {}, CFG, tx, tz)

    # ── hydro masks (normalized as run_pipeline's tile_streamer provides) ──
    def norm(name, scale):
        return read_tile(name, tx, tz) / scale
    masks = {
        "hydro_order": norm("hydro_order.tif", 255.0),
        "hydro_width": norm("hydro_width.tif", 255.0),
        "hydro_depth": norm("hydro_depth.tif", 255.0),
        "hydro_lake": norm("hydro_lake.tif", 65535.0),
        "hydro_lkdep": norm("hydro_lkdep.tif", 255.0),
        "hydro_lake_wl": norm("hydro_lake_wl.tif", 65535.0),
        "flow": norm("flow.tif", 65535.0),
    }
    from pathlib import Path
    sy_carved, river_meta, _conn, water_y_field = rc2.carve_rivers(
        surface_y, masks["flow"], None, CFG,
        hydro_order=masks["hydro_order"], hydro_width=masks["hydro_width"],
        hydro_depth=masks["hydro_depth"], hydro_lake=masks["hydro_lake"],
        hydro_lkdep=masks["hydro_lkdep"], hydro_lake_wl=masks["hydro_lake_wl"],
        hydro_centerline=None, height_norm=height.astype(np.float32) / 65535.0,
        masks_dir=Path(MASKS), tile_x=tx, tile_z=tz)
    sy = sy_carved.astype(np.int16).copy()

    CHAN_LAKE = 3
    lake_mask = river_meta == CHAN_LAKE
    print(f"({tx},{tz}) lake cells: {int(lake_mask.sum())}")
    if not lake_mask.any():
        print("no lake — pick another tile")
        return

    # ── Step 9 replication: per-component flat lake water level (v26) ──
    from scipy.ndimage import label as _label
    from scipy.ndimage import maximum_filter, distance_transform_edt
    river_water_y = np.where((water_y_field is not None) & (np.asarray(water_y_field) > 0),
                             water_y_field, np.int16(-999)).astype(np.int16)
    sp = CFG["terrain_spline"]
    wl_mc = np.interp(masks["hydro_lake_wl"] * 65535.0,
                      np.array(sp["gaea_in"], float),
                      np.array(sp["mc_y_out"], float)).astype(np.float32)
    lk_lab, n_lk = _label(lake_mask)
    for lid in range(1, n_lk + 1):
        lk = lk_lab == lid
        wv = wl_mc[lk]
        wv = wv[wv > -64]
        lake_water = int(np.floor(float(wv.min()))) if len(wv) else SEA_LEVEL
        river_water_y[lk] = np.int16(lake_water)

    sy_pre_fix = sy.copy()

    # ── Step 9 replication: escape-fix (5 iters) ──
    wpos = np.where(river_water_y > 0, river_water_y, np.int16(0)).astype(np.int16)
    for _ in range(5):
        nbr = maximum_filter(wpos, size=3)
        leak = (sy < nbr) & (nbr > SEA_LEVEL) & (river_water_y < 0)
        if not leak.any():
            break
        sy[leak] = nbr[leak]

    # ── Step 9 replication: EDT berm ──
    BERM_RADIUS = 8
    wmask = river_water_y > SEA_LEVEL
    if wmask.any():
        dist, idx = distance_transform_edt(~wmask, return_indices=True)
        nearest_wy = river_water_y[idx[0], idx[1]]
        target = nearest_wy.astype(np.int16) - dist.astype(np.int16)
        need = ((sy < target) & (target > SEA_LEVEL)
                & (river_water_y < 0) & (dist <= BERM_RADIUS))
        sy[need] = target[need]
        print(f"escape+berm raised {int((sy > sy_pre_fix).sum())} cells "
              f"(max raise {int((sy - sy_pre_fix).max())} blocks)")

    # ── metrics + cross-sections ──
    # walk outward from the shore along the densest-water row, both directions
    r = int(np.argmax(lake_mask.sum(1)))
    wl_row = float(np.median(river_water_y[lake_mask & (lk_lab == lk_lab[r, np.argmax(lake_mask[r])])]))
    cols = np.where(lake_mask[r])[0]
    c0, c1 = cols.min(), cols.max()
    lo, hi = max(0, c0 - 60), min(512, c1 + 60)
    xs = np.arange(lo, hi)
    plt.figure(figsize=(14, 4))
    plt.plot(xs, sy_pre_fix[r, lo:hi], color="#888", lw=1.2,
             label="post-carve (pre Step9)")
    plt.plot(xs, sy[r, lo:hi], color="#7a5230", lw=1.6,
             label="post escape-fix + berm")
    wm = lake_mask[r, lo:hi]
    plt.fill_between(xs, sy[r, lo:hi],
                     np.where(wm, wl_row, sy[r, lo:hi]),
                     color="#4a90d9", alpha=.6)
    plt.axhline(wl_row, color="#1f4e9c", ls="--", lw=1, label=f"water Y{wl_row:.0f}")
    plt.title(f"lake ({tx},{tz}) row {r} — retaining wall + trench reproduction")
    plt.legend()
    plt.grid(alpha=.3)
    plt.tight_layout()
    plt.savefig(out + "_xsection.png", dpi=90)

    # wall metric: land cells raised by escape/berm that are ABOVE both water
    # and the natural terrain behind them
    raised = (sy > sy_pre_fix)
    print(f"raised-cell count: {int(raised.sum())}")
    # trench metric: dry land below water level within 24px of the lake
    dist_l = distance_transform_edt(~lake_mask)
    trench = (~lake_mask) & (dist_l <= 24) & (sy < np.float32(wl_row)) & (river_water_y < 0)
    print(f"dry-below-water cells within 24px of shore (trench): {int(trench.sum())}")
    print(f"wrote {out}_xsection.png")


if __name__ == "__main__":
    main()
