"""
diag_world_real_lakes.py — World-scale topdown:
    hillshade + REAL lakes (terrain-intersection only) + rivers + dropped
    orange basin shore (so we can see where rivers SHOULD have connected).

Output is downscaled to 6250x6250 (1:8) for tractable rendering.

Usage:
    py tools/diag_world_real_lakes.py --out memory/world_real_lakes.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
import matplotlib.pyplot as plt
from PIL import Image

SEA_LEVEL_RAW = 17050
SCALE = 8  # source 50000 → working 6250


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    masks_dir = Path(args.masks)

    print("Reading 50k masks at 1:8 working scale...", file=sys.stderr)

    def _read_downscaled(name, dtype=None):
        with rasterio.open(str(masks_dir / f"{name}.tif")) as src:
            arr = src.read(1, out_shape=(src.height // SCALE, src.width // SCALE),
                           resampling=rasterio.enums.Resampling.nearest)
        return arr if dtype is None else arr.astype(dtype)

    height = _read_downscaled("height")  # uint16
    lake_id = _read_downscaled("hydro_lake")
    lake_wl_norm = _read_downscaled("hydro_lake_wl").astype(np.float32)
    centerline = _read_downscaled("hydro_centerline")

    H, W = height.shape
    print(f"  shape: {H}x{W}", file=sys.stderr)

    gaea_in = np.array([0, SEA_LEVEL_RAW, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    height_blocks = np.interp(
        height.ravel(), gaea_in, mc_y_out
    ).reshape(height.shape).astype(np.float32)
    lake_wl_mc = np.interp(
        (lake_wl_norm * 65535.0).ravel(), gaea_in, mc_y_out
    ).reshape(lake_wl_norm.shape).astype(np.float32)

    basin_dry = (lake_id > 0) & (height > SEA_LEVEL_RAW)
    underwater = basin_dry & (height_blocks < lake_wl_mc)
    basin_dry = basin_dry & ~underwater  # only the SHORE, not the lake itself
    river = centerline > 0
    ocean = height <= SEA_LEVEL_RAW

    # Hillshade base
    norm = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height_blocks)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1)

    img = base.copy()
    img[ocean] = [0.13, 0.27, 0.49]
    img[basin_dry] = [0.95, 0.55, 0.20]   # orange = dropped shore
    img[underwater] = [0.20, 0.50, 0.80]  # blue = real lake
    img[river] = [0.10, 0.40, 0.85]       # blue = rivers

    rgb = (img * 255).astype(np.uint8)

    # Save full
    Image.fromarray(rgb).save(args.out, optimize=True)
    print(f"Saved {args.out}  ({rgb.shape[1]}x{rgb.shape[0]})", file=sys.stderr)

    # Save 2k preview
    out_2k = args.out.replace(".png", "_2k.png")
    img_2k = Image.fromarray(rgb)
    img_2k.thumbnail((2000, 2000), Image.LANCZOS)
    img_2k.save(out_2k, optimize=True)
    print(f"Saved {out_2k}  ({img_2k.size[0]}x{img_2k.size[1]})", file=sys.stderr)

    print(f"Stats: real lakes {underwater.sum():,} px | "
          f"dropped basin shore {basin_dry.sum():,} px | "
          f"rivers {river.sum():,} px",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
