"""
diag_rock_staircase.py — Diagnose rock/biome boundary staircasing

Reads raw masks for a single tile and renders a 6-panel top-down PNG
showing biome assignment, rock_exposure gradient, gap_mask, conflict zones,
elevation, and final surface blocks — WITHOUT running the full pipeline.

Usage:
    python diag_rock_staircase.py --tile-x 36 --tile-z 20
"""

from __future__ import annotations
import argparse
import sys
import numpy as np
from pathlib import Path

PYTHON = sys.executable
MASKS_DIR = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
TILE_SIZE = 512

# Zone code → biome name (from biome_assignment.py)
OVERRIDE_BIOME_MAP = {
    0: "", 10: "COASTAL_HEATH", 20: "TEMPERATE_RAINFOREST",
    30: "BOREAL_TAIGA", 35: "SNOWY_BOREAL_TAIGA", 40: "MIXED_FOREST",
    50: "ARCTIC_TUNDRA", 60: "BIRCH_FOREST", 70: "RAINFOREST_COAST",
    80: "CONTINENTAL_STEPPE", 90: "DRY_PINE_BARRENS",
    100: "DRY_OAK_SAVANNA", 110: "DRY_WOODLAND_MAQUIS",
    120: "TEMPERATE_DECIDUOUS", 130: "SAND_DUNE_DESERT",
    140: "ALPINE_MEADOW", 150: "KARST_BARRENS", 160: "MOSS_OLD_GROWTH",
    170: "SCRUBBY_HEATHLAND", 180: "SUBTROPICAL_HUMID",
    190: "DESERT_STEPPE_TRANSITION", 200: "SEMI_ARID_SHRUBLAND",
    210: "FROZEN_FLATS", 220: "TROPICAL_MONSOON_FOREST",
    230: "JUNGLE_HIGHLANDS", 235: "LUSH_RAINFOREST_COAST",
    240: "EASTERN_TEMPERATE_COAST",
}

# Biome → distinct RGB color
BIOME_COLORS = {
    "COASTAL_HEATH": (180, 200, 150),
    "TEMPERATE_RAINFOREST": (20, 100, 40),
    "BOREAL_TAIGA": (40, 80, 60),
    "SNOWY_BOREAL_TAIGA": (200, 220, 240),
    "MIXED_FOREST": (60, 130, 50),
    "ARCTIC_TUNDRA": (220, 230, 245),
    "BIRCH_FOREST": (140, 180, 80),
    "RAINFOREST_COAST": (10, 80, 30),
    "CONTINENTAL_STEPPE": (180, 170, 100),
    "DRY_PINE_BARRENS": (120, 130, 70),
    "DRY_OAK_SAVANNA": (190, 150, 80),
    "DRY_WOODLAND_MAQUIS": (160, 130, 60),
    "TEMPERATE_DECIDUOUS": (80, 150, 60),
    "SAND_DUNE_DESERT": (220, 200, 140),
    "ALPINE_MEADOW": (160, 190, 140),
    "KARST_BARRENS": (170, 160, 140),
    "MOSS_OLD_GROWTH": (30, 90, 50),
    "SCRUBBY_HEATHLAND": (150, 160, 100),
    "SUBTROPICAL_HUMID": (50, 120, 70),
    "DESERT_STEPPE_TRANSITION": (200, 180, 120),
    "SEMI_ARID_SHRUBLAND": (190, 170, 110),
    "FROZEN_FLATS": (230, 240, 250),
    "TROPICAL_MONSOON_FOREST": (30, 110, 50),
    "JUNGLE_HIGHLANDS": (40, 100, 60),
    "LUSH_RAINFOREST_COAST": (20, 90, 45),
    "EASTERN_TEMPERATE_COAST": (100, 160, 80),
}


def read_mask_tile(name: str, col_off: int, row_off: int, raw: bool = False):
    """Read a single 512x512 tile from a mask TIF."""
    import rasterio
    from rasterio.windows import Window

    path = MASKS_DIR / f"{name}.tif"
    if not path.exists():
        return np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.float32)

    with rasterio.open(str(path)) as src:
        w = min(TILE_SIZE, src.width - col_off)
        h = min(TILE_SIZE, src.height - row_off)
        if w <= 0 or h <= 0:
            return np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.float32)
        win = Window(col_off, row_off, w, h)
        data = src.read(1, window=win)

        if raw:
            out = np.zeros((TILE_SIZE, TILE_SIZE), dtype=data.dtype)
            out[:h, :w] = data
            return out

        if data.dtype == np.uint16:
            tile = data.astype(np.float32) / 65535.0
        elif data.dtype == np.uint8:
            tile = data.astype(np.float32) / 255.0
        elif data.dtype in (np.float32, np.float64):
            tile = data.astype(np.float32)
        else:
            tile = data.astype(np.float32) / float(np.iinfo(data.dtype).max)

        out = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.float32)
        out[:h, :w] = tile
        return out


