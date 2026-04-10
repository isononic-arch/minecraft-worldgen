"""
diag_desert_rivers_preview.py — Preview of desert river treatment.

Shows the SW desert continent with:
- BLUE: Preserved through-flowing rivers (order >= 4) + lake feeders + the main desert lake
- TAN/BROWN: Dry wadis (remaining rivers in sand dune desert, rendered as sand channels)
- Normal biome colors + hillshade everywhere else
- Ocean rivers removed

Two panels: BEFORE (current) and AFTER (proposed treatment)
"""
from __future__ import annotations
import sys, time
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import (binary_dilation, maximum_filter as maxfilt,
                           label, gaussian_filter)

MASKS_DIR = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\desert_rivers_preview.png")

TILE = 512
DS = 8  # higher res for desert detail (each tile = 64px)

# SW continent region — tiles ~8-30 x 62-90
TX_MIN, TX_MAX = 6, 32
TZ_MIN, TZ_MAX = 60, 92

BIOME_COLORS = {
    0:   ( 30,  80, 160),  # _OCEAN
    10:  (180, 200, 140),  # COASTAL_HEATH
    20:  ( 30, 120,  60),  # TEMPERATE_RAINFOREST
    30:  ( 60, 130,  90),  # BOREAL_TAIGA
    35:  (180, 200, 220),  # SNOWY_BOREAL_TAIGA
    40:  (140, 180, 100),  # ALPINE_MEADOW
    50:  (220, 230, 240),  # ARCTIC_TUNDRA
    55:  (240, 245, 255),  # FROZEN_FLATS
    60:  ( 80, 160,  80),  # TEMPERATE_DECIDUOUS
    70:  ( 20, 160,  80),  # RAINFOREST_COAST
    80:  ( 60, 140, 100),  # RIPARIAN_WOODLAND
    90:  (190, 160,  80),  # DRY_OAK_SAVANNA
    100: (180, 170, 150),  # KARST_BARRENS
    110: (160, 200, 140),  # BIRCH_FOREST
    115: (120, 180, 130),  # EASTERN_TEMPERATE_COAST
    120: ( 60, 140,  70),  # MIXED_FOREST
    130: (200, 180, 100),  # CONTINENTAL_STEPPE
    140: (140, 160, 100),  # DRY_PINE_BARRENS
    150: (180, 160, 120),  # SCRUBBY_HEATHLAND
    160: ( 20, 140,  80),  # LUSH_RAINFOREST_COAST
    170: (230, 200, 120),  # SAND_DUNE_DESERT
    190: (210, 185, 120),  # DESERT_STEPPE_TRANSITION
    200: (200, 170, 110),  # SEMI_ARID_SHRUBLAND
    210: (170, 160, 100),  # DRY_WOODLAND_MAQUIS
    220: ( 40, 150, 100),  # TIDAL_JUNGLE_FRINGE
    230: ( 50, 140,  90),  # MANGROVE_COAST
    240: ( 80, 150, 130),  # FRESHWATER_FEN
}


def _build_color_lut():
    lut = np.full((256, 3), (128, 128, 128), dtype=np.uint8)
    for code, rgb in BIOME_COLORS.items():
        lut[code] = rgb
    return lut


def build_base(override, height, shade):
    """Build biome + hillshade base image."""
    color_lut = _build_color_lut()
    comp = color_lut[override]
    comp = np.clip(comp.astype(np.float32) * shade[..., None] * 1.6, 0, 255).astype(np.uint8)
    sea_norm = 17050.0 / 65535.0
    is_sea = height < sea_norm
    comp[is_sea] = (30, 80, 160)
    return comp, is_sea


