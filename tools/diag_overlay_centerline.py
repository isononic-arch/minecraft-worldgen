"""
diag_overlay_centerline.py - Overlay the precompute centerline directly on
top of the carved MCA topdown.  Reveals coordinate alignment + which
centerlines made it through to actual rivers vs which got dropped.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from PIL import Image
from scipy.ndimage import binary_dilation


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tile-x", type=int, required=True)
    p.add_argument("--tile-z", type=int, required=True)
    p.add_argument("--mca-png", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()

    x0 = args.tile_x * 512
    z0 = args.tile_z * 512
    win = Window(x0, z0, 512, 512)

    masks = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
    with rasterio.open(str(masks / "hydro_centerline.tif")) as src:
        cl = src.read(1, window=win)
    with rasterio.open(str(masks / "hydro_lake.tif")) as src:
        lake = src.read(1, window=win)
    with rasterio.open(str(masks / "hydro_lake_spill.tif")) as src:
        spill = src.read(1, window=win)

    mca = np.asarray(Image.open(args.mca_png).convert("RGB")).copy()

    # Dilate centerline 1 px for visibility, paint magenta on top
    cl_mask = binary_dilation(cl > 0, iterations=1)
    mca[cl_mask] = (255, 30, 220)  # magenta — precompute centerline
    # Lake outline (perimeter) in cyan-yellow
    lake_mask = lake > 0
    lake_edge = lake_mask & ~binary_dilation(lake_mask, iterations=-1)  # not quite, simpler:
    from scipy.ndimage import binary_erosion
    lake_edge = lake_mask & ~binary_erosion(lake_mask)
    mca[lake_edge] = (255, 255, 60)  # yellow — precompute lake outline
    # Spillpoints in bright red dots
    spill_dilated = binary_dilation(spill > 0, iterations=2)
    mca[spill_dilated] = (255, 50, 50)

    Image.fromarray(mca).save(args.out)
    print(f"Saved {args.out}")


if __name__ == "__main__":
    main()
