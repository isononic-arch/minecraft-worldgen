"""
diag_centerline_compare.py — Compare hydro_order vs hydro_centerline
for the 7x3 tile range to see what the global NMS suppressed.

Left = hydro_order (all river pixels), Right = hydro_centerline (surviving NMS).
"""

from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import rasterio
from rasterio.windows import Window
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\centerline_compare.png")

CENTRE_TX, CENTRE_TZ = 51, 53
TILE = 512
GRID_X, GRID_Z = 7, 3

LAND_RGB  = (160, 160, 160)
ORDER_RGB = (180, 80, 80)    # red = order but NOT in centerline (suppressed)
CL_RGB    = (70, 130, 210)   # blue = surviving centerline
BRAID_RGB = (40, 200, 120)   # green = braid fill (255)


def main():
    tx0 = CENTRE_TX - GRID_X // 2  # 48
    tz0 = CENTRE_TZ - GRID_Z // 2  # 52

    region_w = GRID_X * TILE
    region_h = GRID_Z * TILE

    # Read the relevant window from both masks
    col0 = tx0 * TILE
    row0 = tz0 * TILE
    win = Window(col0, row0, region_w, region_h)

    with rasterio.open(str(MASKS_DIR / "hydro_order.tif")) as src:
        order = src.read(1, window=win)
    print(f"hydro_order: shape={order.shape}, river_px={(order>0).sum()}")

    with rasterio.open(str(MASKS_DIR / "hydro_centerline.tif")) as src:
        cl = src.read(1, window=win)
    print(f"hydro_centerline: shape={cl.shape}, "
          f"thin_px={(cl>0 & (cl<255)).sum()}, "
          f"braid_px={(cl==255).sum()}")

    # Composite: land background
    # Red = in order but NOT in centerline (suppressed)
    # Blue = thin centerline (1-5)
    # Green = braid fill (255)
    comp = np.full((region_h, region_w, 3), LAND_RGB, dtype=np.uint8)

    has_order = order > 0
    has_thin  = (cl > 0) & (cl < 255)
    has_braid = cl == 255
    suppressed = has_order & ~has_thin & ~has_braid

    comp[suppressed] = ORDER_RGB   # red: suppressed channels
    comp[has_thin]   = CL_RGB      # blue: surviving thin channels
    comp[has_braid]  = BRAID_RGB   # green: braid fill

    # Tile grid
    img = Image.fromarray(comp, "RGB")
    draw = ImageDraw.Draw(img)
    for i in range(GRID_X + 1):
        x = i * TILE
        if x < region_w:
            draw.line([(x, 0), (x, region_h - 1)], fill=(100, 100, 100), width=1)
    for j in range(GRID_Z + 1):
        y = j * TILE
        if y < region_h:
            draw.line([(0, y), (region_w - 1, y)], fill=(100, 100, 100), width=1)
    for gi in range(GRID_X):
        for gj in range(GRID_Z):
            tx = tx0 + gi
            tz = tz0 + gj
            draw.text((gi * TILE + 4, gj * TILE + 4),
                      f"({tx},{tz})", fill=(60, 60, 60))

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT))
    print(f"\nSaved: {OUTPUT}")
    print(f"Red = suppressed by NMS, Blue = thin centerline, Green = braid fill")


if __name__ == "__main__":
    main()
