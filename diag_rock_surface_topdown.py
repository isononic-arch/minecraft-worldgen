"""
diag_rock_surface_topdown.py — Render surface block topdown for rock exposure tile.
Shows grass_block vs stone varieties after surface_decorator runs.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR   = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG_PATH = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
TILE = 512

BLOCK_COLORS = {
    "grass_block":     (100, 180, 60),
    "stone":           (128, 128, 128),
    "andesite":        (110, 110, 110),
    "granite":         (160, 110, 90),
    "diorite":         (200, 200, 200),
    "gravel":          (140, 135, 130),
    "coarse_dirt":     (120, 85, 55),
    "cobblestone":     (100, 100, 100),
    "dirt_path":       (170, 140, 80),
    "podzol":          (90, 65, 30),
    "dirt":            (130, 95, 60),
    "moss_block":      (80, 120, 40),
    "sand":            (220, 210, 160),
    "red_sand":        (190, 100, 40),
    "mud":             (60, 50, 40),
    "snow_block":      (240, 245, 250),
    "powder_snow":     (230, 235, 245),
    "sandstone":       (220, 200, 145),
    "terracotta":      (155, 95, 60),
    "orange_terracotta": (200, 110, 45),
    "red_terracotta":  (140, 55, 35),
    "brown_terracotta":(95, 60, 40),
    "smooth_sandstone":(220, 200, 155),
    "basalt":          (50, 50, 55),
    "smooth_basalt":   (40, 40, 45),
    "yellow_terracotta":(195, 145, 60),
    "white_terracotta":(200, 175, 150),
    "rooted_dirt":     (110, 80, 50),
    "clay":            (160, 165, 175),
    "packed_mud":      (140, 110, 80),
}
DEFAULT_COLOR = (200, 50, 200)  # magenta = unknown block


def main(tile_x, tile_z):
    t0 = time.perf_counter()
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    from core import tile_streamer, biome_assignment, column_generator
    from core import river_carver_v2, eco_gradients, surface_decorator, noise_fields

    col_off = tile_x * TILE
    row_off = tile_z * TILE

    noise = noise_fields.load_noise_generators(CONFIG_PATH)
    masks = tile_streamer.read_tile(MASKS_DIR, col_off, row_off, TILE, TILE)

    biome_grid = biome_assignment.assign_biomes(
        masks["height"], masks["slope"], masks["flow"],
        masks["erosion"], masks["override"], noise, cfg)

    height_uint16 = np.round(masks["height"] * 65535.0).astype(np.uint16)
    surface_y = column_generator.generate_columns(
        height_uint16, masks["slope"], biome_grid,
        masks["shore"], noise, cfg, tile_x, tile_z)

    pre_carve_y = surface_y.copy()
    surface_y, river_meta, conn_mask = river_carver_v2.carve_rivers(
        surface_y, masks["flow"], masks["river"], cfg,
        hydro_order=masks.get("hydro_order"),
        hydro_width=masks.get("hydro_width"),
        hydro_depth=masks.get("hydro_depth"),
        hydro_lake=masks.get("hydro_lake"),
        hydro_lkdep=masks.get("hydro_lkdep"),
        hydro_lake_wl=masks.get("hydro_lake_wl"),
        hydro_centerline=masks.get("hydro_centerline"),
        height_norm=masks["height"],
        masks_dir=MASKS_DIR, tile_x=tile_x, tile_z=tile_z)

    _gy, _gx = np.gradient(surface_y.astype(np.float32))
    cliff_deg = np.degrees(np.arctan(np.hypot(_gx, _gy))).astype(np.float32)
    land_mask = surface_y >= column_generator.SEA_LEVEL

    eco = eco_gradients.compute_eco_gradients(
        surface_y, masks["flow"], masks["erosion"], cliff_deg,
        masks.get("hydro_order", np.zeros_like(masks["height"])),
        masks.get("hydro_width", np.zeros_like(masks["height"])),
        masks.get("hydro_lake", np.zeros_like(masks["height"])),
        land_mask, cfg, river_meta, tile_x, tile_z, biome_grid,
        hydro_floodplain=masks.get("hydro_floodplain"),
        wind_windthrow=masks.get("wind_windthrow"),
        rock_exposure=masks.get("rock_exposure"),
        rock_exposure_tight=masks.get("rock_exposure_tight"),
        snow_caps=masks.get("snow_caps"),
        snow_caps_north=masks.get("snow_caps_north"),
        sand_dunes=masks.get("sand_dunes"),
        beach=masks.get("beach"))

    # Alpine biome inheritance (same as run_pipeline)
    if hasattr(eco, 'alpine_biome_source'):
        alpine_gap = (eco.gap_mask == 5) | (eco.gap_mask == 6) | (eco.gap_mask == 7)
        alpine_bio = biome_grid == "ALPINE_MEADOW"
        alpine_any = alpine_gap | alpine_bio
        if alpine_any.any():
            biome_grid[alpine_any] = eco.alpine_biome_source[alpine_any]

    surface_blk, sub_blk, ground_cover = surface_decorator.decorate_surface(
        surface_y=surface_y,
        biome_grid=biome_grid,
        erosion_tile=masks["erosion"],
        moisture_tile=masks["flow"],
        height_tile=masks["height"],
        river_meta=river_meta,
        flow_tile=masks["flow"],
        noise_fields=noise,
        cfg=cfg,
        tile_x=tile_x,
        tile_y=tile_z,
        eco_grads=eco,
        cliff_deg=cliff_deg)

    # Render topdown
    img = np.zeros((TILE, TILE, 3), dtype=np.uint8)
    img[:] = (30, 50, 80)  # ocean default

    for block, color in BLOCK_COLORS.items():
        m = surface_blk == block
        if m.any():
            img[m] = color

    # Count unknown
    known = np.zeros((TILE, TILE), dtype=bool)
    for block in BLOCK_COLORS:
        known |= (surface_blk == block)
    unknown = land_mask & ~known
    if unknown.any():
        img[unknown] = DEFAULT_COLOR
        # Print unknown blocks
        uniq = np.unique(surface_blk[unknown])
        print(f"Unknown surface blocks: {list(uniq)}")

    # Stats
    for block in ["grass_block", "stone", "andesite", "granite", "diorite",
                   "gravel", "coarse_dirt", "snow_block", "powder_snow",
                   "podzol", "moss_block", "sand", "red_sand", "sandstone",
                   "terracotta", "orange_terracotta", "red_terracotta",
                   "brown_terracotta", "smooth_sandstone",
                   "basalt", "smooth_basalt"]:
        c = int((surface_blk == block).sum())
        if c > 0:
            print(f"  {block}: {c} px ({100*c/TILE/TILE:.1f}%)")

    gap = eco.gap_mask
    for v, label in [(5, "bare_rock"), (6, "alpine_meadow"), (7, "snow_cap"), (8, "sand_dune")]:
        c = int((gap == v).sum())
        if c > 0:
            print(f"  gap={v} ({label}): {c} px ({100*c/TILE/TILE:.1f}%)")

    out_path = Path("output") / f"rock_surface_topdown_{tile_x}_{tile_z}.png"
    out_path.parent.mkdir(exist_ok=True)
    Image.fromarray(img).save(str(out_path))
    print(f"\nSaved: {out_path} ({time.perf_counter()-t0:.1f}s)")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--tile-x", type=int, default=36)
    p.add_argument("--tile-z", type=int, default=20)
    a = p.parse_args()
    main(a.tile_x, a.tile_z)
