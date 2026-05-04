"""
diag_v33_skeleton_and_carve.py — Show the v33 skeleton + the predicted
carve footprint (centerline + EDT-derived width + slope/proximity
modifiers). This is what the carver will actually trench, before
running an MCA render.

Usage: py tools/diag_v33_skeleton_and_carve.py --tile-x 51 --tile-z 53 \
        --out memory/v33_carve_preview_51_53.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import distance_transform_edt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.hydro_region_overlay import apply_hydro_region_overlay

TILE = 512
SEA_LEVEL_RAW = 17050


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tile-x", type=int, required=True)
    p.add_argument("--tile-z", type=int, required=True)
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    masks_dir = Path(args.masks)
    tx, tz = args.tile_x, args.tile_z
    col_off, row_off = tx * TILE, tz * TILE

    # Load tile masks (subset needed for the overlay's slope+proximity logic)
    win = Window(col_off, row_off, TILE, TILE)
    def _read(name, dtype=None):
        with rasterio.open(masks_dir / f"{name}.tif") as src:
            arr = src.read(1, window=win)
        if dtype is not None and arr.dtype != dtype:
            arr = arr.astype(dtype)
        return arr

    h_raw = _read("height")
    height_norm = h_raw.astype(np.float32) / 65535.0
    slope_raw = _read("slope")
    slope_norm = slope_raw.astype(np.float32)
    if slope_norm.max() > 1.5:
        slope_norm = slope_norm / 65535.0

    masks = {
        "hydro_centerline": np.zeros((TILE, TILE), dtype=np.float32),
        "hydro_order":      np.zeros((TILE, TILE), dtype=np.float32),
        "hydro_width":      np.zeros((TILE, TILE), dtype=np.float32),
        "hydro_depth":      np.zeros((TILE, TILE), dtype=np.float32),
        "hydro_lake":       np.zeros((TILE, TILE), dtype=np.float32),
        "hydro_lkdep":      np.zeros((TILE, TILE), dtype=np.float32),
        "height":           height_norm,
        "slope":            slope_norm,
    }

    print(f"Applying v33 overlay (paint + EDT width + slope/proximity)...",
          file=sys.stderr)
    apply_hydro_region_overlay(masks, masks_dir, col_off, row_off, TILE,
                                verbose=True)

    centerline = masks["hydro_centerline"] > 0
    width = masks["hydro_width"]    # per-cell radius in MC blocks (uint8)
    print(f"  centerline cells: {int(centerline.sum())}", file=sys.stderr)
    if centerline.any():
        ws = width[centerline]
        print(f"  width radius: min={ws.min()} max={ws.max()} "
              f"mean={ws.mean():.1f}", file=sys.stderr)

    # Predicted carve footprint = dist_to_centerline <= width[nearest_centerline]
    if centerline.any():
        dist, idx = distance_transform_edt(~centerline, return_indices=True)
        nearest_width = width[idx[0], idx[1]]
        carve_footprint = dist <= nearest_width
    else:
        carve_footprint = np.zeros((TILE, TILE), dtype=bool)
    print(f"  carve footprint cells: {int(carve_footprint.sum())}",
          file=sys.stderr)

    # Hillshade base (dim)
    gaea_in = np.array([0, SEA_LEVEL_RAW, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    height_blocks = np.interp(h_raw.ravel(), gaea_in, mc_y_out
                               ).reshape(h_raw.shape).astype(np.float32)
    norm = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height_blocks)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1) * 0.55

    ocean = h_raw <= SEA_LEVEL_RAW
    base[ocean] = [0.05, 0.10, 0.20]

    # Lakes (real terrain-intersection)
    with rasterio.open(masks_dir / "hydro_lake.tif") as src:
        lk = src.read(1, window=win)
    with rasterio.open(masks_dir / "hydro_lake_wl.tif") as src:
        wl = src.read(1, window=win).astype(np.float32)
    if wl.max() <= 1.5:
        wl *= 65535.0
    lake_wl_mc = np.interp(wl.ravel(), gaea_in, mc_y_out
                            ).reshape(wl.shape).astype(np.float32)
    underwater = (lk > 0) & (height_blocks < lake_wl_mc) & ~ocean
    base[underwater] = [0.20, 0.55, 0.75]

    # Carve footprint (orange — what the trench WILL cover)
    base[carve_footprint] = [0.95, 0.60, 0.20]
    # Centerline (yellow — the spine)
    base[centerline] = [1.0, 0.95, 0.10]

    rgb = (base * 255).astype(np.uint8)
    Image.fromarray(rgb).save(args.out, optimize=True)
    print(f"Saved {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
