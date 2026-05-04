"""
diag_preview_skeleton_at_tile.py — Show what the carver actually sees for
a specific tile after hydro_region.png has been skeletonized + Bresenham-
rasterized at 50k. Confirms the painted hydrology survives the overlay
pipeline and matches the user's intent.

Usage:
    py tools/diag_preview_skeleton_at_tile.py --tile-x 51 --tile-z 53 \
        --out memory/painted_skeleton_51_53.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from rasterio.enums import Resampling
import matplotlib.pyplot as plt
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.hydro_region_overlay import apply_hydro_region_overlay

TILE_SIZE = 512
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
    col_off, row_off = tx * TILE_SIZE, tz * TILE_SIZE

    # Build empty masks dict matching what the runtime passes
    H = W = TILE_SIZE
    masks = {
        "hydro_centerline": np.zeros((H, W), dtype=np.float32),
        "hydro_order":      np.zeros((H, W), dtype=np.float32),
        "hydro_width":      np.zeros((H, W), dtype=np.float32),
        "hydro_depth":      np.zeros((H, W), dtype=np.float32),
        "hydro_lake":       np.zeros((H, W), dtype=np.float32),
        "hydro_lkdep":      np.zeros((H, W), dtype=np.float32),
    }

    print(f"Applying hydro_region overlay to tile ({tx},{tz})...",
          file=sys.stderr)
    apply_hydro_region_overlay(
        masks, masks_dir, col_off, row_off, TILE_SIZE, verbose=True,
    )

    cl = masks["hydro_centerline"] > 0
    print(f"  centerline cells in tile: {int(cl.sum()):,}", file=sys.stderr)
    print(f"  width range: {masks['hydro_width'].min():.2f}-{masks['hydro_width'].max():.2f}",
          file=sys.stderr)

    # Load terrain hillshade for this tile for visual context
    win = Window(col_off, row_off, TILE_SIZE, TILE_SIZE)
    with rasterio.open(masks_dir / "height.tif") as src:
        h_raw = src.read(1, window=win)

    gaea_in = np.array([0, SEA_LEVEL_RAW, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    height = np.interp(h_raw.ravel(), gaea_in, mc_y_out
                        ).reshape(h_raw.shape).astype(np.float32)
    norm = np.clip((height + 64) / (448 + 64), 0, 1)
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1)
    base = base * 0.55  # dim

    ocean = h_raw <= SEA_LEVEL_RAW
    base[ocean] = [0.05, 0.10, 0.20]

    # Real-lake overlay so we see it
    with rasterio.open(masks_dir / "hydro_lake.tif") as src:
        lake_raw = src.read(1, window=win)
    with rasterio.open(masks_dir / "hydro_lake_wl.tif") as src:
        wl_raw = src.read(1, window=win).astype(np.float32)
    if wl_raw.max() <= 1.5:
        wl_raw = wl_raw * 65535.0
    lake_wl_mc = np.interp(wl_raw.ravel(), gaea_in, mc_y_out
                            ).reshape(wl_raw.shape).astype(np.float32)
    underwater = (lake_raw > 0) & (height < lake_wl_mc) & ~ocean
    base[underwater] = [0.20, 0.55, 0.75]

    # Skeletonized centerline (the actual carver input)
    base[cl] = [1.0, 0.85, 0.15]

    rgb = (base * 255).astype(np.uint8)
    Image.fromarray(rgb).save(args.out, optimize=True)
    print(f"Saved {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
