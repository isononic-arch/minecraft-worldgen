"""
diag_biome_rivers.py — Layer river/lake overlay onto biome map.

Renders biome colors from override.tif at 1:16 (same as river overview),
then overlays rivers (centerline, braid, order, river.tif) and
terrain-intersection lakes on top.

Output: output/biome_rivers_overlay.png
"""
from __future__ import annotations
import sys, time
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window
from PIL import Image, ImageDraw
from scipy.ndimage import binary_dilation, maximum_filter as maxfilt

MASKS_DIR = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\biome_rivers_overlay.png")

TILE = 512
GRID_N = 97
DS = 16  # downsample factor — each tile = 32px

# Zone code → biome name
ZONE_BIOME_MAP = {
    0: "_OCEAN", 10: "COASTAL_HEATH", 20: "TEMPERATE_RAINFOREST",
    30: "BOREAL_TAIGA", 35: "SNOWY_BOREAL_TAIGA", 40: "ALPINE_MEADOW",
    50: "ARCTIC_TUNDRA", 55: "FROZEN_FLATS", 60: "TEMPERATE_DECIDUOUS",
    70: "RAINFOREST_COAST", 80: "RIPARIAN_WOODLAND", 90: "DRY_OAK_SAVANNA",
    100: "KARST_BARRENS", 110: "BIRCH_FOREST", 115: "EASTERN_TEMPERATE_COAST",
    120: "MIXED_FOREST", 130: "CONTINENTAL_STEPPE", 140: "DRY_PINE_BARRENS",
    150: "SCRUBBY_HEATHLAND", 160: "LUSH_RAINFOREST_COAST",
    170: "SAND_DUNE_DESERT", 190: "DESERT_STEPPE_TRANSITION",
    200: "SEMI_ARID_SHRUBLAND", 210: "DRY_WOODLAND_MAQUIS",
    220: "TIDAL_JUNGLE_FRINGE", 230: "MANGROVE_COAST", 240: "FRESHWATER_FEN",
}

BIOME_COLORS = {
    "_OCEAN":                  ( 30,  80, 160),
    "COASTAL_HEATH":           (180, 200, 140),
    "TEMPERATE_RAINFOREST":    ( 30, 120,  60),
    "BOREAL_TAIGA":            ( 60, 130,  90),
    "SNOWY_BOREAL_TAIGA":      (180, 200, 220),
    "ALPINE_MEADOW":           (140, 180, 100),
    "ARCTIC_TUNDRA":           (220, 230, 240),
    "FROZEN_FLATS":            (240, 245, 255),
    "TEMPERATE_DECIDUOUS":     ( 80, 160,  80),
    "RAINFOREST_COAST":        ( 20, 160,  80),
    "RIPARIAN_WOODLAND":       ( 60, 140, 100),
    "DRY_OAK_SAVANNA":         (190, 160,  80),
    "KARST_BARRENS":           (180, 170, 150),
    "BIRCH_FOREST":            (160, 200, 140),
    "EASTERN_TEMPERATE_COAST": (120, 180, 130),
    "MIXED_FOREST":            ( 60, 140,  70),
    "CONTINENTAL_STEPPE":      (200, 180, 100),
    "DRY_PINE_BARRENS":        (140, 160, 100),
    "SCRUBBY_HEATHLAND":       (180, 160, 120),
    "LUSH_RAINFOREST_COAST":   ( 20, 140,  80),
    "SAND_DUNE_DESERT":        (230, 200, 120),
    "DESERT_STEPPE_TRANSITION":(210, 185, 120),
    "SEMI_ARID_SHRUBLAND":     (200, 170, 110),
    "DRY_WOODLAND_MAQUIS":     (170, 160, 100),
    "TIDAL_JUNGLE_FRINGE":     ( 40, 150, 100),
    "MANGROVE_COAST":          ( 50, 140,  90),
    "FRESHWATER_FEN":          ( 80, 150, 130),
    "default":                 (128, 128, 128),
}


def _build_color_lut():
    lut = np.full((256, 3), BIOME_COLORS["default"], dtype=np.uint8)
    for code, biome in ZONE_BIOME_MAP.items():
        if biome in BIOME_COLORS:
            lut[code] = BIOME_COLORS[biome]
    return lut


