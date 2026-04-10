"""
diag_desert_rivers_south.py — Zoomed view of southern desert rivers.

Labels each river component with its ID, size, max order, and whether
it's currently marked as preserved (lake feeder / high order).
Helps identify which rivers to cull.
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
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\desert_rivers_south_labeled.png")

TILE = 512
DS = 4  # high res for detail

# Full SW continent — generous bounds
TX_MIN, TX_MAX = 6, 32
TZ_MIN, TZ_MAX = 60, 92

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

    print("Reading masks at 1:4...", flush=True)
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

    print("Analyzing...", flush=True)
    desert = (override == 170)
    desert_steppe = (override == 190)
    semi_arid = (override == 200)
    arid_biomes = desert | desert_steppe | semi_arid

    any_river = (cl > 0) | (order > 0) | (river > 0.15)

    # Label river components on land
    river_land = any_river & ~is_sea
    river_labels, n_rivers = label(river_land)

    # Desert lakes
    lake_in_desert = desert & (lake > 0)
    all_desert_lakes = lake_in_desert
    lake_dilated = binary_dilation(all_desert_lakes, iterations=5)

    # Lake feeder components
    touching_labels = set(np.unique(river_labels[lake_dilated & (river_labels > 0)]))

    # High-order components in desert
    high_order_desert = desert & (order >= 4) & (river_labels > 0)
    high_order_labels = set(np.unique(river_labels[high_order_desert]))

    preserve_labels = touching_labels | high_order_labels

    # Find all river components that have pixels in arid biomes
    desert_component_ids = set(np.unique(river_labels[arid_biomes & (river_labels > 0)]))
    print(f"River components with pixels in arid biomes: {len(desert_component_ids)}")

    # Build component info
    comp_info = []
    for lbl in desert_component_ids:
        comp = river_labels == lbl
        total = comp.sum()
        if total < 5:
            continue  # skip tiny fragments
        in_desert = (comp & desert).sum()
        in_steppe = (comp & desert_steppe).sum()
        in_semi = (comp & semi_arid).sum()
        in_arid = (comp & arid_biomes).sum()
        max_ord = order[comp].max()
        ys, xs = np.where(comp)
        cy, cx = ys.mean(), xs.mean()
        is_preserved = lbl in preserve_labels
        reason = []
        if lbl in touching_labels:
            reason.append("lake")
        if lbl in high_order_labels:
            reason.append("ord4+")
        comp_info.append({
            'lbl': lbl, 'total': total, 'in_desert': in_desert,
            'in_steppe': in_steppe, 'in_semi': in_semi, 'in_arid': in_arid,
            'max_ord': max_ord, 'cx': cx, 'cy': cy,
            'preserved': is_preserved, 'reason': ','.join(reason),
            'tile_x': int(cx * DS // TILE) + TX_MIN,
            'tile_z': int(cy * DS // TILE) + TZ_MIN,
        })

    comp_info.sort(key=lambda c: -c['total'])

    print(f"\nTop river components in arid biomes (>= 5px):")
    print(f"{'ID':>6} {'Total':>6} {'Desert':>6} {'Steppe':>6} {'SemiAr':>6} "
          f"{'MaxOrd':>6} {'Tile':>10} {'Preserved':>10} {'Reason':>10}")
    for c in comp_info[:30]:
        print(f"{c['lbl']:>6} {c['total']:>6} {c['in_desert']:>6} {c['in_steppe']:>6} "
              f"{c['in_semi']:>6} {c['max_ord']:>6} "
              f"({c['tile_x']:>2},{c['tile_z']:>2}) "
              f"{'YES' if c['preserved'] else 'no':>10} {c['reason']:>10}")

    # ── Render ───────────────────────────────────────────────────────
    print("\nRendering...", flush=True)
    color_lut = _build_color_lut()
    comp_img = color_lut[override]

    # Hillshade
    h_smooth = gaussian_filter(height, sigma=2.0)
    gy, gx = np.gradient(h_smooth)
    shade = (-gx + gy) / (np.sqrt(gx**2 + gy**2 + 0.001) + 0.001)
    shade = np.clip(shade * 0.3 + 0.5, 0.25, 0.75)
    comp_img = np.clip(comp_img.astype(np.float32) * shade[..., None] * 1.6, 0, 255).astype(np.uint8)
    comp_img[is_sea] = (30, 80, 160)

    # Lakes
    lake_basin = lake > 0
    basin_expand = max(1, 32 // DS)
    lake_basin_exp = binary_dilation(lake_basin, iterations=basin_expand)
    lake_wl_exp = maxfilt(lake_wl, size=basin_expand * 2 + 1)
    lake_mask = lake_basin_exp & (lake_wl_exp > 0) & (height < lake_wl_exp) & ~is_sea
    comp_img[lake_mask] = (50, 110, 190)

    # Color rivers by status
    # Preserved = bright blue, wadi candidates = orange/tan, small fragments = dim
    for c in comp_info:
        mask = (river_labels == c['lbl']) & ~lake_mask & ~is_sea
        if c['preserved']:
            comp_img[mask] = (65, 130, 215)  # blue = preserved water
        else:
            comp_img[mask] = (210, 140, 60)  # orange = wadi candidate (easy to see)

    # Desert boundary outline (subtle)
    from scipy.ndimage import binary_erosion
    desert_edge = desert & ~binary_erosion(desert, iterations=1)
    comp_img[desert_edge] = np.clip(
        comp_img[desert_edge].astype(np.int16) - 30, 0, 255).astype(np.uint8)

    img = Image.fromarray(comp_img, "RGB")
    draw = ImageDraw.Draw(img)

    # Tile grid
    tile_px = TILE // DS
    for i in range(TX_MAX - TX_MIN + 1):
        x = i * tile_px
        draw.line([(x, 0), (x, out_h - 1)], fill=(50, 50, 50), width=1)
    for j in range(TZ_MAX - TZ_MIN + 1):
        y = j * tile_px
        draw.line([(0, y), (out_w - 1, y)], fill=(50, 50, 50), width=1)

    # Label tile coords (every 2)
    for i in range(TX_MAX - TX_MIN):
        for j in range(TZ_MAX - TZ_MIN):
            tx = TX_MIN + i
            tz = TZ_MIN + j
            if tx % 2 == 0 and tz % 2 == 0:
                draw.text((i * tile_px + 2, j * tile_px + 2),
                          f"{tx},{tz}", fill=(160, 160, 160))

    # Label river components on the map
    for c in comp_info:
        if c['total'] < 20:
            continue
        x = int(c['cx'])
        y = int(c['cy'])
        color = (100, 180, 255) if c['preserved'] else (255, 180, 80)
        txt = f"#{c['lbl']} o{c['max_ord']} {c['total']}px"
        if c['preserved']:
            txt += f" [{c['reason']}]"
        # Background for readability
        draw.rectangle([(x - 2, y - 2), (x + len(txt) * 6 + 2, y + 10)],
                       fill=(20, 20, 20, 180))
        draw.text((x, y), txt, fill=color)

    # Legend
    draw.rectangle([(0, out_h - 25), (out_w, out_h)], fill=(20, 20, 20))
    x = 10
    ly = out_h - 20
    for lbl, col in [("Preserved (water)", (65, 130, 215)),
                      ("Wadi candidate", (210, 140, 60)),
                      ("Lake", (50, 110, 190)),
                      ("Desert boundary", (200, 170, 90))]:
        draw.rectangle([(x, ly), (x + 12, ly + 12)], fill=col)
        draw.text((x + 16, ly), lbl, fill=(200, 200, 200))
        x += 160

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"\nSaved: {OUTPUT}  ({out_w}x{out_h}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
