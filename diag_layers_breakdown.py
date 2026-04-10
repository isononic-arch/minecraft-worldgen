"""
diag_layers_breakdown.py — Fast layer-by-layer breakdown for one tile.

Reads every relevant mask + runs eco_gradients only (no column_generator,
no river_carver, no surface_decorator). Renders 12 panels in a grid:

  1. Override biome (zone colors)
  2. Elevation (greyscale)
  3. Slope (greyscale)
  4. rock_exposure raw
  5. rock_exposure_tight raw
  6. snow_caps raw
  7. sand_dunes raw
  8. hydro_floodplain raw
  9. wind_windthrow raw
 10. gap_mask (final, color-coded)
 11. Biome inheritance sources (where alpine pixels pull biome from)
 12. Combined: gap mask overlaid on hillshaded elevation

Usage: python diag_layers_breakdown.py --tile-x 24 --tile-z 80
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
import rasterio
from rasterio.windows import Window

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR  = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG     = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
TILE = 512

OVERRIDE_BIOME_MAP = {
    0:"", 10:"COASTAL_HEATH", 20:"TEMPERATE_RAINFOREST", 30:"BOREAL_TAIGA",
    35:"SNOWY_BOREAL_TAIGA", 40:"ALPINE_MEADOW", 50:"ARCTIC_TUNDRA", 55:"FROZEN_FLATS",
    60:"TEMPERATE_DECIDUOUS", 70:"RAINFOREST_COAST", 80:"RIPARIAN_WOODLAND",
    90:"DRY_OAK_SAVANNA", 100:"KARST_BARRENS", 110:"BIRCH_FOREST",
    115:"EASTERN_TEMPERATE_COAST", 120:"MIXED_FOREST", 130:"CONTINENTAL_STEPPE",
    140:"DRY_PINE_BARRENS", 150:"SCRUBBY_HEATHLAND", 160:"LUSH_RAINFOREST_COAST",
    170:"SAND_DUNE_DESERT", 190:"DESERT_STEPPE_TRANSITION", 200:"SEMI_ARID_SHRUBLAND",
    210:"DRY_WOODLAND_MAQUIS", 220:"TIDAL_JUNGLE_FRINGE", 230:"MANGROVE_COAST",
    240:"FRESHWATER_FEN",
}

BIOME_COLORS = {
    "COASTAL_HEATH": (180,200,150), "TEMPERATE_RAINFOREST": (20,100,40),
    "BOREAL_TAIGA": (40,80,60), "SNOWY_BOREAL_TAIGA": (200,220,240),
    "MIXED_FOREST": (60,130,50), "ALPINE_MEADOW": (160,190,140),
    "ARCTIC_TUNDRA": (220,230,245), "FROZEN_FLATS": (230,240,250),
    "TEMPERATE_DECIDUOUS": (80,150,60), "BIRCH_FOREST": (140,180,80),
    "DRY_OAK_SAVANNA": (190,150,80), "KARST_BARRENS": (170,160,140),
    "CONTINENTAL_STEPPE": (180,170,100), "DRY_PINE_BARRENS": (120,130,70),
    "SCRUBBY_HEATHLAND": (150,160,100), "DRY_WOODLAND_MAQUIS": (160,130,60),
    "SAND_DUNE_DESERT": (220,200,140), "DESERT_STEPPE_TRANSITION": (200,180,120),
    "SEMI_ARID_SHRUBLAND": (190,170,110), "RAINFOREST_COAST": (10,80,30),
    "EASTERN_TEMPERATE_COAST": (100,160,80), "LUSH_RAINFOREST_COAST": (20,90,45),
}

GAP_COLORS = {
    0: (60, 130, 60),    # forest green (regular biome)
    1: (180, 220, 100),  # meadow lime
    2: (200, 150, 60),   # windthrow amber
    4: (60, 100, 200),   # floodplain blue
    5: (140, 140, 140),  # rock grey
    6: (130, 200, 100),  # alpine green
    7: (245, 250, 255),  # snow white
    8: (220, 200, 140),  # sand tan
}


def read(name, co, ro):
    path = MASKS_DIR / f"{name}.tif"
    if not path.exists():
        return np.zeros((TILE, TILE), dtype=np.float32), False
    with rasterio.open(str(path)) as src:
        d = src.read(1, window=Window(co, ro, TILE, TILE))
        if d.dtype == np.uint8: return d.astype(np.float32) / 255.0, True
        if d.dtype == np.uint16: return d.astype(np.float32) / 65535.0, True
        return d.astype(np.float32), True


def heatmap(arr, mask=None):
    """Convert [0,1] gradient to RGB heatmap (black→blue→cyan→yellow→red)."""
    h = np.clip(arr, 0, 1)
    rgb = np.zeros((*h.shape, 3), dtype=np.uint8)
    rgb[:,:,0] = np.clip(h * 510 - 100, 0, 255)
    rgb[:,:,1] = np.clip(np.where(h < 0.5, h * 510, (1-h) * 510), 0, 255)
    rgb[:,:,2] = np.clip(255 - h * 510, 0, 255)
    if mask is not None:
        rgb[~mask] = (30, 30, 30)
    return rgb


def greyscale(arr, mask=None):
    """Normalized [0,1] → grey."""
    g = np.clip(arr * 255, 0, 255).astype(np.uint8)
    rgb = np.stack([g, g, g], axis=-1)
    if mask is not None:
        rgb[~mask] = (30, 30, 30)
    return rgb


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tile-x", type=int, required=True)
    p.add_argument("--tile-z", type=int, required=True)
    args = p.parse_args()

    t0 = time.perf_counter()
    tx, tz = args.tile_x, args.tile_z
    co, ro = tx * TILE, tz * TILE

    print(f"Reading masks for tile ({tx},{tz}) ...", flush=True)

    height, _ = read("height", co, ro)
    slope, _ = read("slope", co, ro)
    flow, _ = read("flow", co, ro)
    erosion, _ = read("erosion", co, ro)
    override_f, _ = read("override", co, ro)
    rock_exp, _ = read("rock_exposure", co, ro)
    rock_tight, _ = read("rock_exposure_tight", co, ro)
    snow, _ = read("snow_caps", co, ro)
    sand, _ = read("sand_dunes", co, ro)
    flood, _ = read("hydro_floodplain", co, ro)
    wind, _ = read("wind_windthrow", co, ro)

    # Crude surface_y from height
    sy = np.interp(np.round(height*65535).astype(np.uint16).astype(np.float32),
                   [0, 17050, 45000, 65496], [-64, 63, 200, 448]).astype(np.int16)
    land_mask = sy >= 63
    cliff_deg = slope * 90.0

    # Build biome grid
    override_u8 = np.round(override_f * 255).astype(np.uint8)
    biome_grid = np.empty((TILE, TILE), dtype=object)
    biome_grid[:] = ""
    biomes_present = {}
    for code, name in OVERRIDE_BIOME_MAP.items():
        if not name: continue
        m = override_u8 == code
        if m.any():
            biome_grid[m] = name
            biomes_present[name] = int(m.sum())

    print(f"Biomes present: {biomes_present}")

    # Run eco_gradients
    print("Running eco_gradients ...", flush=True)
    from core import eco_gradients
    with open(CONFIG) as f: cfg = json.load(f)

    eco = eco_gradients.compute_eco_gradients(
        sy, flow, erosion, cliff_deg,
        np.zeros_like(height), np.zeros_like(height), np.zeros_like(height),
        land_mask, cfg, None, tx, tz, biome_grid,
        hydro_floodplain=flood, wind_windthrow=wind,
        rock_exposure=rock_exp, rock_exposure_tight=rock_tight,
        snow_caps=snow, sand_dunes=sand)

    g = eco.gap_mask
    print(f"\ngap_mask values:")
    for v in [0,1,2,4,5,6,7,8]:
        c = (g==v).sum()
        if c: print(f"  gap=={v}: {100*c/g.size:.1f}%")

    # ── Render 12 panels in a 4×3 grid ─────────────────────────────
    PAD = 4
    LH = 22
    pw = TILE
    ph = TILE + LH
    cols, rows = 4, 3
    img_w = cols * pw + (cols - 1) * PAD
    img_h = rows * ph + (rows - 1) * PAD
    canvas = Image.new("RGB", (img_w, img_h), (20, 20, 20))
    draw = ImageDraw.Draw(canvas)

    def paste(arr_rgb, row, col, label):
        x = col * (pw + PAD)
        y = row * (ph + PAD)
        canvas.paste(Image.fromarray(arr_rgb.astype(np.uint8)), (x, y + LH))
        draw.text((x + 4, y + 4), label, fill=(255, 255, 200))

    # 1: Override biome
    p1 = np.full((TILE, TILE, 3), (40, 40, 40), dtype=np.uint8)
    for bname, color in BIOME_COLORS.items():
        m = biome_grid == bname
        if m.any():
            p1[m] = color
    paste(p1, 0, 0, "1: Biome (override)")

    # 2: Elevation
    sy_norm = (sy.astype(np.float32) - sy.min()) / max(float(sy.max() - sy.min()), 1.0)
    paste(greyscale(sy_norm, land_mask), 0, 1, f"2: Elevation Y={int(sy.min())}-{int(sy.max())}")

    # 3: Slope (slope.tif raw)
    paste(heatmap(slope, land_mask), 0, 2, f"3: slope.tif (max={slope.max():.2f})")

    # 4: rock_exposure raw
    paste(heatmap(rock_exp, land_mask), 0, 3, f"4: rock_exposure (mean={rock_exp.mean():.2f})")

    # 5: rock_exposure_tight raw
    paste(heatmap(rock_tight, land_mask), 1, 0, f"5: rock_tight (mean={rock_tight.mean():.2f})")

    # 6: snow_caps raw
    paste(heatmap(snow, land_mask), 1, 1, f"6: snow_caps (mean={snow.mean():.2f})")

    # 7: sand_dunes raw
    paste(heatmap(sand, land_mask), 1, 2, f"7: sand_dunes (mean={sand.mean():.2f})")

    # 8: hydro_floodplain
    paste(heatmap(flood, land_mask), 1, 3, f"8: floodplain (mean={flood.mean():.2f})")

    # 9: wind_windthrow
    paste(heatmap(wind, land_mask), 2, 0, f"9: windthrow (mean={wind.mean():.2f})")

    # 10: gap_mask
    p10 = np.full((TILE, TILE, 3), (20, 20, 60), dtype=np.uint8)
    for v, color in GAP_COLORS.items():
        m = g == v
        if m.any():
            p10[m] = color
    paste(p10, 2, 1, "10: gap_mask")

    # 11: Alpine biome inheritance source
    p11 = np.full((TILE, TILE, 3), (40, 40, 40), dtype=np.uint8)
    if hasattr(eco, 'alpine_biome_source'):
        for bname, color in BIOME_COLORS.items():
            m = eco.alpine_biome_source == bname
            if m.any():
                p11[m] = color
    paste(p11, 2, 2, "11: alpine biome source")

    # 12: gap_mask over hillshade
    gy_h = np.gradient(sy.astype(np.float32), axis=0)
    gx_h = np.gradient(sy.astype(np.float32), axis=1)
    mag = np.maximum(np.hypot(gx_h, gy_h), 1e-6)
    hs = np.clip((gx_h * np.sin(np.radians(315)) + gy_h * np.cos(np.radians(315))) / mag * 0.5 + 0.5, 0, 1)
    grey = (hs * 180 + 40).astype(np.uint8)
    p12 = np.stack([grey, grey, grey], axis=-1)
    p12[~land_mask] = (20, 20, 60)
    for v, color in GAP_COLORS.items():
        m = (g == v) & (v != 0)
        if m.any():
            p12[m] = (np.array(color) * 0.7 + np.array([grey[m].mean()]*3) * 0.3).astype(np.uint8)
    paste(p12, 2, 3, "12: gap on hillshade")

    out = Path("output") / f"layers_{tx}_{tz}.png"
    out.parent.mkdir(exist_ok=True)
    canvas.save(str(out))
    print(f"\nSaved: {out} ({img_w}x{img_h}) — {time.perf_counter()-t0:.1f}s")


if __name__ == "__main__":
    main()
