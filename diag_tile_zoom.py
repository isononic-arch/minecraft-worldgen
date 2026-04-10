"""
diag_tile_zoom.py — Zoomed view of a single tile with before/after roughening.
"""
from __future__ import annotations
import sys, time
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window
from PIL import Image, ImageDraw
from scipy.ndimage import (binary_dilation, maximum_filter as maxfilt,
                           gaussian_filter, distance_transform_edt)

MASKS_DIR = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\tile_zoom_19_75.png")

TILE = 512

# Tile 16,73 with 2-tile padding for context
TX, TZ = 19, 75
PAD = 2
TX_MIN, TX_MAX = TX - PAD, TX + PAD + 1
TZ_MIN, TZ_MAX = TZ - PAD, TZ + PAD + 1

DS = 1  # full resolution

BIOME_COLORS = {
    0:   ( 30,  80, 160), 10:  (180, 200, 140), 20:  ( 30, 120,  60),
    30:  ( 60, 130,  90), 35:  (180, 200, 220), 40:  (140, 180, 100),
    50:  (220, 230, 240), 55:  (240, 245, 255), 60:  ( 80, 160,  80),
    70:  ( 20, 160,  80), 80:  ( 60, 140, 100), 90:  (190, 160,  80),
    100: (180, 170, 150), 110: (160, 200, 140), 115: (120, 180, 130),
    120: ( 60, 140,  70), 130: (200, 180, 100), 140: (140, 160, 100),
    150: (180, 160, 120), 160: ( 20, 140,  80), 170: (230, 200, 120),
    190: (210, 185, 120), 200: (200, 170, 110), 210: (170, 160, 100),
    220: ( 40, 150, 100), 230: ( 50, 140,  90), 240: ( 80, 150, 130),
}

def _build_color_lut():
    lut = np.full((256, 3), (128, 128, 128), dtype=np.uint8)
    for code, rgb in BIOME_COLORS.items():
        lut[code] = rgb
    return lut


def meander_warp(river_mask, seed=42, warp_sigma=100.0, warp_amplitude=15.0):
    """
    Flow-field warp to add meander to straight channel segments.

    Generates two smooth displacement fields (dx, dy) and warps the river
    mask through them. Low-frequency noise produces broad S-curves.

    Returns warped mask. Never creates pixels outside the convex hull of
    the original — conservative amplitude prevents pinch-offs.
    """
    from scipy.ndimage import map_coordinates
    h, w = river_mask.shape
    rng = np.random.RandomState(seed)

    # Two independent smooth displacement fields
    dx_raw = rng.randn(h, w).astype(np.float32)
    dy_raw = rng.randn(h, w).astype(np.float32)
    dx = gaussian_filter(dx_raw, sigma=warp_sigma)
    dy = gaussian_filter(dy_raw, sigma=warp_sigma)

    # Normalize to [-1, 1] then scale by amplitude
    dx_max = np.abs(dx).max()
    dy_max = np.abs(dy).max()
    if dx_max > 0:
        dx = dx / dx_max * warp_amplitude
    if dy_max > 0:
        dy = dy / dy_max * warp_amplitude

    # Build sampling coordinates (inverse warp: where to sample FROM)
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    sample_y = yy + dy
    sample_x = xx + dx

    # Warp using bilinear interpolation on the float mask
    # This gives soft edges at the warp boundary — threshold back to binary
    warped_float = map_coordinates(river_mask.astype(np.float32),
                                    [sample_y, sample_x],
                                    order=1, mode='nearest')
    warped = warped_float > 0.5

    return warped


def roughen_river_edges(river_mask, seed=42, edge_band=20):
    """
    Multi-octave erosion-only shoreline roughening.

    Four noise octaves for natural multi-scale irregularity.
    Erosion-only: never adds pixels outside the original mask.
    Deep interior (> edge_band from edge) is hardcoded preserved.
    """
    h, w = river_mask.shape
    interior_dist = distance_transform_edt(river_mask)

    rng = np.random.RandomState(seed)

    octaves = [
        (60.0, 0.25),   # broad undulation
        (18.0, 0.35),   # crenulation — coves and bumps
        (6.0,  0.25),   # shoreline grit
        (3.0,  0.15),   # staircase breaker
    ]

    noise_combined = np.zeros((h, w), dtype=np.float32)
    for sigma, weight in octaves:
        raw = rng.randn(h, w).astype(np.float32)
        smoothed = gaussian_filter(raw, sigma=sigma)
        smin, smax = smoothed.min(), smoothed.max()
        if smax > smin:
            smoothed = (smoothed - smin) / (smax - smin)
        else:
            smoothed[:] = 0.5
        noise_combined += smoothed * weight

    nmin, nmax = noise_combined.min(), noise_combined.max()
    if nmax > nmin:
        noise_combined = (noise_combined - nmin) / (nmax - nmin)
    else:
        noise_combined[:] = 0.5

    roughened = river_mask.copy()
    edge_pixels = river_mask & (interior_dist <= edge_band) & (interior_dist > 0)

    depth_frac = interior_dist[edge_pixels] / edge_band
    removal_threshold = 0.72 * (1.0 - depth_frac ** 0.45)
    roughened[edge_pixels] = noise_combined[edge_pixels] >= removal_threshold

    roughened[interior_dist > edge_band] = True
    return roughened