def build_biome_grid(override_f: np.ndarray) -> np.ndarray:
    """Convert override float [0,1] → uint8 zone → biome name grid."""
    override_u8 = np.round(override_f * 255.0).astype(np.uint8)
    biome_grid = np.empty(override_f.shape, dtype=object)
    biome_grid[:] = ""
    for zone_val, bname in OVERRIDE_BIOME_MAP.items():
        if zone_val == 0:
            continue
        biome_grid[override_u8 == zone_val] = bname
    return biome_grid, override_u8


def compute_gap_mask_only(
    surface_y, flow_f, erosion_f, cliff_deg, land_mask,
    rock_exposure, wind_windthrow, hydro_floodplain,
    biome_grid, tile_x, tile_z,
    hydro_order, hydro_width, hydro_lake, river_meta,
):
    """Minimal gap_mask computation — just enough to see what the pipeline does."""
    import json
    cfg_path = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
    with open(cfg_path) as f:
        cfg = json.load(f)

    from core.eco_gradients import compute_eco_gradients
    eco = compute_eco_gradients(
        surface_y=surface_y,
        flow_f=flow_f,
        erosion_f=erosion_f,
        cliff_deg=cliff_deg,
        hydro_order=hydro_order,
        hydro_width=hydro_width,
        hydro_lake=hydro_lake,
        land_mask=land_mask,
        cfg=cfg,
        river_meta=river_meta,
        tile_x=tile_x,
        tile_z=tile_z,
        biome_grid=biome_grid,
        hydro_floodplain=hydro_floodplain,
        wind_windthrow=wind_windthrow,
        rock_exposure=rock_exposure,
    )
    return eco


