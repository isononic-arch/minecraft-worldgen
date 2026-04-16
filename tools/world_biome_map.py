"""
world_biome_map.py — Generate a full-world biome overview PNG.

Reads override.tif directly and maps zone codes → biome colours via LUT.
Does NOT use assign_biomes() — override is the sole biome source for display.

Usage:
    python tools/world_biome_map.py [--res 8] [--out output/world_biome_map.png]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import rasterio
from rasterio.windows import Window
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MASKS_DIR   = _PROJECT_ROOT / "masks"
OUTPUT_DIR  = _PROJECT_ROOT / "output"
CONFIG_PATH = _PROJECT_ROOT / "config" / "thresholds.json"

GRID_N    = 97          # tiles per axis
TILE_SIZE = 512         # source pixels per tile

# ---------------------------------------------------------------------------
# Biome colour palette
# ---------------------------------------------------------------------------
BIOME_COLORS: dict[str, tuple[int, int, int]] = {
    "_OCEAN":                  (  30,  80, 160),
    "COASTAL_HEATH":           ( 180, 200, 140),
    "TEMPERATE_RAINFOREST":    (  30, 120,  60),
    "BOREAL_TAIGA":            (  60, 130,  90),
    "SNOWY_BOREAL_TAIGA":      ( 180, 200, 220),
    # "ALPINE_MEADOW" retired S56
    "ARCTIC_TUNDRA":           ( 220, 230, 240),
    "FROZEN_FLATS":            ( 240, 245, 255),
    "TEMPERATE_DECIDUOUS":     (  80, 160,  80),
    "RAINFOREST_COAST":        (  20, 160,  80),
    "RIPARIAN_WOODLAND":       (  60, 140, 100),
    "DRY_OAK_SAVANNA":         ( 190, 160,  80),
    "KARST_BARRENS":           ( 180, 170, 150),
    "BIRCH_FOREST":            ( 160, 200, 140),
    "EASTERN_TEMPERATE_COAST": ( 120, 180, 130),
    "MIXED_FOREST":            (  60, 140,  70),
    "CONTINENTAL_STEPPE":      ( 200, 180, 100),
    "DRY_PINE_BARRENS":        ( 140, 160, 100),
    "SCRUBBY_HEATHLAND":       ( 180, 160, 120),
    "LUSH_RAINFOREST_COAST":   (  20, 140,  80),
    "SAND_DUNE_DESERT":        ( 230, 200, 120),
    "DESERT_STEPPE_TRANSITION":( 210, 185, 120),
    "SEMI_ARID_SHRUBLAND":     ( 200, 170, 110),
    "DRY_WOODLAND_MAQUIS":     ( 170, 160, 100),
    "TIDAL_JUNGLE_FRINGE":     (  40, 150, 100),
    "MANGROVE_COAST":          (  50, 140,  90),
    "FRESHWATER_FEN":          (  80, 150, 130),
    "FRESH_WATER":             (  80, 160, 200),
    "RIVER":                   (  60, 120, 200),
    "WETLAND":                 (  90, 140, 120),
    "default":                 ( 128, 128, 128),
}

def biome_color(name: str) -> tuple[int, int, int]:
    return BIOME_COLORS.get(name, BIOME_COLORS["default"])


# ---------------------------------------------------------------------------
# Zone code → biome name LUT (matches OVERRIDE_BIOME_MAP in biome_assignment.py)
# ---------------------------------------------------------------------------
ZONE_BIOME_MAP: dict[int, str] = {
    0: "_OCEAN",
    10: "COASTAL_HEATH", 20: "TEMPERATE_RAINFOREST", 30: "BOREAL_TAIGA",
    35: "SNOWY_BOREAL_TAIGA", 50: "ARCTIC_TUNDRA",  # 40 retired S56
    55: "FROZEN_FLATS", 60: "TEMPERATE_DECIDUOUS", 70: "RAINFOREST_COAST",
    80: "RIPARIAN_WOODLAND", 90: "DRY_OAK_SAVANNA", 100: "KARST_BARRENS",
    110: "BIRCH_FOREST", 115: "EASTERN_TEMPERATE_COAST", 120: "MIXED_FOREST",
    130: "CONTINENTAL_STEPPE", 140: "DRY_PINE_BARRENS", 150: "SCRUBBY_HEATHLAND",
    160: "LUSH_RAINFOREST_COAST", 170: "SAND_DUNE_DESERT",
    190: "DESERT_STEPPE_TRANSITION", 200: "SEMI_ARID_SHRUBLAND",
    210: "DRY_WOODLAND_MAQUIS", 220: "TIDAL_JUNGLE_FRINGE",
    230: "MANGROVE_COAST", 240: "FRESHWATER_FEN",
}

def _build_color_lut() -> np.ndarray:
    """Build a 256×3 uint8 LUT: index = zone code → RGB colour."""
    lut = np.full((256, 3), BIOME_COLORS["default"], dtype=np.uint8)
    for code, biome in ZONE_BIOME_MAP.items():
        if biome in BIOME_COLORS:
            lut[code] = BIOME_COLORS[biome]
    return lut

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_world_biome_map(res: int = 8, out_path: Path | None = None) -> Path:
    """
    Build a full-world biome map at `res` pixels per tile.
    Reads override.tif directly — no assign_biomes().
    Returns path to the saved PNG.
    """
    if out_path is None:
        out_path = OUTPUT_DIR / "world_biome_map.png"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    color_lut = _build_color_lut()

    img_w = GRID_N * res
    img_h = GRID_N * res
    canvas = np.zeros((img_h, img_w, 3), dtype=np.uint8)

    # Open override.tif once
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ov_ds = rasterio.open(MASKS_DIR / "override.tif")

    total = GRID_N * GRID_N
    t0 = time.time()
    done = 0

    for tz in range(GRID_N):
        for tx in range(GRID_N):
            try:
                row = tz * TILE_SIZE
                col = tx * TILE_SIZE
                win = Window(col, row, TILE_SIZE, TILE_SIZE)
                zone = ov_ds.read(1, window=win, out_shape=(res, res),
                                  resampling=rasterio.enums.Resampling.nearest).astype(np.uint8)

                # LUT lookup: zone code → RGB
                py = tz * res
                px = tx * res
                canvas[py:py+res, px:px+res] = color_lut[zone]

            except Exception:
                py = tz * res; px = tx * res
                canvas[py:py+res, px:px+res] = (80, 80, 80)

            done += 1
            if done % 500 == 0 or done == total:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta  = (total - done) / rate if rate > 0 else 0
                print(f"  {done}/{total} tiles  ({rate:.0f}/s, ETA {eta:.0f}s)", end="\r")

    print()

    # -----------------------------------------------------------------------
    # Add legend
    # -----------------------------------------------------------------------
    pil_img = Image.fromarray(canvas, "RGB")
    # Scale up 2× for readability
    pil_img = pil_img.resize((img_w * 2, img_h * 2), Image.NEAREST)

    # Build legend strip
    sorted_biomes = sorted(BIOME_COLORS.keys())
    swatch_h = 14
    swatch_w = 14
    label_w  = 200
    legend_cols = 3
    legend_rows = (len(sorted_biomes) + legend_cols - 1) // legend_cols
    leg_h = legend_rows * (swatch_h + 2) + 8
    leg_w = pil_img.width

    legend_img = Image.new("RGB", (leg_w, leg_h), (30, 30, 30))
    draw = ImageDraw.Draw(legend_img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 10)
    except Exception:
        font = ImageFont.load_default()

    col_width = leg_w // legend_cols
    for i, bname in enumerate(sorted_biomes):
        col_i = i % legend_cols
        row_i = i // legend_cols
        xoff  = col_i * col_width + 4
        yoff  = row_i * (swatch_h + 2) + 4
        rgb   = BIOME_COLORS.get(bname, BIOME_COLORS["default"])
        draw.rectangle([xoff, yoff, xoff + swatch_w, yoff + swatch_h], fill=rgb)
        label = bname.replace("_", " ").lower()
        draw.text((xoff + swatch_w + 4, yoff + 1), label, fill=(220, 220, 220), font=font)

    # Combine
    combined = Image.new("RGB", (pil_img.width, pil_img.height + leg_h))
    combined.paste(pil_img, (0, 0))
    combined.paste(legend_img, (0, pil_img.height))
    combined.save(str(out_path))

    elapsed = time.time() - t0
    print(f"Saved: {out_path}  ({elapsed:.1f}s)")
    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--res",  type=int, default=8,
                        help="Pixels per tile side (default 8 → 776×776 map)")
    parser.add_argument("--out",  type=str, default=None,
                        help="Output PNG path")
    args = parser.parse_args()

    out = Path(args.out) if args.out else None
    result = build_world_biome_map(res=args.res, out_path=out)
    print(f"Done: {result}")
