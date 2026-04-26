"""
diag_biome_only_map.py — minimal biome-zone-only world map.

Renders override.tif as pure BIOME_COLORS at 6250x6250.  No hillshade,
no rock/snow/dune/beach/floodplain/river/lake overlays.  Tile grid
and compact legend included for zone identification.

Output: memory/world_map_biome_only.png  (and .jpg)
"""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.biome_assignment import OVERRIDE_BIOME_MAP
from tools.world_biome_map import BIOME_COLORS

MASKS = Path(r"C:/Users/nicho/minecraft-worldgen/masks")
OUT_PNG = REPO_ROOT / "memory" / "world_map_biome_only.png"
TARGET = 6250


def _hex(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def main() -> int:
    print(f"Reading override.tif at {TARGET}x{TARGET} via Resampling.mode "
          f"(majority vote per 8x8 source-block)...")
    with rasterio.open(MASKS / "override.tif") as src:
        override = src.read(1, out_shape=(TARGET, TARGET),
                             resampling=Resampling.mode)
    print(f"  override shape={override.shape} dtype={override.dtype}")

    # Build 256-entry RGB LUT zone -> color.
    default_rgb = np.array(BIOME_COLORS.get("default", (60, 60, 60)),
                            dtype=np.uint8)
    lut = np.tile(default_rgb, (256, 1)).astype(np.uint8)
    for code, name in OVERRIDE_BIOME_MAP.items():
        rgb = BIOME_COLORS.get(name)
        if rgb is None:
            continue
        lut[code] = rgb
    rgb = lut[override]
    print(f"  rendered biome RGB canvas")

    img = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(img)

    # Tile grid (97x97).  thin black at 15% alpha.
    grid_color = (0, 0, 0, 38)
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    tpx = TARGET / 97.0
    for i in range(98):
        x = int(i * tpx)
        odraw.line([(x, 0), (x, TARGET - 1)], fill=grid_color, width=1)
        odraw.line([(0, x), (TARGET - 1, x)], fill=grid_color, width=1)

    # Tile labels at every 4 tiles
    try:
        font = ImageFont.truetype(r"C:/Windows/Fonts/arial.ttf", 12)
    except OSError:
        font = ImageFont.load_default()
    for tx in range(0, 97, 4):
        for tz in range(0, 97, 4):
            x = int(tx * tpx) + 3
            y = int(tz * tpx) + 1
            odraw.text((x, y), f"{tx},{tz}", fill=(0, 0, 0, 90), font=font)

    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Legend — bottom-right, 420x900 box, biome family groups
    LEG_W, LEG_H = 440, 1000
    LEG_X = TARGET - LEG_W - 30
    LEG_Y = TARGET - LEG_H - 30
    bg = Image.new("RGBA", (LEG_W, LEG_H), (245, 245, 240, 220))
    bd = ImageDraw.Draw(bg)
    try:
        title_font = ImageFont.truetype(r"C:/Windows/Fonts/arialbd.ttf", 22)
        item_font = ImageFont.truetype(r"C:/Windows/Fonts/arial.ttf", 14)
    except OSError:
        title_font = ImageFont.load_default()
        item_font = ImageFont.load_default()

    bd.text((14, 14), "Vandir Biomes (override.tif)", fill=(20, 20, 20), font=title_font)
    bd.text((14, 42), "Mode-pool 50k -> 6250 (majority per 8x8 block)",
            fill=(80, 80, 80), font=item_font)

    # Group by ecological family.  Order chosen for legibility.
    GROUPS = [
        ("Arctic / Cold", ["FROZEN_FLATS", "ARCTIC_TUNDRA",
                            "SNOWY_BOREAL_TAIGA", "BOREAL_ALPINE"]),
        ("Boreal", ["BOREAL_TAIGA"]),
        ("Temperate Forest", ["TEMPERATE_DECIDUOUS", "MIXED_FOREST",
                                "BIRCH_FOREST", "TEMPERATE_RAINFOREST"]),
        ("Coast", ["COASTAL_HEATH", "EASTERN_TEMPERATE_COAST",
                    "RAINFOREST_COAST", "LUSH_RAINFOREST_COAST"]),
        ("Wet / Riparian", ["RIPARIAN_WOODLAND", "FRESHWATER_FEN"]),
        ("Mediterranean / Dry", ["DRY_OAK_SAVANNA", "DRY_PINE_BARRENS",
                                   "DRY_WOODLAND_MAQUIS",
                                   "SCRUBBY_HEATHLAND"]),
        ("Steppe / Arid", ["CONTINENTAL_STEPPE", "SEMI_ARID_SHRUBLAND",
                             "DESERT_STEPPE_TRANSITION", "KARST_BARRENS"]),
        ("Desert / Tropical", ["SAND_DUNE_DESERT", "TIDAL_JUNGLE_FRINGE",
                                 "MANGROVE_COAST"]),
    ]

    y = 76
    sw_size = 16
    for group_name, biomes in GROUPS:
        bd.text((14, y), group_name, fill=(20, 20, 20), font=title_font)
        y += 28
        for b in biomes:
            color = BIOME_COLORS.get(b, (60, 60, 60))
            bd.rectangle([18, y, 18 + sw_size, y + sw_size],
                          fill=color, outline=(60, 60, 60), width=1)
            bd.text((18 + sw_size + 8, y), b,
                     fill=(20, 20, 20), font=item_font)
            y += sw_size + 4
        y += 8

    img_a = img.convert("RGBA")
    img_a.alpha_composite(bg, (LEG_X, LEG_Y))
    img = img_a.convert("RGB")

    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT_PNG, optimize=False)
    jpg = OUT_PNG.with_suffix(".jpg")
    img.save(jpg, "JPEG", quality=88, optimize=True, progressive=True)
    phone = img.copy()
    phone.thumbnail((3000, 3000), Image.LANCZOS)
    phone_path = OUT_PNG.parent / (OUT_PNG.stem + "_phone.jpg")
    phone.save(phone_path, "JPEG", quality=85, optimize=True, progressive=True)
    print(f"Saved:\n  {OUT_PNG}\n  {jpg}\n  {phone_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