def render_panels(tile_x, tile_z):
    """Generate 6-panel diagnostic PNG."""
    from PIL import Image, ImageDraw, ImageFont

    col_off = tile_x * TILE_SIZE
    row_off = tile_z * TILE_SIZE
    S = TILE_SIZE

    print(f"Reading masks for tile ({tile_x},{tile_z})...")

    # Read all needed masks
    height_f = read_mask_tile("height", col_off, row_off)
    override_f = read_mask_tile("override", col_off, row_off)
    rock_exp_f = read_mask_tile("rock_exposure", col_off, row_off)
    slope_f = read_mask_tile("slope", col_off, row_off)
    flow_f = read_mask_tile("flow", col_off, row_off)
    erosion_f = read_mask_tile("erosion", col_off, row_off)
    wind_wt_f = read_mask_tile("wind_windthrow", col_off, row_off)
    flood_f = read_mask_tile("hydro_floodplain", col_off, row_off)
    hydro_order_f = read_mask_tile("hydro_order", col_off, row_off)
    hydro_width_f = read_mask_tile("hydro_width", col_off, row_off)
    hydro_lake_f = read_mask_tile("hydro_lake", col_off, row_off)

    # Compute derived arrays
    # Height → MC Y via spline (simplified linear)
    h_u16 = np.round(height_f * 65535.0).astype(np.uint16)
    # Spline: gaea_in=[0,17050,45000,65496] → mc_y_out=[-64,63,200,448]
    surface_y = np.interp(
        h_u16.astype(np.float32),
        [0, 17050, 45000, 65496],
        [-64, 63, 200, 448]
    ).astype(np.int16)

    # Slope → degrees
    cliff_deg = slope_f * 90.0  # approximate

    # Land mask
    sea_norm = 17050.0 / 65535.0
    land_mask = height_f > sea_norm

    # Biome grid
    biome_grid, override_u8 = build_biome_grid(override_f)

    # River meta (none for this diagnostic)
    river_meta = np.zeros((S, S), dtype=np.uint8)

    print("Computing eco_gradients (gap_mask, rock exposure zones)...")
    eco = compute_gap_mask_only(
        surface_y, flow_f, erosion_f, cliff_deg, land_mask,
        rock_exp_f, wind_wt_f, flood_f,
        biome_grid, tile_x, tile_z,
        hydro_order_f, hydro_width_f, hydro_lake_f, river_meta,
    )

    gap_mask = eco.gap_mask
    re_grad = eco.rock_exposure_gradient

    # --- Stats ---
    biomes_present = {}
    for b in np.unique(biome_grid):
        b = str(b)
        if b:
            count = int((biome_grid == b).sum())
            biomes_present[b] = count
    print(f"Biomes: {biomes_present}")
    for v, label in [(1, "meadow"), (2, "windthrow"), (4, "floodplain"),
                     (5, "bare_rock"), (6, "alpine_meadow")]:
        c = int((gap_mask == v).sum())
        if c > 0:
            print(f"  gap={v} ({label}): {c} px ({100*c/S/S:.1f}%)")

    # Conflict: SNOWY_BOREAL_TAIGA on rock/alpine pixels
    is_taiga = biome_grid == "SNOWY_BOREAL_TAIGA"
    is_rock_zone = (gap_mask == 5) | (gap_mask == 6)
    conflict = is_taiga & is_rock_zone
    print(f"  CONFLICT (taiga on rock/alpine): {int(conflict.sum())} px")

    # Also check: non-taiga on rock zone (should be fine)
    non_taiga_rock = ~is_taiga & is_rock_zone & (biome_grid != "")
    print(f"  Non-taiga rock/alpine: {int(non_taiga_rock.sum())} px")

    # --- Render 6 panels ---
    PAD = 4
    LABEL_H = 20
    panel_w = S
    panel_h = S + LABEL_H
    cols = 3
    rows = 2
    img_w = cols * panel_w + (cols - 1) * PAD
    img_h = rows * panel_h + (rows - 1) * PAD
    canvas = Image.new("RGB", (img_w, img_h), (30, 30, 30))
    draw = ImageDraw.Draw(canvas)

    def paste_panel(arr_rgb, row, col, label):
        """Paste a (S,S,3) uint8 array into the canvas grid."""
        x = col * (panel_w + PAD)
        y = row * (panel_h + PAD)
        img = Image.fromarray(arr_rgb.astype(np.uint8))
        canvas.paste(img, (x, y + LABEL_H))
        draw.text((x + 4, y + 2), label, fill=(255, 255, 200))

    # Panel 1: Override biome (color-coded)
    p1 = np.zeros((S, S, 3), dtype=np.uint8)
    p1[:] = (40, 40, 40)  # background
    for bname, color in BIOME_COLORS.items():
        m = biome_grid == bname
        if m.any():
            p1[m] = color
    paste_panel(p1, 0, 0, "1: Override Biome")

    # Panel 2: Rock exposure gradient (heat map)
    p2 = np.zeros((S, S, 3), dtype=np.uint8)
    # Black→blue→cyan→yellow→red
    re_u8 = np.clip(re_grad * 255, 0, 255).astype(np.uint8)
    # Simple: low=dark green, mid=yellow, high=red
    p2[:, :, 0] = np.clip(re_u8 * 2, 0, 255).astype(np.uint8)  # R
    p2[:, :, 1] = np.clip(255 - re_u8, 0, 255).astype(np.uint8)  # G
    p2[:, :, 2] = np.clip(80 - re_u8 // 3, 0, 80).astype(np.uint8)  # B
    p2[~land_mask] = (20, 20, 40)
    paste_panel(p2, 0, 1, "2: Rock Exposure Gradient")

    # Panel 3: Gap mask (color-coded)
    GAP_COLORS = {
        0: (40, 40, 40),      # none
        1: (80, 200, 80),     # meadow - green
        2: (200, 150, 50),    # windthrow - amber
        4: (50, 100, 200),    # floodplain - blue
        5: (220, 60, 60),     # bare rock - red
        6: (100, 220, 100),   # alpine meadow - bright green
    }
    p3 = np.zeros((S, S, 3), dtype=np.uint8)
    for val, color in GAP_COLORS.items():
        m = gap_mask == val
        if m.any():
            p3[m] = color
    paste_panel(p3, 0, 2, "3: Gap Mask (red=rock, grn=alpine)")

    # Panel 4: Conflict overlay — taiga biome on rock/alpine zone
    p4 = p1.copy()  # start with biome colors
    # Highlight conflict in bright magenta
    p4[conflict] = (255, 0, 255)
    # Outline rock zone boundary in white
    from scipy.ndimage import binary_dilation
    rock_boundary = binary_dilation(is_rock_zone, iterations=1) & ~is_rock_zone
    p4[rock_boundary] = (255, 255, 255)
    paste_panel(p4, 1, 0, "4: CONFLICT (magenta=taiga+rock)")

    # Panel 5: Surface elevation (greyscale) with gap_mask==5 boundary overlay
    sy_f = (surface_y.astype(np.float32) - surface_y.min()) / max(
        float(surface_y.max() - surface_y.min()), 1.0)
    p5 = np.stack([
        (sy_f * 220).astype(np.uint8),
        (sy_f * 220).astype(np.uint8),
        (sy_f * 220).astype(np.uint8),
    ], axis=-1)
    # Overlay rock boundary in red, alpine boundary in green
    rock_edge = binary_dilation(gap_mask == 5, iterations=1) & ~(gap_mask == 5)
    alp_edge = binary_dilation(gap_mask == 6, iterations=1) & ~(gap_mask == 6)
    p5[rock_edge] = (255, 60, 60)
    p5[alp_edge] = (60, 255, 60)
    paste_panel(p5, 1, 1, "5: Elevation + zone edges")

    # Panel 6: Biome boundary staircase vs elevation contours
    # Show the override_u8 raw values to see the 8px staircase
    p6 = np.zeros((S, S, 3), dtype=np.uint8)
    # Use surface_y elevation as greyscale base
    p6[:, :, :] = p5[:, :, :]
    # Overlay biome boundary (where override changes) in cyan
    # Detect biome boundaries by checking if neighboring pixels differ
    shifted_r = np.roll(override_u8, 1, axis=0)
    shifted_c = np.roll(override_u8, 1, axis=1)
    biome_boundary = (override_u8 != shifted_r) | (override_u8 != shifted_c)
    p6[biome_boundary] = (0, 255, 255)  # cyan = biome boundary
    # Also overlay rock_exposure threshold contours (0.3 and 0.7)
    re_03 = (re_grad >= 0.28) & (re_grad < 0.32)
    re_07 = (re_grad >= 0.68) & (re_grad < 0.72)
    p6[re_03] = (255, 255, 0)  # yellow = alpine threshold
    p6[re_08] = (255, 100, 0)  # orange = rock threshold
    paste_panel(p6, 1, 2, "6: Biome bnd(cyan) vs treeline(yel/org)")

    out_path = Path("output") / f"diag_rock_staircase_{tile_x}_{tile_z}.png"
    out_path.parent.mkdir(exist_ok=True)
    canvas.save(str(out_path))
    print(f"\nSaved: {out_path}")

    # --- Zoomed panel: 128x128 crop of the conflict zone boundary ---
    # Find the center of the conflict zone for a tight crop
    if conflict.any():
        cy, cx = np.where(conflict)
        center_y, center_x = int(np.median(cy)), int(np.median(cx))
        CROP = 128
        y0 = max(0, center_y - CROP // 2)
        x0 = max(0, center_x - CROP // 2)
        y1 = min(S, y0 + CROP)
        x1 = min(S, x0 + CROP)

        zoom_w = (x1 - x0) * 4
        zoom_h = (y1 - y0) * 4
        zoom_canvas = Image.new("RGB", (zoom_w * 3 + PAD * 2, zoom_h + LABEL_H), (30, 30, 30))
        zdraw = ImageDraw.Draw(zoom_canvas)

        def paste_zoom(arr_full, col, label):
            crop = arr_full[y0:y1, x0:x1]
            img = Image.fromarray(crop.astype(np.uint8))
            img = img.resize((zoom_w, zoom_h), Image.NEAREST)
            zoom_canvas.paste(img, (col * (zoom_w + PAD), LABEL_H))
            zdraw.text((col * (zoom_w + PAD) + 4, 2), label, fill=(255, 255, 200))

        # Zoom A: biome + rock boundary
        zA = p1[..., :].copy()
        zA[gap_mask == 5] = [220, 60, 60]   # rock = red
        zA[gap_mask == 6] = [100, 220, 100]  # alpine = green
        paste_zoom(zA, 0, f"Biome+GapMask crop ({x0},{y0})")

        # Zoom B: biome boundary (cyan) vs elevation
        paste_zoom(p6, 1, "Biome bnd(cyan) vs treeline")

        # Zoom C: conflict only
        zC = np.zeros((S, S, 3), dtype=np.uint8)
        zC[:] = (40, 40, 40)
        # Show surface_y as base
        zC[:, :, :] = p5[:, :, :]
        # Taiga zone in semi-transparent blue tint
        zC[is_taiga, 2] = np.clip(zC[is_taiga, 2].astype(int) + 80, 0, 255).astype(np.uint8)
        # Rock pixels in red
        zC[gap_mask == 5] = [220, 60, 60]
        # Alpine in green
        zC[gap_mask == 6] = [100, 220, 100]
        # Conflict in magenta
        zC[conflict] = [255, 0, 255]
        paste_zoom(zC, 2, "Conflict (magenta) on elevation")

        zoom_path = Path("output") / f"diag_rock_zoom_{tile_x}_{tile_z}.png"
        zoom_canvas.save(str(zoom_path))
        print(f"Saved zoom: {zoom_path}")

    return out_path


def main():
    parser = argparse.ArgumentParser(description="Rock staircase diagnostic")
    parser.add_argument("--tile-x", type=int, default=36)
    parser.add_argument("--tile-z", type=int, default=20)
    args = parser.parse_args()
    render_panels(args.tile_x, args.tile_z)


if __name__ == "__main__":
    main()