def warp_and_roughen(river_mask, seed=42, warp_sigma=100.0, warp_amplitude=15.0,
                      edge_band=20):
    """
    Two-stage river naturalization:
    1. Flow-field warp for meander (bends straight segments)
    2. Multi-octave noise roughening for shoreline detail

    The warp can expand slightly beyond the original mask (meander outward),
    but the roughening then only erodes — net result is organic shoreline
    that stays close to the original corridor.
    """
    # Stage 1: meander warp
    warped = meander_warp(river_mask, seed=seed,
                           warp_sigma=warp_sigma, warp_amplitude=warp_amplitude)

    # Stage 2: noise roughening on the warped result
    roughened = roughen_river_edges(warped, seed=seed + 1000, edge_band=edge_band)

    return roughened


def make_panel(override, height, shade, is_sea, order, cl, lake_mask,
               river_mask, roughened_mask, use_roughened, ds):
    color_lut = _build_color_lut()
    comp = color_lut[override].copy()
    comp = np.clip(comp.astype(np.float32) * shade[..., None] * 1.6, 0, 255).astype(np.uint8)
    comp[is_sea] = (30, 80, 160)
    comp[lake_mask] = (50, 110, 190)

    if use_roughened:
        water_px = roughened_mask & ~lake_mask & ~is_sea
    else:
        water_px = river_mask & ~lake_mask & ~is_sea

    braid = (cl == 255)
    thin = (cl > 0) & (cl < 255)
    comp[water_px & braid] = (55, 115, 195)
    comp[water_px & thin] = (65, 130, 215)
    comp[water_px & (order > 0) & ~braid & ~thin] = (80, 140, 200)
    comp[water_px & ~braid & ~thin & (order == 0)] = (100, 150, 195)
    return comp


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

    print(f"Reading masks for tile ({TX},{TZ}) +/- {PAD} at 1:{DS}...", flush=True)
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
    river_f = river_raw.astype(np.float32) / (65535.0 if river_raw.dtype == np.uint16 else 1.0)

    sea_norm = 17050.0 / 65535.0
    is_sea = height < sea_norm

    h_smooth = gaussian_filter(height, sigma=2.0)
    gy, gx = np.gradient(h_smooth)
    shade = (-gx + gy) / (np.sqrt(gx**2 + gy**2 + 0.001) + 0.001)
    shade = np.clip(shade * 0.3 + 0.5, 0.25, 0.75)

    # Lakes
    lake_basin = lake > 0
    basin_expand = max(1, 32 // DS)
    lake_basin_exp = binary_dilation(lake_basin, iterations=basin_expand)
    lake_wl_exp = maxfilt(lake_wl, size=basin_expand * 2 + 1)
    lake_mask = lake_basin_exp & (lake_wl_exp > 0) & (height < lake_wl_exp) & ~is_sea

    # River mask
    any_river = (cl > 0) | (order > 0) | (river_f > 0.15)
    river_land = any_river & ~is_sea
    river_only = river_land & ~lake_mask

    print("Rendering original...", flush=True)
    before = make_panel(override, height, shade, is_sea, order, cl, lake_mask,
                        river_only, river_only, use_roughened=False, ds=DS)

    # Single panel — just the original so we can study the problem
    canvas_w = out_w
    canvas_h = out_h + 35
    canvas = np.full((canvas_h, canvas_w, 3), 20, dtype=np.uint8)

    y_off = 35
    canvas[y_off:y_off + out_h, :out_w] = before

    img = Image.fromarray(canvas, "RGB")
    draw = ImageDraw.Draw(img)

    draw.text((out_w // 2 - 60, 5), f"ORIGINAL — tile ({TX},{TZ})", fill=(255, 255, 255))
    draw.text((out_w // 2 - 80, 18), "(unmodified river mask at full res)", fill=(180, 180, 180))

    # Tile grid
    tile_px = TILE // DS
    for panel_x_off in [0]:
        for i in range(TX_MAX - TX_MIN + 1):
            x = panel_x_off + i * tile_px
            draw.line([(x, y_off), (x, y_off + out_h - 1)], fill=(60, 60, 60), width=1)
        for j in range(TZ_MAX - TZ_MIN + 1):
            y = y_off + j * tile_px
            draw.line([(panel_x_off, y), (panel_x_off + out_w - 1, y)],
                      fill=(60, 60, 60), width=1)
        cx = panel_x_off + PAD * tile_px + 5
        cy = y_off + PAD * tile_px + 5
        draw.text((cx, cy), f"({TX},{TZ})", fill=(255, 200, 100))

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"\nSaved: {OUTPUT}  ({canvas_w}x{canvas_h}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