def main():
    t0 = time.perf_counter()

    total_px = GRID_N * TILE
    out_sz = total_px // DS
    win = Window(0, 0, total_px, total_px)

    def read_mask(name, resamp=Resampling.average):
        with rasterio.open(str(MASKS_DIR / f"{name}.tif")) as src:
            return src.read(1, window=win, out_shape=(out_sz, out_sz),
                           resampling=resamp)

    # ── Biome base layer ─────────────────────────────────────────────
    print("Reading override.tif for biome colors...", flush=True)
    override = read_mask("override", Resampling.nearest).astype(np.uint8)
    color_lut = _build_color_lut()
    comp = color_lut[override]  # (H, W, 3)

    # ── Light hillshade for depth ────────────────────────────────────
    print("Reading height for hillshade...", flush=True)
    height_raw = read_mask("height")
    height = height_raw.astype(np.float32) / (65535.0 if height_raw.dtype == np.uint16 else 1.0)

    from scipy.ndimage import gaussian_filter
    h_smooth = gaussian_filter(height, sigma=1.5)
    gy, gx = np.gradient(h_smooth)
    shade = (-gx + gy) / (np.sqrt(gx**2 + gy**2 + 0.001) + 0.001)
    shade = np.clip(shade * 0.3 + 0.5, 0.25, 0.75)

    # Blend hillshade into biome colors (subtle)
    comp = np.clip(comp.astype(np.float32) * shade[..., None] * 1.6, 0, 255).astype(np.uint8)

    # ── Water layers ─────────────────────────────────────────────────
    print("Reading hydro masks...", flush=True)
    sea_norm = 17050.0 / 65535.0
    is_sea = height < sea_norm
    comp[is_sea] = (30, 80, 160)

    order = read_mask("hydro_order", Resampling.nearest).astype(np.uint8)
    cl = read_mask("hydro_centerline", Resampling.nearest).astype(np.uint8)
    lake = read_mask("hydro_lake", Resampling.nearest)
    river_raw = read_mask("river", Resampling.average)
    river = river_raw.astype(np.float32) / (65535.0 if river_raw.dtype == np.uint16 else 1.0)

    lake_wl_raw = read_mask("hydro_lake_wl", Resampling.nearest)
    lake_wl = lake_wl_raw.astype(np.float32)
    if lake_wl_raw.dtype == np.uint16:
        lake_wl = lake_wl / 65535.0

    # Lakes: terrain intersection
    lake_basin = lake > 0
    basin_expand = 32 // DS
    lake_basin_exp = binary_dilation(lake_basin, iterations=basin_expand)
    lake_wl_exp = maxfilt(lake_wl, size=basin_expand * 2 + 1)
    lake_mask = lake_basin_exp & (lake_wl_exp > 0) & (height < lake_wl_exp)

    # Paint water
    WATER_COLOR = np.array([50, 110, 190], dtype=np.uint8)
    RIVER_COLOR = np.array([65, 130, 215], dtype=np.uint8)
    BRAID_COLOR = np.array([55, 115, 195], dtype=np.uint8)

    comp[lake_mask] = WATER_COLOR

    braid_mask = (cl == 255) & ~lake_mask
    comp[braid_mask] = BRAID_COLOR

    thin_mask = (cl > 0) & (cl < 255) & ~lake_mask
    comp[thin_mask] = RIVER_COLOR

    order_only = (order > 0) & ~thin_mask & ~braid_mask & ~lake_mask
    comp[order_only] = (80, 140, 200)

    river_only = (river > 0.15) & (order == 0) & (cl == 0) & ~lake_mask
    comp[river_only] = (100, 150, 195)

    # ── Tile grid ────────────────────────────────────────────────────
    print("Drawing grid...", flush=True)
    img = Image.fromarray(comp, "RGB")
    draw = ImageDraw.Draw(img)

    tile_px = TILE // DS
    for i in range(GRID_N + 1):
        x = i * tile_px
        draw.line([(x, 0), (x, out_sz - 1)], fill=(40, 40, 40, 80), width=1)
    for j in range(GRID_N + 1):
        y = j * tile_px
        draw.line([(0, y), (out_sz - 1, y)], fill=(40, 40, 40, 80), width=1)

    # Sparse tile labels
    for i in range(GRID_N):
        for j in range(GRID_N):
            if i % 4 == 0 and j % 4 == 0:
                draw.text((i * tile_px + 2, j * tile_px + 2),
                          f"{i},{j}", fill=(200, 200, 200))

    # ── Legend ────────────────────────────────────────────────────────
    legend_y = out_sz - 20
    draw.rectangle([(0, legend_y - 5), (out_sz, out_sz)], fill=(20, 20, 20))
    x = 10
    for label, color in [("River", (65, 130, 215)), ("Braid", (55, 115, 195)),
                          ("Lake", (50, 110, 190)), ("Ocean", (30, 80, 160))]:
        draw.rectangle([(x, legend_y), (x + 12, legend_y + 12)], fill=color)
        draw.text((x + 16, legend_y), label, fill=(200, 200, 200))
        x += 80

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"\nSaved: {OUTPUT}  ({out_sz}x{out_sz}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
