"""
diag_lake_wl_sweep.py — Render lake at multiple water-level offsets
side by side to find the right fill amount.

Tests: +0 (current), +2, +4, +6, +8 MC Y blocks of water level raise.
Outputs a 5-panel comparison strip.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

MASKS_DIR   = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG_PATH = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
OUTPUT      = Path(r"C:\Users\nicho\minecraft-worldgen\output\lake_wl_sweep.png")

CENTRE_TX, CENTRE_TZ = 51, 53
TILE = 512
GRID = 3
HALF = GRID // 2
BASIN_EXPAND_PX = 32
CONTOUR_INTERVAL = 5

# Water level raises in MC Y blocks to test
WL_RAISES = [0, 2, 4, 6, 8]


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def height_norm_to_mc_y(h_norm, cfg):
    sp = cfg.get("terrain_spline", {})
    gaea_in = np.array(sp.get("gaea_in", [0, 17050, 45000, 65496]), dtype=np.float64)
    mc_y    = np.array(sp.get("mc_y_out", [-64, 63, 200, 448]), dtype=np.float64)
    gaea_norm = gaea_in / 65535.0
    return np.interp(h_norm.ravel(), gaea_norm, mc_y).reshape(h_norm.shape).astype(np.float32)


def mc_y_to_height_norm(mc_y_val, cfg):
    """Inverse: MC Y blocks → normalised height."""
    sp = cfg.get("terrain_spline", {})
    gaea_in = np.array(sp.get("gaea_in", [0, 17050, 45000, 65496]), dtype=np.float64)
    mc_y    = np.array(sp.get("mc_y_out", [-64, 63, 200, 448]), dtype=np.float64)
    gaea_norm = gaea_in / 65535.0
    return float(np.interp(mc_y_val, mc_y, gaea_norm))


def read_region(tif_path, col_off, row_off, w, h):
    import rasterio
    from rasterio.windows import Window
    with rasterio.open(str(tif_path)) as src:
        return src.read(1, window=Window(col_off, row_off, w, h))


def render_panel(terrain_y, underwater, depth, order_r, region_h, region_w, label, raise_blocks):
    """Render one topdown panel, return PIL Image."""
    from scipy.ndimage import distance_transform_edt

    SEA_LEVEL = 63
    land_mask = terrain_y > SEA_LEVEL
    river_mask = (order_r > 0) & ~underwater

    # EDT depth floor
    LAKE_MIN_DEPTH = 15.0
    if underwater.any():
        sd = distance_transform_edt(underwater).astype(np.float32)
        sd_max = max(float(sd.max()), 1.0)
        synth = (sd / sd_max) ** 1.3 * LAKE_MIN_DEPTH
        depth = np.maximum(depth, synth)
        depth[~underwater] = 0.0

    # Terrain colour
    t_min = float(terrain_y[land_mask].min()) if land_mask.any() else 60
    t_max = float(terrain_y[land_mask].max()) if land_mask.any() else 200
    t_range = max(t_max - t_min, 1.0)
    t_norm = np.clip((terrain_y - t_min) / t_range, 0, 1)

    gy, gx = np.gradient(terrain_y)
    sun_az, sun_alt = np.radians(315), np.radians(45)
    lx = np.cos(sun_alt) * np.sin(sun_az)
    ly = np.cos(sun_alt) * np.cos(sun_az)
    lz = np.sin(sun_alt)
    mag = np.sqrt(gx**2 + gy**2 + 1.0)
    shade = np.clip((-gx * lx - gy * ly + lz) / mag, 0, 1)

    r_l = np.clip(0.25 + t_norm * 0.35, 0, 1) * (0.4 + shade * 0.6)
    g_l = np.clip(0.20 + t_norm * 0.25, 0, 1) * (0.4 + shade * 0.6)
    b_l = np.clip(0.10 + t_norm * 0.12, 0, 1) * (0.4 + shade * 0.6)

    # Contour lines
    cv = terrain_y / CONTOUR_INTERVAL
    cf = cv - np.floor(cv)
    cl = ((cf < 0.06) | (cf > 0.94)) & land_mask & ~underwater
    cd = np.where(cl, 0.65, 1.0)
    r_l *= cd; g_l *= cd; b_l *= cd

    img_r, img_g, img_b = r_l.copy(), g_l.copy(), b_l.copy()

    # Lake
    lake_px = underwater.sum()
    if lake_px > 0:
        md = max(float(depth[underwater].max()), 1.0)
        dn = np.clip(depth / md, 0, 1)
        img_r[underwater] = 0.30 - dn[underwater] * 0.25
        img_g[underwater] = 0.70 - dn[underwater] * 0.55
        img_b[underwater] = 1.00 - dn[underwater] * 0.50

        # Depth contour lines
        dcv = depth / 2.0
        dcf = dcv - np.floor(dcv)
        dcl = ((dcf < 0.08) | (dcf > 0.92)) & underwater
        img_r[dcl] *= 0.7; img_g[dcl] *= 0.7; img_b[dcl] *= 0.7

    # Rivers
    img_r[river_mask] = 0.3; img_g[river_mask] = 0.75; img_b[river_mask] = 0.95

    # Ocean
    ocean = (terrain_y <= SEA_LEVEL) & ~underwater
    img_r[ocean] = 0.1; img_g[ocean] = 0.2; img_b[ocean] = 0.4

    rgba = np.zeros((region_h, region_w, 4), dtype=np.uint8)
    rgba[:,:,0] = np.clip(img_r * 255, 0, 255).astype(np.uint8)
    rgba[:,:,1] = np.clip(img_g * 255, 0, 255).astype(np.uint8)
    rgba[:,:,2] = np.clip(img_b * 255, 0, 255).astype(np.uint8)
    rgba[:,:,3] = 255

    pil = Image.fromarray(rgba, "RGBA")
    draw = ImageDraw.Draw(pil)

    # Tile grid
    for i in range(GRID + 1):
        x = i * TILE
        if x < region_w:
            draw.line([(x, 0), (x, region_h-1)], fill=(200,200,0,120), width=1)
        y = i * TILE
        if y < region_h:
            draw.line([(0, y), (region_w-1, y)], fill=(200,200,0,120), width=1)

    # Label
    draw.text((8, region_h - 30), f"+{raise_blocks} blocks  ({lake_px} px)",
              fill=(255, 255, 255, 220))
    draw.text((8, 8), label, fill=(255, 255, 0, 220))

    return pil


def main():
    t0 = time.perf_counter()
    cfg = load_config()
    from scipy.ndimage import binary_dilation, maximum_filter

    tx0 = CENTRE_TX - HALF
    tz0 = CENTRE_TZ - HALF
    col_off = tx0 * TILE
    row_off = tz0 * TILE
    region_w = GRID * TILE
    region_h = GRID * TILE

    PAD = BASIN_EXPAND_PX + 8
    px0 = max(col_off - PAD, 0)
    pz0 = max(row_off - PAD, 0)
    pw  = region_w + PAD + (col_off - px0)
    ph  = region_h + PAD + (row_off - pz0)

    print("Reading masks ...")
    h_raw = read_region(MASKS_DIR / "height.tif", px0, pz0, pw, ph)
    h_norm = h_raw.astype(np.float32) / 65535.0
    wl_base = read_region(MASKS_DIR / "hydro_lake_wl.tif", px0, pz0, pw, ph).astype(np.float32)
    lake_id = read_region(MASKS_DIR / "hydro_lake.tif", px0, pz0, pw, ph)
    order = read_region(MASKS_DIR / "hydro_order.tif", px0, pz0, pw, ph)

    # Expand basin once (same for all panels)
    basin_orig = lake_id > 0
    basin_exp = binary_dilation(basin_orig, iterations=BASIN_EXPAND_PX)
    k = BASIN_EXPAND_PX * 2 + 1
    wl_exp_base = maximum_filter(wl_base, size=k)

    terrain_y_full = height_norm_to_mc_y(h_norm, cfg)

    # Crop coords
    cr0 = row_off - pz0
    cc0 = col_off - px0
    terrain_y_r = terrain_y_full[cr0:cr0+region_h, cc0:cc0+region_w]
    order_r = order[cr0:cr0+region_h, cc0:cc0+region_w]

    # Find the max safe raise: min ridge height around basin minus current wl
    basin_mask_r = basin_exp[cr0:cr0+region_h, cc0:cc0+region_w]
    perim = binary_dilation(basin_mask_r, iterations=3) & ~basin_mask_r
    if perim.any():
        ridge_min_y = float(terrain_y_r[perim].min())
        base_wl_y = float(height_norm_to_mc_y(
            np.array([wl_base[wl_base > 0].max()]), cfg)[0])
        max_safe_raise = ridge_min_y - base_wl_y
        print(f"Base WL: {base_wl_y:.1f} MC Y, Ridge min: {ridge_min_y:.1f} MC Y, "
              f"Max safe raise: {max_safe_raise:.1f} blocks")

    panels = []
    for raise_blocks in WL_RAISES:
        print(f"Rendering +{raise_blocks} blocks ...")

        # Convert raise from MC Y blocks back to normalised height offset
        if raise_blocks > 0:
            base_mc_y = height_norm_to_mc_y(
                np.array([wl_base[wl_base > 0].max()]), cfg)[0]
            target_mc_y = base_mc_y + raise_blocks
            target_norm = mc_y_to_height_norm(target_mc_y, cfg)
            base_norm = float(wl_base[wl_base > 0].max())
            norm_offset = target_norm - base_norm
        else:
            norm_offset = 0.0

        # Build wl with raise
        wl = wl_base.copy()
        basin = wl > 0
        new_zone = basin_exp & ~basin
        wl[new_zone] = wl_exp_base[new_zone]
        basin = wl > 0
        wl[basin] += norm_offset

        water_y_full = height_norm_to_mc_y(wl, cfg)
        uw = (wl > 0) & (terrain_y_full < water_y_full)
        nat_depth = np.where(uw, water_y_full - terrain_y_full, 0.0)

        # Crop
        uw_r = uw[cr0:cr0+region_h, cc0:cc0+region_w]
        depth_r = nat_depth[cr0:cr0+region_h, cc0:cc0+region_w]

        panel = render_panel(terrain_y_r, uw_r, depth_r, order_r,
                             region_h, region_w,
                             f"+{raise_blocks}blk", raise_blocks)
        panels.append(panel)
        print(f"  Underwater: {uw_r.sum()} px ({uw_r.sum()*100/(region_h*region_w):.1f}%)")

    # Stitch panels side by side
    total_w = region_w * len(panels) + 4 * (len(panels) - 1)
    out = Image.new("RGBA", (total_w, region_h), (30, 30, 30, 255))
    x = 0
    for p in panels:
        out.paste(p, (x, 0))
        x += region_w + 4

    OUTPUT.parent.mkdir(exist_ok=True)
    out.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"Saved: {OUTPUT}  ({total_w}×{region_h}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
