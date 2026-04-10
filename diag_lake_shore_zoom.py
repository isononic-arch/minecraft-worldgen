"""
diag_lake_shore_zoom.py — Zoomed comparison of lake shoreline detail.

Renders 3 panels side by side, all zoomed into the west shore of the lake:
  1. Old morph+blur approach (recreated)
  2. Terrain intersection (no depression)
  3. Terrain intersection + basin depression

Each panel is 400x400 pixels at 1:1 (one pixel = one block).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

MASKS_DIR   = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG_PATH = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
OUTPUT      = Path(r"C:\Users\nicho\minecraft-worldgen\output\lake_shore_compare.png")

# Zoom window — west shore of the lake (interesting terrain contours)
# These are world pixel coords
ZOOM_COL = 26100  # x start
ZOOM_ROW = 26800  # z start
ZOOM_W   = 500
ZOOM_H   = 500

BASIN_EXPAND_PX    = 32
LAKE_BASIN_DEPRESS = 4
LAKE_DEPRESS_POWER = 0.8
CONTOUR_INTERVAL   = 5


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


def render_panel(terrain_y, underwater, label, region_h, region_w):
    """Render one zoomed panel."""
    SEA_LEVEL = 63
    land_mask = terrain_y > SEA_LEVEL

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
    cd = np.where(cl, 0.55, 1.0)
    r_l *= cd; g_l *= cd; b_l *= cd

    # 1-block contour lines (finer detail at zoom)
    cv1 = terrain_y
    cf1 = cv1 - np.floor(cv1)
    cl1 = ((cf1 < 0.15) | (cf1 > 0.85)) & land_mask & ~underwater & ~cl
    cd1 = np.where(cl1, 0.82, 1.0)
    r_l *= cd1; g_l *= cd1; b_l *= cd1

    img_r, img_g, img_b = r_l.copy(), g_l.copy(), b_l.copy()

    # Lake colouring — simple blue for underwater
    img_r[underwater] = 0.20
    img_g[underwater] = 0.50
    img_b[underwater] = 0.90

    # Shoreline highlight — 1px bright edge where water meets land
    from scipy.ndimage import binary_dilation
    shore_edge = binary_dilation(underwater, iterations=1) & ~underwater & land_mask
    img_r[shore_edge] = 0.9
    img_g[shore_edge] = 0.9
    img_b[shore_edge] = 0.3

    rgba = np.zeros((region_h, region_w, 4), dtype=np.uint8)
    rgba[:,:,0] = np.clip(img_r * 255, 0, 255).astype(np.uint8)
    rgba[:,:,1] = np.clip(img_g * 255, 0, 255).astype(np.uint8)
    rgba[:,:,2] = np.clip(img_b * 255, 0, 255).astype(np.uint8)
    rgba[:,:,3] = 255

    pil = Image.fromarray(rgba, "RGBA")
    draw = ImageDraw.Draw(pil)
    draw.text((6, 6), label, fill=(255, 255, 0, 255))
    draw.text((6, region_h - 20), f"{underwater.sum()} water px", fill=(255, 255, 255, 200))
    return pil


def main():
    t0 = time.perf_counter()
    cfg = load_config()
    from scipy.ndimage import (binary_dilation, binary_closing, binary_opening,
                                maximum_filter, distance_transform_edt, gaussian_filter)

    PAD = BASIN_EXPAND_PX + 8
    px0 = max(ZOOM_COL - PAD, 0)
    pz0 = max(ZOOM_ROW - PAD, 0)
    pw = ZOOM_W + PAD + (ZOOM_COL - px0)
    ph = ZOOM_H + PAD + (ZOOM_ROW - pz0)

    print("Reading masks ...")
    h_raw = read_region(MASKS_DIR / "height.tif", px0, pz0, pw, ph)
    h_norm = h_raw.astype(np.float32) / 65535.0
    wl = read_region(MASKS_DIR / "hydro_lake_wl.tif", px0, pz0, pw, ph).astype(np.float32)
    lake_id_raw = read_region(MASKS_DIR / "hydro_lake.tif", px0, pz0, pw, ph)

    terrain_y_full = height_norm_to_mc_y(h_norm, cfg)
    water_y_full = height_norm_to_mc_y(wl, cfg)

    cr0 = ZOOM_ROW - pz0
    cc0 = ZOOM_COL - px0

    # ── Panel 1: Old morph+blur approach (recreated) ──────────────────────
    print("Panel 1: Morph+blur ...")
    lake_raw = lake_id_raw > 0
    morph_r = 12
    struct = np.ones((morph_r*2+1, morph_r*2+1), dtype=bool)
    shaped = binary_closing(lake_raw, structure=struct)
    shaped = binary_opening(shaped, structure=struct)
    smooth = gaussian_filter(shaped.astype(np.float32), sigma=28.0) > 0.30
    smooth = binary_dilation(smooth, iterations=4)
    morph_mask = smooth[cr0:cr0+ZOOM_H, cc0:cc0+ZOOM_W]
    terrain_crop = terrain_y_full[cr0:cr0+ZOOM_H, cc0:cc0+ZOOM_W]

    panel1 = render_panel(terrain_crop.copy(), morph_mask,
                          "OLD: morph+blur", ZOOM_H, ZOOM_W)

    # ── Panel 2: Terrain intersection (no depression) ─────────────────────
    print("Panel 2: Terrain intersection ...")
    basin = wl > 0
    basin_orig = lake_id_raw > 0
    basin_exp = binary_dilation(basin_orig, iterations=BASIN_EXPAND_PX)
    k = BASIN_EXPAND_PX * 2 + 1
    wl2 = wl.copy()
    wl_exp = maximum_filter(wl2, size=k)
    new_zone = basin_exp & ~basin
    wl2[new_zone] = wl_exp[new_zone]
    basin2 = wl2 > 0

    water_y2 = height_norm_to_mc_y(wl2, cfg)
    uw2 = basin2 & (terrain_y_full < water_y2)
    uw2_crop = uw2[cr0:cr0+ZOOM_H, cc0:cc0+ZOOM_W]

    panel2 = render_panel(terrain_crop.copy(), uw2_crop,
                          "NEW: terrain only", ZOOM_H, ZOOM_W)

    # ── Panel 3: Terrain intersection + basin depression ──────────────────
    print("Panel 3: Terrain + depression ...")
    terrain_depressed = terrain_y_full.copy()
    if basin2.any():
        edt = distance_transform_edt(basin2).astype(np.float32)
        edt_max = max(float(edt.max()), 1.0)
        edt_norm = edt / edt_max
        depression = (edt_norm ** LAKE_DEPRESS_POWER) * LAKE_BASIN_DEPRESS
        terrain_depressed[basin2] -= depression[basin2]

    uw3 = basin2 & (terrain_depressed < water_y2)
    uw3_crop = uw3[cr0:cr0+ZOOM_H, cc0:cc0+ZOOM_W]
    terrain_dep_crop = terrain_depressed[cr0:cr0+ZOOM_H, cc0:cc0+ZOOM_W]

    panel3 = render_panel(terrain_dep_crop, uw3_crop,
                          "NEW: terrain + depress", ZOOM_H, ZOOM_W)

    # ── Panel 4: Approach B — +1 block raise → natural basin contour ─────
    # Raise the water level by 1 MC Y block. The resulting underwater area
    # IS the brown basin ring (terrain < spill+1).  Erode slightly so the
    # shoreline sits just inside, then apply EDT parabolic depth.
    print("Panel 4: +1 block raise + erode + parabolic ...")
    from scipy.ndimage import binary_erosion

    spill_mc = float(height_norm_to_mc_y(
        np.array([wl[wl > 0].max()]), cfg)[0])
    raised_wl_y = spill_mc + 1.0  # +1 MC Y block

    uw4_raw = basin2 & (terrain_y_full < raised_wl_y)
    # Erode by 3px to shrink slightly inward from the brown ring edge
    uw4 = binary_erosion(uw4_raw, iterations=3)
    uw4_crop = uw4[cr0:cr0+ZOOM_H, cc0:cc0+ZOOM_W]

    panel4 = render_panel(terrain_crop.copy(), uw4_crop,
                          "B: +1blk erode3 parabolic", ZOOM_H, ZOOM_W)

    # ── Stitch ────────────────────────────────────────────────────────────
    gap = 4
    panels = [panel1, panel2, panel3, panel4]
    total_w = ZOOM_W * len(panels) + gap * (len(panels) - 1)
    out = Image.new("RGBA", (total_w, ZOOM_H), (30, 30, 30, 255))
    x = 0
    for p in panels:
        out.paste(p, (x, 0))
        x += ZOOM_W + gap

    OUTPUT.parent.mkdir(exist_ok=True)
    out.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"Saved: {OUTPUT}  ({total_w}x{ZOOM_H}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
