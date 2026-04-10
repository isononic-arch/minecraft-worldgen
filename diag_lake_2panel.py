"""
diag_lake_2panel.py — Side-by-side 3x3 view:
  Left:  Pure terrain intersection (no depression)
  Right: Terrain intersection + basin depression (4 blocks, power 0.8)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

MASKS_DIR   = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG_PATH = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
OUTPUT      = Path(r"C:\Users\nicho\minecraft-worldgen\output\lake_2panel_3x3.png")

CENTRE_TX, CENTRE_TZ = 51, 53
TILE = 512
GRID = 3
HALF = GRID // 2
BASIN_EXPAND_PX    = 32
CONTOUR_INTERVAL   = 5
LAKE_MIN_DEPTH     = 15.0


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def height_norm_to_mc_y(h_norm, cfg):
    sp = cfg.get("terrain_spline", {})
    gaea_in = np.array(sp.get("gaea_in", [0, 17050, 45000, 65496]), dtype=np.float64)
    mc_y    = np.array(sp.get("mc_y_out", [-64, 63, 200, 448]), dtype=np.float64)
    gaea_norm = gaea_in / 65535.0
    return np.interp(h_norm.ravel(), gaea_norm, mc_y).reshape(h_norm.shape).astype(np.float32)


def read_region(tif_path, col_off, row_off, w, h):
    import rasterio
    from rasterio.windows import Window
    with rasterio.open(str(tif_path)) as src:
        return src.read(1, window=Window(col_off, row_off, w, h))


def render_3x3(terrain_y, underwater, depth, order_r, region_h, region_w, label):
    """Render a full 3x3 topdown panel."""
    from scipy.ndimage import distance_transform_edt

    SEA_LEVEL = 63
    land_mask = terrain_y > SEA_LEVEL
    river_mask = (order_r > 0) & ~underwater

    # EDT depth floor
    if underwater.any() and depth is not None:
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
    if underwater.any() and depth is not None:
        md = max(float(depth[underwater].max()), 1.0)
        dn = np.clip(depth / md, 0, 1)
        img_r[underwater] = 0.30 - dn[underwater] * 0.25
        img_g[underwater] = 0.70 - dn[underwater] * 0.55
        img_b[underwater] = 1.00 - dn[underwater] * 0.50

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
            draw.line([(x, 0), (x, region_h-1)], fill=(200,200,0,140), width=1)
        y = i * TILE
        if y < region_h:
            draw.line([(0, y), (region_w-1, y)], fill=(200,200,0,140), width=1)

    # Tile labels
    tx0 = CENTRE_TX - HALF
    tz0 = CENTRE_TZ - HALF
    for gi in range(GRID):
        for gj in range(GRID):
            draw.text((gi*TILE+4, gj*TILE+4),
                      f"({tx0+gi},{tz0+gj})", fill=(255,255,0,180))

    # Panel label + stats
    uw_pct = underwater.sum() * 100 / (region_h * region_w)
    draw.text((8, region_h - 24),
              f"{label}  |  {underwater.sum()} px ({uw_pct:.1f}%)",
              fill=(255, 255, 255, 220))

    return pil


def main():
    t0 = time.perf_counter()
    cfg = load_config()
    from scipy.ndimage import binary_dilation, maximum_filter, distance_transform_edt

    tx0 = CENTRE_TX - HALF
    tz0 = CENTRE_TZ - HALF
    col_off = tx0 * TILE
    row_off = tz0 * TILE
    region_w = GRID * TILE
    region_h = GRID * TILE

    PAD = BASIN_EXPAND_PX + 8
    px0 = max(col_off - PAD, 0)
    pz0 = max(row_off - PAD, 0)
    pw = region_w + PAD + (col_off - px0)
    ph = region_h + PAD + (row_off - pz0)

    print("Reading masks ...")
    h_raw = read_region(MASKS_DIR / "height.tif", px0, pz0, pw, ph)
    h_norm = h_raw.astype(np.float32) / 65535.0
    wl = read_region(MASKS_DIR / "hydro_lake_wl.tif", px0, pz0, pw, ph).astype(np.float32)
    lake_id = read_region(MASKS_DIR / "hydro_lake.tif", px0, pz0, pw, ph)
    order_raw = read_region(MASKS_DIR / "hydro_order.tif", px0, pz0, pw, ph)

    # Expand basin
    basin = wl > 0
    basin_orig = lake_id > 0
    basin_exp = binary_dilation(basin_orig, iterations=BASIN_EXPAND_PX)
    k = BASIN_EXPAND_PX * 2 + 1
    wl_exp = maximum_filter(wl, size=k)
    new_zone = basin_exp & ~basin
    wl[new_zone] = wl_exp[new_zone]
    basin = wl > 0

    terrain_y = height_norm_to_mc_y(h_norm, cfg)
    water_y = height_norm_to_mc_y(wl, cfg)

    cr0 = row_off - pz0
    cc0 = col_off - px0

    # ── Panel A: Pure terrain intersection ────────────────────────────────
    print("Panel A: Pure terrain intersection ...")
    uw_a = basin & (terrain_y < water_y)
    depth_a = np.where(uw_a, water_y - terrain_y, 0.0)

    uw_a_r = uw_a[cr0:cr0+region_h, cc0:cc0+region_w]
    depth_a_r = depth_a[cr0:cr0+region_h, cc0:cc0+region_w]
    terrain_r = terrain_y[cr0:cr0+region_h, cc0:cc0+region_w]
    order_r = order_raw[cr0:cr0+region_h, cc0:cc0+region_w]

    panel_a = render_3x3(terrain_r.copy(), uw_a_r, depth_a_r.copy(), order_r,
                         region_h, region_w, "TERRAIN ONLY")

    # ── Panel B: Terrain + basin depression ───────────────────────────────
    print("Panel B: Terrain + depression ...")
    terrain_dep = terrain_y.copy()
    if basin.any():
        edt = distance_transform_edt(basin).astype(np.float32)
        edt_max = max(float(edt.max()), 1.0)
        edt_norm = edt / edt_max
        depression = (edt_norm ** 0.8) * 4.0
        terrain_dep[basin] -= depression[basin]

    uw_b = basin & (terrain_dep < water_y)
    depth_b = np.where(uw_b, water_y - terrain_dep, 0.0)

    uw_b_r = uw_b[cr0:cr0+region_h, cc0:cc0+region_w]
    depth_b_r = depth_b[cr0:cr0+region_h, cc0:cc0+region_w]
    terrain_dep_r = terrain_dep[cr0:cr0+region_h, cc0:cc0+region_w]

    panel_b = render_3x3(terrain_dep_r, uw_b_r, depth_b_r.copy(), order_r,
                         region_h, region_w, "TERRAIN + DEPRESS (4blk)")

    # ── Stitch side by side ───────────────────────────────────────────────
    gap = 6
    total_w = region_w * 2 + gap
    out = Image.new("RGBA", (total_w, region_h), (30, 30, 30, 255))
    out.paste(panel_a, (0, 0))
    out.paste(panel_b, (region_w + gap, 0))

    OUTPUT.parent.mkdir(exist_ok=True)
    out.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"Saved: {OUTPUT}  ({total_w}x{region_h}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
