"""
diag_flow_threshold.py — Compare binary-mask vs flow-threshold river boundaries.
Shows how flow-threshold matches lake boundary smoothness.
"""
from __future__ import annotations
import time
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window
from PIL import Image, ImageDraw
from scipy.ndimage import (binary_dilation, maximum_filter as maxfilt,
                           gaussian_filter)

MASKS_DIR = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\flow_threshold_compare.png")

TILE = 512
TX, TZ = 19, 75
PAD = 2
TX_MIN, TX_MAX = TX - PAD, TX + PAD + 1
TZ_MIN, TZ_MAX = TZ - PAD, TZ + PAD + 1
DS = 1

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


def make_panel(override, shade, is_sea, lake_mask, river_mask, label):
    color_lut = _build_color_lut()
    comp = color_lut[override].copy()
    comp = np.clip(comp.astype(np.float32) * shade[..., None] * 1.6, 0, 255).astype(np.uint8)
    comp[is_sea] = (30, 80, 160)
    comp[lake_mask] = (50, 110, 190)
    water = river_mask & ~lake_mask & ~is_sea
    comp[water] = (65, 130, 215)
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

    print(f"Reading masks for tile ({TX},{TZ}) +/- {PAD}...", flush=True)
    override = read_mask("override", Resampling.nearest).astype(np.uint8)
    height_raw = read_mask("height")
    height = height_raw.astype(np.float32) / (65535.0 if height_raw.dtype == np.uint16 else 1.0)
    cl = read_mask("hydro_centerline", Resampling.nearest).astype(np.uint8)
    lake = read_mask("hydro_lake", Resampling.nearest)
    lake_wl_raw = read_mask("hydro_lake_wl", Resampling.nearest)
    lake_wl = lake_wl_raw.astype(np.float32)
    if lake_wl_raw.dtype == np.uint16:
        lake_wl = lake_wl / 65535.0
    flow_raw = read_mask("flow", Resampling.average)
    flow = flow_raw.astype(np.float32)
    if flow_raw.dtype == np.uint16:
        flow = flow / 65535.0

    sea_norm = 17050.0 / 65535.0
    is_sea = height < sea_norm

    h_smooth = gaussian_filter(height, sigma=2.0)
    gy, gx = np.gradient(h_smooth)
    shade = (-gx + gy) / (np.sqrt(gx**2 + gy**2 + 0.001) + 0.001)
    shade = np.clip(shade * 0.3 + 0.5, 0.25, 0.75)

    # Lakes — terrain intersection (same for both panels)
    lake_basin = lake > 0
    basin_expand = max(1, 32 // DS)
    lake_basin_exp = binary_dilation(lake_basin, iterations=basin_expand)
    lake_wl_exp = maxfilt(lake_wl, size=basin_expand * 2 + 1)
    lake_mask = lake_basin_exp & (lake_wl_exp > 0) & (height < lake_wl_exp) & ~is_sea

    # ── Panel 1: OLD — binary mask from precomputed centerline ──
    old_river = (cl > 0) & ~is_sea & ~lake_mask
    print(f"Old river: {old_river.sum()} px")

    # ── Panel 2: NEW — spline-rasterized at 50k ──
    # Load spline data and rasterize directly at full resolution
    import pickle
    from scipy.interpolate import splev

    spline_path = MASKS_DIR / "river_splines.pkl"
    new_river = np.zeros((out_h, out_w), dtype=bool)

    if spline_path.exists():
        with open(spline_path, "rb") as f:
            spline_bundle = pickle.load(f)
        SCALE = spline_bundle.get("scale", 8)
        branches_data = spline_bundle.get("branches", [])
        print(f"Loaded {len(branches_data)} splines")

        # Bounding box in 1:8 coords
        y0_18 = TZ_MIN * TILE / SCALE
        y1_18 = TZ_MAX * TILE / SCALE
        x0_18 = TX_MIN * TILE / SCALE
        x1_18 = TX_MAX * TILE / SCALE

        for spl in branches_data:
            tck = spl.get("tck")
            if tck is None:
                continue
            widths = spl.get("widths_18", [])

            # Quick bbox check
            u_ck = np.linspace(0, 1, 20)
            xc, yc = splev(u_ck, tck)
            if (yc.max() < y0_18 - 10 or yc.min() > y1_18 + 10 or
                xc.max() < x0_18 - 10 or xc.min() > x1_18 + 10):
                continue

            # Dense evaluation
            dx = np.diff(xc * SCALE); dy = np.diff(yc * SCALE)
            arc = np.sum(np.sqrt(dx**2 + dy**2))
            n_pts = max(int(arc * 1.5), 100)
            u_d = np.linspace(0, 1, n_pts)
            sx, sy = splev(u_d, tck)

            # Local coordinates
            px_x = (sx * SCALE - col0) / DS
            px_y = (sy * SCALE - row0) / DS

            # Width interpolation
            if len(widths) > 1:
                w_idx = np.linspace(0, len(widths) - 1, n_pts)
                radii = np.interp(w_idx, np.arange(len(widths)),
                                  np.array(widths, dtype=np.float32))
            else:
                radii = np.full(n_pts, max(widths[0] if widths else 1.0, 1.0))
            radii = radii * SCALE / DS

            for k in range(0, n_pts, max(1, DS)):
                cx, cy, r = px_x[k], px_y[k], max(radii[k], 1.0)
                if cx + r < 0 or cx - r >= out_w or cy + r < 0 or cy - r >= out_h:
                    continue
                ir = int(np.ceil(r))
                iy0 = max(0, int(cy) - ir); iy1 = min(out_h, int(cy) + ir + 1)
                ix0 = max(0, int(cx) - ir); ix1 = min(out_w, int(cx) + ir + 1)
                if iy1 <= iy0 or ix1 <= ix0:
                    continue
                yy = np.arange(iy0, iy1, dtype=np.float32) - cy
                xx = np.arange(ix0, ix1, dtype=np.float32) - cx
                dyy, dxx = np.meshgrid(yy, xx, indexing='ij')
                new_river[iy0:iy1, ix0:ix1] |= (dyy**2 + dxx**2) <= r**2

    # Braid fill: gaussian smooth to eliminate 8x8 staircase
    braid_mask = cl == 255
    if braid_mask.any():
        braid_smooth = gaussian_filter(braid_mask.astype(np.float32), sigma=5.0)
        braid_water = braid_smooth > 0.5
        new_river |= braid_water
    new_river &= ~is_sea & ~lake_mask
    print(f"New river (spline+terrain): {new_river.sum()} px")

    # Render panels
    print("Rendering comparison...", flush=True)
    panel_old = make_panel(override, shade, is_sea, lake_mask, old_river, "OLD")
    panel_new = make_panel(override, shade, is_sea, lake_mask, new_river, "NEW")

    # Side-by-side canvas
    gap = 4
    canvas_w = out_w * 2 + gap
    canvas_h = out_h + 40
    canvas = np.full((canvas_h, canvas_w, 3), 20, dtype=np.uint8)

    y_off = 40
    canvas[y_off:y_off + out_h, :out_w] = panel_old
    canvas[y_off:y_off + out_h, out_w + gap:] = panel_new

    img = Image.fromarray(canvas, "RGB")
    draw = ImageDraw.Draw(img)

    draw.text((out_w // 2 - 100, 5), "OLD: Binary Mask (8x NEAREST)", fill=(255, 100, 100))
    draw.text((out_w // 2 - 100, 20), "blocky staircase edges", fill=(180, 120, 120))
    draw.text((out_w + gap + out_w // 2 - 120, 5),
              "NEW: Spline-Rasterized (50k native)", fill=(100, 255, 100))
    draw.text((out_w + gap + out_w // 2 - 120, 20),
              "smooth curves from splev() at target res", fill=(120, 180, 120))

    # Tile grid
    tile_px = TILE // DS
    for panel_x_off in [0, out_w + gap]:
        for i in range(TX_MAX - TX_MIN + 1):
            x = panel_x_off + i * tile_px
            draw.line([(x, y_off), (x, y_off + out_h - 1)], fill=(60, 60, 60), width=1)
        for j in range(TZ_MAX - TZ_MIN + 1):
            y = y_off + j * tile_px
            draw.line([(panel_x_off, y), (panel_x_off + out_w - 1, y)],
                      fill=(60, 60, 60), width=1)

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"\nSaved: {OUTPUT}  ({canvas_w}x{canvas_h}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
