"""
diag_river_3x3_topdown.py — Render 3×3 topdown of lake area using
the actual carve_rivers() pipeline output (padded NMS + lake terrain
intersection + outlet stitching).

Flat blue water on grey terrain, matching the river_3x3_lake_v3.png style.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR   = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG_PATH = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
OUTPUT      = Path(r"C:\Users\nicho\minecraft-worldgen\output\river_3x3_topdown_padded_nms.png")

CENTRE_TX, CENTRE_TZ = 51, 53
TILE = 512
GRID_X = 7   # tiles wide
GRID_Z = 3   # tiles tall
# (HALF removed — tx0/tz0 computed from GRID_X/GRID_Z directly)

# Water colour (flat blue matching your reference)
WATER_RGB = (70, 130, 210)
LAND_RGB  = (160, 160, 160)


def main():
    t0 = time.perf_counter()

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    from core.tile_streamer import read_tile
    from core.river_carver_v2 import carve_rivers

    tx0 = CENTRE_TX - GRID_X // 2  # 48
    tz0 = CENTRE_TZ - GRID_Z // 2  # 52

    region_h = GRID_Z * TILE
    region_w = GRID_X * TILE
    composite = np.full((region_h, region_w, 3), LAND_RGB, dtype=np.uint8)

    for gi in range(GRID_X):
        for gj in range(GRID_Z):
            tx = tx0 + gi
            tz = tz0 + gj
            print(f"Processing tile ({tx},{tz}) ...")

            col_off = tx * TILE
            row_off = tz * TILE
            masks = read_tile(MASKS_DIR, col_off, row_off, TILE, TILE)

            # Column generation — just need surface_y from height
            from core.river_carver_v2 import _height_norm_to_mc_y
            surface_y = _height_norm_to_mc_y(masks["height"], cfg).astype(np.int16)

            surface_y_carved, river_meta = carve_rivers(
                surface_y     = surface_y,
                flow_tile     = masks["flow"],
                river_tile    = masks["river"],
                cfg           = cfg,
                hydro_order   = masks.get("hydro_order"),
                hydro_width   = masks.get("hydro_width"),
                hydro_depth   = masks.get("hydro_depth"),
                hydro_lake    = masks.get("hydro_lake"),
                hydro_lkdep   = masks.get("hydro_lkdep"),
                hydro_lake_wl = masks.get("hydro_lake_wl"),
                hydro_centerline = masks.get("hydro_centerline"),
                height_norm   = masks["height"],
                masks_dir     = MASKS_DIR,
                tile_x        = tx,
                tile_z        = tz,
            )

            # Any pixel with river_meta > 0 is water
            water = river_meta > 0
            r0 = gj * TILE
            c0 = gi * TILE
            tile_rgb = composite[r0:r0+TILE, c0:c0+TILE]
            tile_rgb[water] = WATER_RGB

            water_pct = water.sum() * 100 / (TILE * TILE)
            print(f"  water: {water.sum()} px ({water_pct:.1f}%)")

    # Tile grid overlay
    img = Image.fromarray(composite, "RGB")
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
    elapsed = time.perf_counter() - t0
    print(f"\nSaved: {OUTPUT}  ({region_w}×{region_h}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
