"""
diag_river_overview.py — Render the full river network at 1:8 scale
with terrain shading, lakes, and river channels.

Reads precomputed masks directly — no per-tile carving, very fast.
"""

from __future__ import annotations
import sys, time
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from PIL import Image, ImageDraw

MASKS_DIR = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\river_overview.png")
SCALE     = 8
FULL      = 50_000
DS        = FULL // SCALE  # 6250


def read_ds(name, resamp=Resampling.average):
    with rasterio.open(str(MASKS_DIR / f"{name}.tif")) as src:
        return src.read(1, out_shape=(DS, DS), resampling=resamp)


def main():
    t0 = time.perf_counter()
    print("Reading masks at 1:8...", flush=True)

    height_raw = read_ds("height", Resampling.average)
    height = height_raw.astype(np.float32) / (65535.0 if height_raw.dtype == np.uint16 else 1.0)

    order = read_ds("hydro_order", Resampling.nearest).astype(np.uint8)
    lake  = read_ds("hydro_lake", Resampling.nearest)
    cl    = read_ds("hydro_centerline", Resampling.nearest).astype(np.uint8)

    # Also read river.tif for the thin channels not in hydro_order
    river_raw = read_ds("river", Resampling.average)
    river = river_raw.astype(np.float32) / (65535.0 if river_raw.dtype == np.uint16 else 1.0)

    print(f"  Read done in {time.perf_counter()-t0:.1f}s", flush=True)

    # ── Find river extent to crop ────────────────────────────────────
    has_water = (order > 0) | (lake > 0) | (cl > 0) | (river > 0.1)
    rows_any = np.any(has_water, axis=1)
    cols_any = np.any(has_water, axis=0)

    if not rows_any.any():
        print("No water found!"); return

    r0 = max(0, np.argmax(rows_any) - 50)
    r1 = min(DS, DS - np.argmax(rows_any[::-1]) + 50)
    c0 = max(0, np.argmax(cols_any) - 50)
    c1 = min(DS, DS - np.argmax(cols_any[::-1]) + 50)

    print(f"  Water extent: rows [{r0}:{r1}], cols [{c0}:{c1}] "
          f"({c1-c0}×{r1-r0} px at 1:8)", flush=True)

    # Crop
    h = height[r0:r1, c0:c1]
    o = order[r0:r1, c0:c1]
    lk = lake[r0:r1, c0:c1]
    c = cl[r0:r1, c0:c1]
    rv = river[r0:r1, c0:c1]
    H, W = h.shape

    # ── Terrain shading ──────────────────────────────────────────────
    # Hillshade-style grey terrain
    from scipy.ndimage import gaussian_filter
    h_smooth = gaussian_filter(h, sigma=1.0)
    gy, gx = np.gradient(h_smooth)
    # Simple directional hillshade (NW light)
    shade = (-gx + gy) / (np.sqrt(gx**2 + gy**2 + 0.01) + 0.001)
    shade = np.clip(shade * 0.5 + 0.5, 0.15, 0.85)

    # Base terrain RGB: height-tinted grey
    terrain_v = np.clip(h * 0.6 + shade * 0.4, 0.05, 0.95)
    # Color ramp: low=dark green-grey, mid=grey-brown, high=light grey
    r_ch = np.clip(terrain_v * 180 + 40, 60, 220).astype(np.uint8)
    g_ch = np.clip(terrain_v * 170 + 50, 70, 210).astype(np.uint8)
    b_ch = np.clip(terrain_v * 150 + 40, 50, 190).astype(np.uint8)

    comp = np.stack([r_ch, g_ch, b_ch], axis=-1)

    # ── Paint water ──────────────────────────────────────────────────
    # Lakes: deep blue
    lake_mask = lk > 0
    comp[lake_mask] = (50, 100, 180)

    # Braid fill: medium blue
    braid_mask = c == 255
    comp[braid_mask] = (60, 120, 200)

    # Centerline thin channels: blue
    thin_mask = (c > 0) & (c < 255)
    comp[thin_mask] = (70, 140, 220)

    # Order channels not in centerline: lighter blue
    order_only = (o > 0) & ~thin_mask & ~braid_mask
    comp[order_only] = (90, 150, 210)

    # Raw river.tif channels not in any hydro: faint blue
    river_only = (rv > 0.15) & (o == 0) & (c == 0) & ~lake_mask
    comp[river_only] = (120, 160, 200)

    # ── Tile grid (light, every 64px = 1 tile at 1:8) ────────────────
    img = Image.fromarray(comp, "RGB")
    draw = ImageDraw.Draw(img)

    # Draw tile grid every 64 pixels (= 512 blocks = 1 tile)
    tile_sz = 64  # 512/8
    for x in range(0, W, tile_sz):
        draw.line([(x, 0), (x, H-1)], fill=(80, 80, 80, 60), width=1)
    for y in range(0, H, tile_sz):
        draw.line([(0, y), (W-1, y)], fill=(80, 80, 80, 60), width=1)

    # Label key tiles
    for gi in range(0, W, tile_sz):
        for gj in range(0, H, tile_sz):
            tx = (c0 + gi) // tile_sz
            tz = (r0 + gj) // tile_sz
            # Only label every 2nd tile to reduce clutter
            if tx % 2 == 0 and tz % 2 == 0:
                draw.text((gi + 2, gj + 2), f"{tx},{tz}",
                          fill=(40, 40, 40))

    # Upscale 2x for visibility
    w2, h2 = img.size
    img = img.resize((w2 * 2, h2 * 2), Image.NEAREST)

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"\nSaved: {OUTPUT}  ({w2*2}×{h2*2}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
