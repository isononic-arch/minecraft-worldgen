"""
diag_floodplain_topdown.py — Render 3×3 topdown of gap_mask (clearings)
overlaid on hillshaded terrain.  Shows all 4 gap types in distinct colours:

    1 = meadow (green)    2 = windthrow (orange)
    3 = bare (brown)      4 = floodplain (yellow-green)

Water shown in blue.  Run after any eco_gradients change to visually
validate floodplain corridor shape, width, and continuity.

Usage:
    python diag_floodplain_topdown.py [--cx 51] [--cz 53]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR   = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG_PATH = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
OUTPUT_DIR  = Path(r"C:\Users\nicho\minecraft-worldgen\output")

TILE = 512
GRID_X = 3
GRID_Z = 3

# Gap type colours (RGBA premultiplied onto terrain)
GAP_COLOURS = {
    1: np.array([90, 200, 80]),    # meadow — green
    2: np.array([220, 160, 50]),   # windthrow — orange
    3: np.array([160, 120, 70]),   # bare — brown
    4: np.array([180, 220, 60]),   # floodplain — yellow-green
}
WATER_RGB = np.array([70, 130, 210])
GAP_ALPHA = 0.55  # blend strength for gap overlay


def hillshade(height_norm: np.ndarray) -> np.ndarray:
    """Simple hillshade from normalised height."""
    gy, gx = np.gradient(height_norm)
    azimuth = np.radians(315)
    altitude = np.radians(45)
    shade = (np.cos(altitude) * np.cos(np.arctan(np.hypot(gx, gy))) +
             np.sin(altitude) * (gx * np.sin(azimuth) + gy * np.cos(azimuth)) /
             np.maximum(np.hypot(gx, gy), 1e-6))
    shade = np.clip((shade + 1) / 2, 0, 1)
    grey = (shade * 200 + 40).astype(np.uint8)
    return np.stack([grey, grey, grey], axis=-1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cx", type=int, default=51, help="Centre tile X")
    parser.add_argument("--cz", type=int, default=53, help="Centre tile Z")
    args = parser.parse_args()

    t0 = time.perf_counter()

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    from core.tile_streamer import read_tile
    from core.river_carver_v2 import carve_rivers, _height_norm_to_mc_y
    from core.eco_gradients import compute_eco_gradients
    from core.biome_assignment import assign_biomes

    tx0 = args.cx - GRID_X // 2
    tz0 = args.cz - GRID_Z // 2

    region_h = GRID_Z * TILE
    region_w = GRID_X * TILE
    composite = np.zeros((region_h, region_w, 3), dtype=np.uint8)

    gap_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    total_land = 0

    for gi in range(GRID_X):
        for gj in range(GRID_Z):
            tx = tx0 + gi
            tz = tz0 + gj
            print(f"Processing tile ({tx},{tz}) ...")

            col_off = tx * TILE
            row_off = tz * TILE
            masks = read_tile(MASKS_DIR, col_off, row_off, TILE, TILE)

            # Surface Y
            surface_y = _height_norm_to_mc_y(masks["height"], cfg).astype(np.int16)

            # River carving
            carve_result = carve_rivers(
                surface_y=surface_y,
                flow_tile=masks["flow"],
                river_tile=masks["river"],
                cfg=cfg,
                hydro_order=masks.get("hydro_order"),
                hydro_width=masks.get("hydro_width"),
                hydro_depth=masks.get("hydro_depth"),
                hydro_lake=masks.get("hydro_lake"),
                hydro_lkdep=masks.get("hydro_lkdep"),
                hydro_lake_wl=masks.get("hydro_lake_wl"),
                hydro_centerline=masks.get("hydro_centerline"),
                height_norm=masks["height"],
                masks_dir=MASKS_DIR,
                tile_x=tx, tile_z=tz,
            )
            if len(carve_result) == 3:
                surface_y_carved, river_meta, _ = carve_result
            else:
                surface_y_carved, river_meta = carve_result

            # Biome assignment
            land_mask = surface_y_carved >= 63
            biome_grid = assign_biomes(
                height_tile=masks["height"],
                slope_tile=masks.get("slope", np.zeros((TILE, TILE), dtype=np.float32)),
                flow_tile=masks["flow"],
                erosion_tile=masks["erosion"],
                override_tile=masks.get("override"),
                noise_fields=None,
                cfg=cfg,
                tile_x=tx,
                tile_y=tz,
            )

            # Cliff degrees
            _gy, _gx = np.gradient(surface_y_carved.astype(np.float32))
            cliff_deg = np.degrees(np.arctan(np.hypot(_gx, _gy))).astype(np.float32)

            # Eco gradients (includes gap_mask with floodplain)
            eco = compute_eco_gradients(
                surface_y=surface_y_carved,
                flow_f=masks["flow"],
                erosion_f=masks["erosion"],
                cliff_deg=cliff_deg,
                hydro_order=masks.get("hydro_order", np.zeros((TILE, TILE), dtype=np.float32)),
                hydro_width=masks.get("hydro_width", np.zeros((TILE, TILE), dtype=np.float32)),
                hydro_lake=masks.get("hydro_lake", np.zeros((TILE, TILE), dtype=np.float32)),
                land_mask=land_mask,
                cfg=cfg,
                river_meta=river_meta,
                tile_x=tx, tile_z=tz,
                biome_grid=biome_grid,
                hydro_floodplain=masks.get("hydro_floodplain"),
                wind_windthrow=masks.get("wind_windthrow"),
                rock_exposure=masks.get("rock_exposure"),
            )

            # Render: hillshade base + gap overlay + water
            tile_rgb = hillshade(masks["height"])

            gap = eco.gap_mask
            for gval, colour in GAP_COLOURS.items():
                px = gap == gval
                if px.any():
                    tile_rgb[px] = (tile_rgb[px] * (1 - GAP_ALPHA) +
                                    colour * GAP_ALPHA).astype(np.uint8)
                    gap_counts[gval] += px.sum()

            water = river_meta > 0
            tile_rgb[water] = WATER_RGB

            total_land += land_mask.sum()

            r0 = gj * TILE
            c0 = gi * TILE
            composite[r0:r0+TILE, c0:c0+TILE] = tile_rgb

            # Per-tile stats
            n_land = land_mask.sum()
            for gval, name in [(1, "meadow"), (2, "windthrow"),
                               (3, "bare"), (4, "floodplain")]:
                n = (gap == gval).sum()
                pct = n * 100 / max(n_land, 1)
                print(f"  {name:12s}: {n:6d} px ({pct:.1f}%)")

    # Draw grid + labels
    img = Image.fromarray(composite, "RGB")
    draw = ImageDraw.Draw(img)
    for i in range(GRID_X + 1):
        x = i * TILE
        if x < region_w:
            draw.line([(x, 0), (x, region_h - 1)], fill=(80, 80, 80), width=1)
    for j in range(GRID_Z + 1):
        y = j * TILE
        if y < region_h:
            draw.line([(0, y), (region_w - 1, y)], fill=(80, 80, 80), width=1)
    for gi in range(GRID_X):
        for gj in range(GRID_Z):
            tx = tx0 + gi
            tz = tz0 + gj
            draw.text((gi * TILE + 4, gj * TILE + 4),
                      f"({tx},{tz})", fill=(255, 255, 255))

    # Legend
    y_leg = region_h - 80
    legend = [
        ("Meadow", (90, 200, 80)),
        ("Windthrow", (220, 160, 50)),
        ("Bare", (160, 120, 70)),
        ("Floodplain", (180, 220, 60)),
        ("Water", (70, 130, 210)),
    ]
    for i, (label, colour) in enumerate(legend):
        x_leg = 10
        y_pos = y_leg + i * 14
        draw.rectangle([(x_leg, y_pos), (x_leg + 10, y_pos + 10)], fill=colour)
        draw.text((x_leg + 14, y_pos - 1), label, fill=(255, 255, 255))

    out_path = OUTPUT_DIR / "floodplain_topdown.png"
    OUTPUT_DIR.mkdir(exist_ok=True)
    img.save(str(out_path))

    elapsed = time.perf_counter() - t0
    print(f"\n--- Summary ({GRID_X}x{GRID_Z} tiles, {elapsed:.1f}s) ---")
    for gval, name in [(1, "meadow"), (2, "windthrow"),
                       (3, "bare"), (4, "floodplain")]:
        n = gap_counts[gval]
        pct = n * 100 / max(total_land, 1)
        print(f"  {name:12s}: {n:8d} px ({pct:.2f}% of land)")
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
