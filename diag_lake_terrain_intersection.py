"""
diag_lake_terrain_intersection.py — Render 3×3 topdown view of lake area
using terrain-intersection carving (no morph/blur).

Reads height.tif + hydro_lake_wl.tif + hydro_order.tif directly,
computes lake mask from terrain vs water-level comparison, renders
topdown with depth colouring, contour lines, tile grid, and rivers.

Lake tiles: 50-52 × 52-54 (9 tiles covering the main lake)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# ─── Config ───────────────────────────────────────────────────────────────
MASKS_DIR   = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG_PATH = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
OUTPUT      = Path(r"C:\Users\nicho\minecraft-worldgen\output\lake_3x3_topdown_padded_nms.png")

# 3×3 tile grid centred on (51, 53) — covers tiles 50-52 × 52-54
CENTRE_TX, CENTRE_TZ = 51, 53
TILE = 512
GRID = 3
HALF = GRID // 2

# Basin expansion and basin depression
BASIN_EXPAND_PX       = 32
LAKE_BASIN_DEPRESS    = 4    # max MC Y blocks to lower basin floor at center
LAKE_DEPRESS_POWER    = 0.8  # EDT falloff exponent (lower = flatter profile)

# Contour line interval in MC Y blocks
CONTOUR_INTERVAL = 5


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def height_norm_to_mc_y(h_norm, cfg):
    """Convert normalised height [0,1] → MC Y blocks via terrain spline."""
    sp = cfg.get("terrain_spline", {})
    gaea_in = np.array(sp.get("gaea_in", [0, 17050, 45000, 65496]), dtype=np.float64)
    mc_y    = np.array(sp.get("mc_y_out", [-64, 63, 200, 448]), dtype=np.float64)
    gaea_norm = gaea_in / 65535.0
    return np.interp(h_norm.ravel(), gaea_norm, mc_y).reshape(h_norm.shape).astype(np.float32)


def read_region(tif_path, col_off, row_off, w, h):
    """Read a window from a TIF, return raw array."""
    import rasterio
    from rasterio.windows import Window
    with rasterio.open(str(tif_path)) as src:
        return src.read(1, window=Window(col_off, row_off, w, h))


def main():
    t0 = time.perf_counter()
    cfg = load_config()

    from scipy.ndimage import binary_dilation, maximum_filter, distance_transform_edt

    # ── Region geometry ───────────────────────────────────────────────────
    tx0 = CENTRE_TX - HALF  # 50
    tz0 = CENTRE_TZ - HALF  # 52
    col_off = tx0 * TILE     # 25600
    row_off = tz0 * TILE     # 26624
    region_w = GRID * TILE   # 1536
    region_h = GRID * TILE   # 1536

    print(f"Reading 3×3 region: tiles ({tx0}-{tx0+GRID-1}) × ({tz0}-{tz0+GRID-1})")
    print(f"  pixel window: col={col_off}, row={row_off}, {region_w}×{region_h}")

    # ── Read with padding for basin expansion ─────────────────────────────
    PAD = BASIN_EXPAND_PX + 8  # extra margin
    px0 = max(col_off - PAD, 0)
    pz0 = max(row_off - PAD, 0)
    pw  = region_w + PAD + (col_off - px0)  # accounts for clamped left edge
    ph  = region_h + PAD + (row_off - pz0)

    print("Reading height.tif ...")
    h_raw = read_region(MASKS_DIR / "height.tif", px0, pz0, pw, ph)
    h_norm = h_raw.astype(np.float32) / 65535.0

    print("Reading hydro_lake_wl.tif ...")
    wl = read_region(MASKS_DIR / "hydro_lake_wl.tif", px0, pz0, pw, ph).astype(np.float32)

    print("Reading hydro_lake.tif ...")
    lake_id = read_region(MASKS_DIR / "hydro_lake.tif", px0, pz0, pw, ph)

    print("Reading hydro_order.tif ...")
    order = read_region(MASKS_DIR / "hydro_order.tif", px0, pz0, pw, ph)

    # ── Terrain intersection ──────────────────────────────────────────────
    print("Computing terrain intersection ...")

    # Basin expansion: dilate lake region so terrain intersection can
    # reach past the 8×8 NEAREST staircase edge
    basin = wl > 0
    if BASIN_EXPAND_PX > 0:
        basin_orig = lake_id > 0
        basin_exp = binary_dilation(basin_orig, iterations=BASIN_EXPAND_PX)
        k = BASIN_EXPAND_PX * 2 + 1
        wl_exp = maximum_filter(wl, size=k)
        new_zone = basin_exp & ~basin
        wl[new_zone] = wl_exp[new_zone]
        basin = wl > 0

    # Convert to MC Y
    terrain_y = height_norm_to_mc_y(h_norm, cfg)
    water_y   = height_norm_to_mc_y(wl, cfg)

    # Depress the basin floor: lower terrain within the basin proportional
    # to distance from shore.  Shore pixels barely move (organic contour
    # preserved), center pixels drop by up to LAKE_BASIN_DEPRESS blocks.
    if LAKE_BASIN_DEPRESS > 0 and basin.any():
        edt_basin = distance_transform_edt(basin).astype(np.float32)
        edt_max = max(float(edt_basin.max()), 1.0)
        edt_norm = edt_basin / edt_max
        depression = (edt_norm ** LAKE_DEPRESS_POWER) * LAKE_BASIN_DEPRESS
        terrain_y[basin] -= depression[basin]

    # Underwater test
    underwater = basin & (terrain_y < water_y)
    nat_depth = np.where(underwater, water_y - terrain_y, 0.0)

    # EDT depth floor: ensures meaningful depth toward lake center
    # even when the natural basin is nearly flat
    LAKE_MIN_CENTER_DEPTH = 15.0  # blocks
    if underwater.any():
        shore_dist = distance_transform_edt(underwater).astype(np.float32)
        max_sd = max(float(shore_dist.max()), 1.0)
        shore_norm = shore_dist / max_sd
        synth_depth = (shore_norm ** 1.3) * LAKE_MIN_CENTER_DEPTH
        depth = np.maximum(nat_depth, synth_depth)
        depth[~underwater] = 0.0
    else:
        depth = nat_depth

    # Crop to region extent (remove padding)
    cr0 = row_off - pz0
    cc0 = col_off - px0
    terrain_y_r = terrain_y[cr0:cr0+region_h, cc0:cc0+region_w]
    underwater_r = underwater[cr0:cr0+region_h, cc0:cc0+region_w]
    depth_r     = depth[cr0:cr0+region_h, cc0:cc0+region_w]
    order_r     = order[cr0:cr0+region_h, cc0:cc0+region_w]
    basin_r     = basin[cr0:cr0+region_h, cc0:cc0+region_w]

    lake_px = underwater_r.sum()
    print(f"  Underwater pixels: {lake_px} ({lake_px*100/(region_w*region_h):.1f}%)")
    if lake_px > 0:
        print(f"  Depth range: {depth_r[underwater_r].min():.1f} - {depth_r[underwater_r].max():.1f} blocks")

    # ── River mask — padded NMS flow ridgeline ──────────────────────────
    # Read flow.tif for the region (with NMS padding) and extract
    # thin NMS centerlines matching river_carver_v2.py's padded approach.
    NMS_PAD = 17  # flow_refine_radius(8) + flow_nms_window(9)
    nms_px0 = max(col_off - NMS_PAD, 0)
    nms_pz0 = max(row_off - NMS_PAD, 0)
    nms_pw  = region_w + NMS_PAD + (col_off - nms_px0)
    nms_ph  = region_h + NMS_PAD + (row_off - nms_pz0)

    print("Reading flow.tif for NMS ...")
    flow_raw = read_region(MASKS_DIR / "flow.tif", nms_px0, nms_pz0, nms_pw, nms_ph)
    flow_f = flow_raw.astype(np.float32) / 65535.0

    print("Reading hydro_order.tif (padded for NMS) ...")
    order_nms = read_region(MASKS_DIR / "hydro_order.tif", nms_px0, nms_pz0, nms_pw, nms_ph).astype(np.uint8)

    print("Computing padded NMS ...")
    nms_cl_raw = order_nms > 0
    nms_corridor = binary_dilation(nms_cl_raw, iterations=8)
    nms_flow_corr = np.where(nms_corridor, flow_f, 0.0)
    nms_flow_peak = maximum_filter(nms_flow_corr, size=9)
    nms_centerline = (nms_corridor
                      & (flow_f >= nms_flow_peak * 0.85)
                      & (flow_f > 0.001))

    # Crop NMS result to region extent
    nms_cr0 = row_off - nms_pz0
    nms_cc0 = col_off - nms_px0
    river_mask = nms_centerline[nms_cr0:nms_cr0+region_h, nms_cc0:nms_cc0+region_w] & ~underwater_r

    # Blob removal: per-tile, keep edge-touching components >= 150px
    from scipy.ndimage import label as _label_blobs
    for gi in range(GRID):
        for gj in range(GRID):
            r0, r1 = gj * TILE, (gj + 1) * TILE
            c0, c1 = gi * TILE, (gi + 1) * TILE
            tile_cl = river_mask[r0:r1, c0:c1]
            lbl, n = _label_blobs(tile_cl)
            if n > 1:
                edge = np.zeros_like(tile_cl)
                edge[0, :] = True; edge[-1, :] = True
                edge[:, 0] = True; edge[:, -1] = True
                for cid in range(1, n + 1):
                    comp = lbl == cid
                    if not ((comp & edge).any() and comp.sum() >= 150):
                        tile_cl[comp] = False

    river_px = river_mask.sum()
    print(f"  River pixels (NMS): {river_px} ({river_px*100/(region_w*region_h):.1f}%)")

    # ── Build RGBA image ──────────────────────────────────────────────────
    print("Rendering topdown ...")

    SEA_LEVEL = 63
    land_mask = terrain_y_r > SEA_LEVEL

    # Terrain base colour — brown/green hillshade style
    # Normalize terrain for display
    t_min, t_max = float(terrain_y_r[land_mask].min()) if land_mask.any() else 60, \
                   float(terrain_y_r[land_mask].max()) if land_mask.any() else 200
    t_range = max(t_max - t_min, 1.0)
    t_norm = np.clip((terrain_y_r - t_min) / t_range, 0, 1)

    # Hillshade
    gy, gx = np.gradient(terrain_y_r)
    sun_az = np.radians(315)
    sun_alt = np.radians(45)
    lx = np.cos(sun_alt) * np.sin(sun_az)
    ly = np.cos(sun_alt) * np.cos(sun_az)
    lz = np.sin(sun_alt)
    mag = np.sqrt(gx**2 + gy**2 + 1.0)
    shade = np.clip((-gx * lx - gy * ly + lz) / mag, 0, 1)

    # Terrain colour ramp: dark brown (low) → olive (mid) → tan (high)
    r_land = np.clip(0.25 + t_norm * 0.35, 0, 1)
    g_land = np.clip(0.20 + t_norm * 0.25, 0, 1)
    b_land = np.clip(0.10 + t_norm * 0.12, 0, 1)

    # Apply hillshade
    shade_f = 0.4 + shade * 0.6
    r_land *= shade_f
    g_land *= shade_f
    b_land *= shade_f

    # Contour lines on terrain
    contour_val = terrain_y_r / CONTOUR_INTERVAL
    contour_frac = contour_val - np.floor(contour_val)
    contour_line = (contour_frac < 0.06) | (contour_frac > 0.94)
    contour_line &= land_mask & ~underwater_r

    # Darken contour lines
    contour_darken = np.where(contour_line, 0.65, 1.0)
    r_land *= contour_darken
    g_land *= contour_darken
    b_land *= contour_darken

    # Start with terrain
    img_r = r_land.copy()
    img_g = g_land.copy()
    img_b = b_land.copy()

    # ── Lake water colouring ──────────────────────────────────────────────
    # Depth → blue gradient: shallow = light cyan, deep = dark navy
    if lake_px > 0:
        max_depth = max(float(depth_r[underwater_r].max()), 1.0)
        d_norm = np.clip(depth_r / max_depth, 0, 1)

        # Shallow: (0.3, 0.7, 1.0) → Deep: (0.05, 0.15, 0.5)
        lake_r = np.where(underwater_r, 0.30 - d_norm * 0.25, 0)
        lake_g = np.where(underwater_r, 0.70 - d_norm * 0.55, 0)
        lake_b = np.where(underwater_r, 1.00 - d_norm * 0.50, 0)

        img_r[underwater_r] = lake_r[underwater_r]
        img_g[underwater_r] = lake_g[underwater_r]
        img_b[underwater_r] = lake_b[underwater_r]

        # Underwater contour lines (depth contours every 2 blocks)
        depth_contour_val = depth_r / 2.0
        depth_contour_frac = depth_contour_val - np.floor(depth_contour_val)
        depth_contour_line = ((depth_contour_frac < 0.08) | (depth_contour_frac > 0.92)) & underwater_r
        img_r[depth_contour_line] *= 0.7
        img_g[depth_contour_line] *= 0.7
        img_b[depth_contour_line] *= 0.7

    # ── River colouring — by Strahler order ─────────────────────────────
    # Propagate order to NMS centerline pixels, colour streams vs rivers
    order_prop = maximum_filter(order_r.astype(np.float32), size=17).astype(np.uint8)
    river_order = np.where(river_mask, order_prop, 0)

    # Streams (order 1-2): light cyan
    stream_px = river_mask & (river_order <= 2)
    img_r[stream_px] = 0.3
    img_g[stream_px] = 0.75
    img_b[stream_px] = 0.95

    # Rivers (order 3+): deeper blue
    river_px_mask = river_mask & (river_order >= 3)
    img_r[river_px_mask] = 0.15
    img_g[river_px_mask] = 0.45
    img_b[river_px_mask] = 0.90

    # ── Ocean (below sea level, not lake) ─────────────────────────────────
    ocean = (terrain_y_r <= SEA_LEVEL) & ~underwater_r
    img_r[ocean] = 0.1
    img_g[ocean] = 0.2
    img_b[ocean] = 0.4

    # ── Convert to uint8 ──────────────────────────────────────────────────
    rgba = np.zeros((region_h, region_w, 4), dtype=np.uint8)
    rgba[:, :, 0] = np.clip(img_r * 255, 0, 255).astype(np.uint8)
    rgba[:, :, 1] = np.clip(img_g * 255, 0, 255).astype(np.uint8)
    rgba[:, :, 2] = np.clip(img_b * 255, 0, 255).astype(np.uint8)
    rgba[:, :, 3] = 255

    img = Image.fromarray(rgba, "RGBA")
    draw = ImageDraw.Draw(img)

    # ── Tile grid overlay ─────────────────────────────────────────────────
    for i in range(GRID + 1):
        # Vertical lines
        x = i * TILE
        if x < region_w:
            draw.line([(x, 0), (x, region_h - 1)], fill=(200, 200, 0, 180), width=1)
        # Horizontal lines
        y = i * TILE
        if y < region_h:
            draw.line([(0, y), (region_w - 1, y)], fill=(200, 200, 0, 180), width=1)

    # Tile labels
    for gi in range(GRID):
        for gj in range(GRID):
            tx = tx0 + gi
            tz = tz0 + gj
            lx = gi * TILE + 4
            ly = gj * TILE + 4
            draw.text((lx, ly), f"({tx},{tz})", fill=(255, 255, 0, 200))

    # ── Save ──────────────────────────────────────────────────────────────
    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"Saved: {OUTPUT}  ({region_w}×{region_h}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