def add_lakes(comp, lake, lake_wl, height, is_sea, ds):
    """Add terrain-intersection lakes."""
    lake_basin = lake > 0
    basin_expand = max(1, 32 // ds)
    lake_basin_exp = binary_dilation(lake_basin, iterations=basin_expand)
    lake_wl_exp = maxfilt(lake_wl, size=basin_expand * 2 + 1)
    lake_mask = lake_basin_exp & (lake_wl_exp > 0) & (height < lake_wl_exp) & ~is_sea
    comp[lake_mask] = (50, 110, 190)
    return lake_mask


def main():
    t0 = time.perf_counter()

    col0 = TX_MIN * TILE
    row0 = TZ_MIN * TILE
    w = (TX_MAX - TX_MIN) * TILE
    h = (TZ_MAX - TZ_MIN) * TILE
    win = Window(col0, row0, w, h)
    out_h, out_w = h // DS, w // DS

    def read_mask(name, resamp=Resampling.average):
        with rasterio.open(str(MASKS_DIR / f"{name}.tif")) as src:
            return src.read(1, window=win, out_shape=(out_h, out_w),
                           resampling=resamp)

    print("Reading masks...", flush=True)
    override = read_mask("override", Resampling.nearest).astype(np.uint8)
    height_raw = read_mask("height")
    height = height_raw.astype(np.float32) / (65535.0 if height_raw.dtype == np.uint16 else 1.0)
    order = read_mask("hydro_order", Resampling.nearest).astype(np.uint8)
    cl = read_mask("hydro_centerline", Resampling.nearest).astype(np.uint8)
    lake = read_mask("hydro_lake", Resampling.nearest)
    lake_wl_raw = read_mask("hydro_lake_wl", Resampling.nearest)
    lake_wl = lake_wl_raw.astype(np.float32)
    if lake_wl_raw.dtype == np.uint16:
        lake_wl = lake_wl / 65535.0
    river_raw = read_mask("river", Resampling.average)
    river = river_raw.astype(np.float32) / (65535.0 if river_raw.dtype == np.uint16 else 1.0)

    sea_norm = 17050.0 / 65535.0
    is_sea = height < sea_norm

    # Hillshade
    h_smooth = gaussian_filter(height, sigma=1.5)
    gy, gx = np.gradient(h_smooth)
    shade = (-gx + gy) / (np.sqrt(gx**2 + gy**2 + 0.001) + 0.001)
    shade = np.clip(shade * 0.3 + 0.5, 0.25, 0.75)

    print("Building river analysis...", flush=True)

    desert = (override == 170)
    any_river = (cl > 0) | (order > 0)
    any_river_or_raw = any_river | (river > 0.15)

    # ── Identify features to PRESERVE (keep as water) ──────────────

    # 1. Main desert lake (largest lake component in desert)
    lake_in_desert = desert & (lake > 0)
    lake_labels, n_lakes = label(lake_in_desert)
    lake_sizes = np.bincount(lake_labels.ravel())
    lake_sizes[0] = 0  # background
    main_lake_id = lake_sizes.argmax() if n_lakes > 0 else 0
    main_lake_seed = (lake_labels == main_lake_id) if main_lake_id > 0 else np.zeros_like(desert)

    # Also include secondary lakes near the main one (the cluster around tile 20,75)
    all_desert_lakes = lake_in_desert  # preserve ALL lakes in desert

    # 2. Rivers feeding the main lake — dilate lake, find touching river components
    lake_dilated = binary_dilation(all_desert_lakes, iterations=5)
    river_mask_land = any_river & ~is_sea
    river_labels, n_rivers = label(river_mask_land)

    # Components touching desert lakes
    touching_labels = set(np.unique(river_labels[lake_dilated & (river_labels > 0)]))

    # 3. High-order through-flowing rivers (order >= 4) in desert
    # Find components that have order >= 4 pixels within the desert
    high_order_desert = desert & (order >= 4) & (river_labels > 0)
    high_order_labels = set(np.unique(river_labels[high_order_desert]))

    # Combine: preserve lake feeders + high-order rivers
    preserve_labels = touching_labels | high_order_labels
    print(f"  Preserving {len(preserve_labels)} river components "
          f"({len(touching_labels)} lake feeders, {len(high_order_labels)} high-order)")

    # Build preserve mask — full component preserved (not just desert portion)
    preserve_mask = np.zeros_like(desert)
    for lbl in preserve_labels:
        preserve_mask |= (river_labels == lbl)

    # ── Classify river pixels in desert ──────────────────────────────
    # Dry wadi = river in desert that is NOT preserved
    desert_river_all = desert & any_river_or_raw
    dry_wadi = desert_river_all & ~preserve_mask & ~all_desert_lakes
    preserved_river = desert & any_river & preserve_mask

    wadi_px = dry_wadi.sum()
    preserved_px = preserved_river.sum()
    print(f"  Dry wadi pixels: {wadi_px}")
    print(f"  Preserved water pixels in desert: {preserved_px}")
    print(f"  Desert lake pixels: {all_desert_lakes.sum()}")

    # ══════════════════════════════════════════════════════════════════
    # RENDER: BEFORE (left) and AFTER (right)
    # ══════════════════════════════════════════════════════════════════

    print("Rendering BEFORE panel...", flush=True)
    # ── BEFORE: current state (all rivers as water) ──────────────────
    before, _ = build_base(override, height, shade)
    before[is_sea] = (30, 80, 160)
    lake_mask = add_lakes(before, lake, lake_wl, height, is_sea, DS)

    # Paint ALL rivers as water (current behavior)
    braid = (cl == 255) & ~lake_mask & ~is_sea
    thin = (cl > 0) & (cl < 255) & ~lake_mask & ~is_sea
    order_only = (order > 0) & ~thin & ~braid & ~lake_mask & ~is_sea
    river_only = (river > 0.15) & (order == 0) & (cl == 0) & ~lake_mask & ~is_sea
    # Including ocean rivers (current bug)
    before[braid] = (55, 115, 195)
    before[thin] = (65, 130, 215)
    before[order_only] = (80, 140, 200)
    before[river_only] = (100, 150, 195)

    print("Rendering AFTER panel...", flush=True)
    # ── AFTER: proposed treatment ────────────────────────────────────
    after, _ = build_base(override, height, shade)
    after[is_sea] = (30, 80, 160)
    lake_mask_after = add_lakes(after, lake, lake_wl, height, is_sea, DS)

    # 1. Remove ocean rivers entirely
    land = ~is_sea

    # 2. Paint dry wadis in sand dune desert (sandstone/sand mix)
    #    Use noise for visual variation
    np.random.seed(42)
    wadi_noise = np.random.random(dry_wadi.shape)
    # Sandstone base (194, 178, 128) with sand highlights (219, 207, 163)
    wadi_r = np.where(wadi_noise > 0.4, 194, 219).astype(np.uint8)
    wadi_g = np.where(wadi_noise > 0.4, 178, 207).astype(np.uint8)
    wadi_b = np.where(wadi_noise > 0.4, 128, 163).astype(np.uint8)
    # Darken slightly in carved channels (using shade)
    wadi_shade = np.clip(shade * 1.1, 0.6, 1.0)
    after[dry_wadi, 0] = np.clip(wadi_r[dry_wadi] * wadi_shade[dry_wadi], 0, 255).astype(np.uint8)
    after[dry_wadi, 1] = np.clip(wadi_g[dry_wadi] * wadi_shade[dry_wadi], 0, 255).astype(np.uint8)
    after[dry_wadi, 2] = np.clip(wadi_b[dry_wadi] * wadi_shade[dry_wadi], 0, 255).astype(np.uint8)

    # 3. Paint preserved water features (lake feeders + high-order rivers)
    #    Only on LAND pixels
    preserved_braid = braid & land & (~desert | preserve_mask)
    preserved_thin = thin & land & (~desert | preserve_mask)
    preserved_order = order_only & land & (~desert | preserve_mask)
    preserved_raw = river_only & land & (~desert | preserve_mask)

    after[preserved_braid & ~lake_mask_after] = (55, 115, 195)
    after[preserved_thin & ~lake_mask_after] = (65, 130, 215)
    after[preserved_order & ~lake_mask_after] = (80, 140, 200)
    after[preserved_raw & ~lake_mask_after] = (100, 150, 195)

    # ── Compose side-by-side ─────────────────────────────────────────
    print("Composing panels...", flush=True)
    gap = 20
    canvas_w = out_w * 2 + gap
    canvas_h = out_h + 60  # room for labels
    canvas = np.full((canvas_h, canvas_w, 3), 20, dtype=np.uint8)

    # Place panels
    y_off = 40
    canvas[y_off:y_off + out_h, :out_w] = before
    canvas[y_off:y_off + out_h, out_w + gap:] = after

    img = Image.fromarray(canvas, "RGB")
    draw = ImageDraw.Draw(img)

    # Headers
    draw.text((out_w // 2 - 40, 8), "BEFORE", fill=(255, 255, 255))
    draw.text((out_w + gap + out_w // 2 - 40, 8), "AFTER", fill=(255, 255, 255))
    draw.text((out_w // 2 - 120, 22), "(all rivers as water, ocean rivers present)",
              fill=(180, 180, 180))
    draw.text((out_w + gap + out_w // 2 - 140, 22),
              "(ocean cut, dry wadis in desert, lake+feeders preserved)",
              fill=(180, 180, 180))

    # Tile grid on both panels
    tile_px = TILE // DS
    for panel_x_off in [0, out_w + gap]:
        for i in range(TX_MAX - TX_MIN + 1):
            x = panel_x_off + i * tile_px
            draw.line([(x, y_off), (x, y_off + out_h - 1)], fill=(40, 40, 40), width=1)
        for j in range(TZ_MAX - TZ_MIN + 1):
            y = y_off + j * tile_px
            draw.line([(panel_x_off, y), (panel_x_off + out_w - 1, y)],
                      fill=(40, 40, 40), width=1)

    # Legend at bottom
    legend_y = y_off + out_h + 5
    x = 10
    for lbl, color in [("Water river", (65, 130, 215)), ("Braid/lake", (50, 110, 190)),
                        ("Dry wadi", (194, 178, 128)), ("Desert", (230, 200, 120)),
                        ("Ocean", (30, 80, 160))]:
        draw.rectangle([(x, legend_y), (x + 12, legend_y + 12)], fill=color)
        draw.text((x + 16, legend_y), lbl, fill=(200, 200, 200))
        x += 120

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"\nSaved: {OUTPUT}  ({canvas_w}x{canvas_h}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
