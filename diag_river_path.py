"""
diag_river_path.py — Render the river path from mountain to lowland
around the lake area we've been working on.

Wider view than the 7x3 tile diagnostic but using 1:8 precomputed
masks for speed. Shows terrain + rivers + lakes in context.
"""

from __future__ import annotations
import sys, time
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window
from PIL import Image, ImageDraw

MASKS_DIR = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\river_world_overview.png")

# Region to render — full world (97x97 tiles)
TX_MIN, TX_MAX = 0, 97
TZ_MIN, TZ_MAX = 0, 97
TILE = 512


def main():
    t0 = time.perf_counter()
    print("Reading region masks at 50k...", flush=True)

    col0 = TX_MIN * TILE
    row0 = TZ_MIN * TILE
    w = (TX_MAX - TX_MIN) * TILE
    h = (TZ_MAX - TZ_MIN) * TILE
    win = Window(col0, row0, w, h)

    # Read at 1/16 for full world (each tile = 32px, total ~3100x3100)
    ds = 16
    out_h, out_w = h // ds, w // ds

    def read_win(name, resamp=Resampling.average):
        with rasterio.open(str(MASKS_DIR / f"{name}.tif")) as src:
            return src.read(1, window=win, out_shape=(out_h, out_w),
                           resampling=resamp)

    height_raw = read_win("height")
    height = height_raw.astype(np.float32) / (65535.0 if height_raw.dtype == np.uint16 else 1.0)

    order = read_win("hydro_order", Resampling.nearest).astype(np.uint8)
    cl = read_win("hydro_centerline", Resampling.nearest).astype(np.uint8)
    lake = read_win("hydro_lake", Resampling.nearest)
    river_raw = read_win("river", Resampling.average)
    river = river_raw.astype(np.float32) / (65535.0 if river_raw.dtype == np.uint16 else 1.0)

    # Lake water level for terrain intersection
    lake_wl_raw = read_win("hydro_lake_wl", Resampling.nearest)
    lake_wl = lake_wl_raw.astype(np.float32)
    if lake_wl_raw.dtype == np.uint16:
        lake_wl = lake_wl / 65535.0

    print(f"  Region: tiles ({TX_MIN},{TZ_MIN})-({TX_MAX},{TZ_MAX}), "
          f"render {out_w}x{out_h}px", flush=True)
    print(f"  Read done in {time.perf_counter()-t0:.1f}s", flush=True)

    # ── Terrain shading ──────────────────────────────────────────────
    from scipy.ndimage import gaussian_filter
    h_smooth = gaussian_filter(height, sigma=1.5)
    gy, gx = np.gradient(h_smooth)
    shade = (-gx + gy) / (np.sqrt(gx**2 + gy**2 + 0.001) + 0.001)
    shade = np.clip(shade * 0.4 + 0.5, 0.15, 0.85)

    # Sea level in normalised height
    sea_norm = 17050.0 / 65535.0

    # Terrain colour
    terrain_v = np.clip(height * 0.5 + shade * 0.5, 0.05, 0.95)

    # Green lowlands → brown mid → grey-white peaks
    is_sea = height < sea_norm
    elev_frac = np.clip((height - sea_norm) / (1.0 - sea_norm), 0, 1)

    r_ch = np.where(is_sea, 40,
           np.clip(100 + elev_frac * 120 + shade * 30, 80, 240)).astype(np.uint8)
    g_ch = np.where(is_sea, 60,
           np.clip(140 - elev_frac * 60 + shade * 30, 80, 210)).astype(np.uint8)
    b_ch = np.where(is_sea, 100,
           np.clip(80 - elev_frac * 20 + shade * 20, 50, 180)).astype(np.uint8)

    comp = np.stack([r_ch, g_ch, b_ch], axis=-1)

    # Ocean
    comp[is_sea] = (40, 60, 100)

    # ── Paint water ──────────────────────────────────────────────────
    # Lakes: terrain intersection (height < spill_elevation)
    # This matches the actual in-game lake carver — organic shorelines
    # following terrain contours, not the blocky hydro_lake seed mask.
    # Must expand the basin like the carver does (32px at 50k = 8px at 1/4)
    from scipy.ndimage import binary_dilation, maximum_filter as maxfilt
    lake_basin = lake > 0
    basin_expand = 32 // ds  # 8 px at 1/4 res
    lake_basin_exp = binary_dilation(lake_basin, iterations=basin_expand)
    # Propagate water level into expanded basin
    lake_wl_exp = maxfilt(lake_wl, size=basin_expand * 2 + 1)
    lake_mask = lake_basin_exp & (lake_wl_exp > 0) & (height < lake_wl_exp)
    lake_px = lake_mask.sum()
    basin_px = lake_basin.sum()
    print(f"  Lakes: {lake_px} px terrain-intersection "
          f"(basin seed: {basin_px} px, expanded: {lake_basin_exp.sum()} px)",
          flush=True)
    comp[lake_mask] = (50, 100, 180)

    braid_mask = cl == 255
    comp[braid_mask & ~lake_mask] = (55, 115, 195)

    thin_mask = (cl > 0) & (cl < 255)
    comp[thin_mask & ~lake_mask] = (65, 130, 215)

    # Order channels not in centerline
    order_only = (order > 0) & ~thin_mask & ~braid_mask & ~lake_mask
    comp[order_only] = (80, 140, 200)

    # Raw river.tif not in hydro
    river_only = (river > 0.15) & (order == 0) & (cl == 0) & ~lake_mask
    comp[river_only] = (100, 150, 195)

    # ── Tile grid + labels ───────────────────────────────────────────
    img = Image.fromarray(comp, "RGB")
    draw = ImageDraw.Draw(img)

    tile_px = TILE // ds  # 128 px per tile
    n_tx = TX_MAX - TX_MIN
    n_tz = TZ_MAX - TZ_MIN

    for i in range(n_tx + 1):
        x = i * tile_px
        draw.line([(x, 0), (x, out_h - 1)], fill=(60, 60, 60), width=1)
    for j in range(n_tz + 1):
        y = j * tile_px
        draw.line([(0, y), (out_w - 1, y)], fill=(60, 60, 60), width=1)

    for i in range(n_tx):
        for j in range(n_tz):
            tx = TX_MIN + i
            tz = TZ_MIN + j
            if tx % 2 == 0 and tz % 2 == 0:
                draw.text((i * tile_px + 3, j * tile_px + 3),
                          f"{tx},{tz}", fill=(200, 200, 200))

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"\nSaved: {OUTPUT}  ({out_w}×{out_h}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
