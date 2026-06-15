"""S94: find GENUINE river-crossing tile seams (+ biome-dither and relief seams)
by reading full-res boundary strips at every tile boundary. Thin centerlines
survive (no downsample). Ranks seams so the validation set can pin tile PAIRS
that actually share a wide river / a high-relief biome boundary across the seam.

Usage: py tools/diag_seam_crossings.py
Writes diag_seam_crossings.json + prints top crossings.
"""
import json
import numpy as np
import rasterio
from rasterio.windows import Window

MASKS = "masks"
N = 97
TS = 512
SEA_RAW = 17050

ZONES = {
    10: "COASTAL_HEATH", 20: "TEMPERATE_RAINFOREST", 30: "BOREAL_TAIGA",
    35: "SNOWY_BOREAL_TAIGA", 40: "BOREAL_ALPINE", 50: "ARCTIC_TUNDRA",
    55: "FROZEN_FLATS", 60: "TEMPERATE_DECIDUOUS", 70: "RAINFOREST_COAST",
    80: "RIPARIAN_WOODLAND", 90: "DRY_OAK_SAVANNA", 100: "KARST_BARRENS",
    110: "BIRCH_FOREST", 115: "EASTERN_TEMPERATE_COAST", 120: "MIXED_FOREST",
    130: "CONTINENTAL_STEPPE", 140: "DRY_PINE_BARRENS", 150: "SCRUBBY_HEATHLAND",
    160: "LUSH_RAINFOREST_COAST", 170: "SAND_DUNE_DESERT",
    190: "DESERT_STEPPE_TRANSITION", 200: "SEMI_ARID_SHRUBLAND",
    210: "DRY_WOODLAND_MAQUIS", 220: "TIDAL_JUNGLE_FRINGE",
    230: "MANGROVE_COAST", 240: "FRESHWATER_FEN",
}


def main():
    w_src = rasterio.open(f"{MASKS}/hydro_width.tif")
    o_src = rasterio.open(f"{MASKS}/hydro_order.tif")
    h_src = rasterio.open(f"{MASKS}/height.tif")
    ov_src = rasterio.open(f"{MASKS}/override.tif")
    W = w_src.width
    H = w_src.height
    print(f"masks {W}x{H}")

    cross = []   # river crossings: dict per (orientation, seam_idx, tile_band)

    # ---- vertical seams: world x = b*TS, between tile col b-1 and b ----------
    for b in range(1, N):
        X = b * TS
        if X >= W:
            break
        win = Window(X - 2, 0, 4, H)            # cols [X-2..X+1], all rows
        wd = w_src.read(1, window=win).astype(np.int32)   # H x 4
        hh = h_src.read(1, window=win).astype(np.int32)
        # river spans the seam where BOTH sides of the boundary carry width>0
        left = wd[:, 1] > 0                      # x = X-1
        right = wd[:, 2] > 0                     # x = X
        span = left & right                      # H bool: river crosses here
        above = (hh[:, 1] > SEA_RAW) & (hh[:, 2] > SEA_RAW)
        wmax_line = np.maximum(wd[:, 1], wd[:, 2])
        for tz in range(N):
            z0, z1 = tz * TS, min((tz + 1) * TS, H)
            if z0 >= H:
                break
            sl = slice(z0, z1)
            sp = span[sl]
            ab = above[sl] & sp
            if int(ab.sum()) < 4:                # need a real above-sea crossing
                continue
            cross.append({
                "kind": "river", "orient": "V",
                "a": [b - 1, tz], "b": [b, tz],
                "rows": int(sp.sum()),
                "rows_abovesea": int(ab.sum()),
                "wmax": int(wmax_line[sl][ab].max()) if ab.any() else 0,
                "wsum": int(wmax_line[sl][ab].sum()),
            })

    # ---- horizontal seams: world z = b*TS, between tile row b-1 and b --------
    for b in range(1, N):
        Z = b * TS
        if Z >= H:
            break
        win = Window(0, Z - 2, W, 4)            # rows [Z-2..Z+1], all cols
        wd = w_src.read(1, window=win).astype(np.int32)   # 4 x W
        hh = h_src.read(1, window=win).astype(np.int32)
        top = wd[1, :] > 0                       # z = Z-1
        bot = wd[2, :] > 0                       # z = Z
        span = top & bot
        above = (hh[1, :] > SEA_RAW) & (hh[2, :] > SEA_RAW)
        wmax_line = np.maximum(wd[1, :], wd[2, :])
        for tx in range(N):
            x0, x1 = tx * TS, min((tx + 1) * TS, W)
            if x0 >= W:
                break
            sl = slice(x0, x1)
            sp = span[sl]
            ab = above[sl] & sp
            if int(ab.sum()) < 4:
                continue
            cross.append({
                "kind": "river", "orient": "H",
                "a": [tx, b - 1], "b": [tx, b],
                "rows": int(sp.sum()),
                "rows_abovesea": int(ab.sum()),
                "wmax": int(wmax_line[sl][ab].max()) if ab.any() else 0,
                "wsum": int(wmax_line[sl][ab].sum()),
            })

    cross.sort(key=lambda c: (c["wmax"], c["wsum"]), reverse=True)
    print(f"\n=== top 18 above-sea river-crossing seams (wmax=widest channel "
          f"crossing the boundary, blocks) ===")
    for c in cross[:18]:
        print(f"  {c['orient']} {tuple(c['a'])}|{tuple(c['b'])}  "
              f"wmax={c['wmax']}  rows_abovesea={c['rows_abovesea']}  "
              f"wsum={c['wsum']}")

    with open("diag_seam_crossings.json", "w") as f:
        json.dump(cross[:60], f, indent=1)
    print(f"\nwrote diag_seam_crossings.json ({len(cross)} crossings total)")


if __name__ == "__main__":
    main()
