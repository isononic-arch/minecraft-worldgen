"""
diag_desert_rivers_final.py — Final preview of desert river treatment.

Rules:
- ONLY modify rivers within SAND_DUNE_DESERT (zone 170). Leave all other biomes untouched.
- Preserve: #495 (order 4, west), #104 (order 4, NE), #1 (order 3, north),
  #394 (lake feeder), #503 (lake feeder), all desert lakes
- Kill entirely: #621 (south desert), #140 (north desert)
- All other rivers in sand dune desert → dry wadi
- River boundary roughening: low-frequency noise perturbation on edges (~8px),
  centerline preserved, only affects visual boundary. Applied GLOBALLY to all rivers.
- Ocean rivers removed everywhere.
"""
from __future__ import annotations
import sys, time
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window
from PIL import Image, ImageDraw
from scipy.ndimage import (binary_dilation, binary_erosion, maximum_filter as maxfilt,
                           label, gaussian_filter, distance_transform_edt)

MASKS_DIR = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\desert_rivers_final.png")

TILE = 512
DS = 4  # high res

# SW continent
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


def roughen_river_edges(river_mask, seed=42, noise_scale=40.0, max_perturbation=6,
                         edge_band=8):
    """
    Perturb river boundary pixels using low-frequency noise.

    - Finds pixels within `edge_band` of the river edge
    - Uses low-frequency simplex-like noise (gaussian-smoothed random field)
      to push the boundary in or out by up to `max_perturbation` pixels
    - Centerline (interior) stays untouched
    - No land-within-water: only erodes edges, never creates islands
    """
    h, w = river_mask.shape

    # Distance from edge (inside the river only)
    interior_dist = distance_transform_edt(river_mask)

    # Generate low-frequency noise field
    rng = np.random.RandomState(seed)
    noise_raw = rng.randn(h, w).astype(np.float32)
    # Smooth heavily for low-frequency, erosion-like shapes
    noise_smooth = gaussian_filter(noise_raw, sigma=noise_scale)
    # Normalize to [0, 1]
    nmin, nmax = noise_smooth.min(), noise_smooth.max()
    if nmax > nmin:
        noise_smooth = (noise_smooth - nmin) / (nmax - nmin)
    else:
        noise_smooth[:] = 0.5

    # Erosion-only approach: selectively remove edge pixels based on noise.
    # A pixel is removed if it's within the edge band AND noise says so.
    # Deeper interior pixels need higher noise to be removed (graduated).
    # Never expand beyond original mask. Never touch deep interior.
    roughened = river_mask.copy()

    # Edge band: pixels inside the river within edge_band of the boundary
    edge_pixels = river_mask & (interior_dist <= edge_band) & (interior_dist > 0)

    # Removal threshold: scales with depth into river.
    # At dist=1 (outermost): removed if noise < 0.6 (40% chance of removal)
    # At dist=edge_band: removed if noise < 0.05 (rarely removed)
    # This creates graduated nibbling — more erosion at edges, less deeper in
    depth_frac = interior_dist[edge_pixels] / edge_band  # 0..1 (0=edge, 1=deep)
    removal_threshold = 0.55 * (1.0 - depth_frac ** 0.7)  # high at edge, near-zero deep

    roughened[edge_pixels] = noise_smooth[edge_pixels] >= removal_threshold

    # Safety: centerline preserved — anything deeper than edge_band is untouched
    deep_interior = interior_dist > edge_band
    roughened[deep_interior] = True

    return roughened


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
    river_f = river_raw.astype(np.float32) / (65535.0 if river_raw.dtype == np.uint16 else 1.0)

    sea_norm = 17050.0 / 65535.0
    is_sea = height < sea_norm
    desert = (override == 170)

    # ── River component analysis ─────────────────────────────────────
    print("Analyzing river components...", flush=True)
    any_river = (cl > 0) | (order > 0) | (river_f > 0.15)
    river_land = any_river & ~is_sea
    river_labels, n_rivers = label(river_land)

    # Desert lakes
    lake_in_desert = desert & (lake > 0)
    all_desert_lakes = lake_in_desert
    lake_dilated = binary_dilation(all_desert_lakes, iterations=5)
    touching_labels = set(np.unique(river_labels[lake_dilated & (river_labels > 0)]))

    # High-order in desert
    high_order_desert = desert & (order >= 4) & (river_labels > 0)
    high_order_labels = set(np.unique(river_labels[high_order_desert]))

    # ── User specifications ──────────────────────────────────────────
    # Preserve these specific components (keep as water):
    #   #495 (order 4, west), #104 (order 4, NE), #1 (order 3, north)
    #   + all lake feeders + all high-order
    # Kill entirely (no channel at all):
    #   #621, #140
    # Everything else in sand dune desert → dry wadi

    # Find component IDs at this resolution (they may differ from DS=4 vs DS=8)
    # Re-identify by matching properties
    user_preserve = set()
    user_kill = set()

    # Match components by tile location and size
    for lbl in range(1, n_rivers + 1):
        comp = river_labels == lbl
        total = comp.sum()
        if total < 50:
            continue
        in_desert = (comp & desert).sum()
        if in_desert == 0:
            continue
        max_ord = order[comp].max()
        ys, xs = np.where(comp)
        tile_x = int(xs.mean() * DS // TILE) + TX_MIN
        tile_z = int(ys.mean() * DS // TILE) + TZ_MIN

        # #495: order 4, tile ~13,79 — large west river
        if max_ord >= 4 and 11 <= tile_x <= 15 and 77 <= tile_z <= 82 and total > 10000:
            user_preserve.add(lbl)
            print(f"  Preserve #{lbl}: order {max_ord}, tile ({tile_x},{tile_z}), {total}px [~#495]")
        # #104: order 4, tile ~24,69 — NE desert
        elif max_ord >= 4 and 22 <= tile_x <= 26 and 67 <= tile_z <= 72 and total > 10000:
            user_preserve.add(lbl)
            print(f"  Preserve #{lbl}: order {max_ord}, tile ({tile_x},{tile_z}), {total}px [~#104]")
        # #1: order 3, tile ~19,65 — north desert/steppe
        elif max_ord >= 3 and 17 <= tile_x <= 21 and 63 <= tile_z <= 67 and total > 20000:
            user_preserve.add(lbl)
            print(f"  Preserve #{lbl}: order {max_ord}, tile ({tile_x},{tile_z}), {total}px [~#1]")
        # #621: order 3, tile ~17,84 — south desert (KILL)
        elif max_ord >= 3 and 15 <= tile_x <= 19 and 82 <= tile_z <= 87 and total > 10000:
            user_kill.add(lbl)
            print(f"  Kill #{lbl}: order {max_ord}, tile ({tile_x},{tile_z}), {total}px [~#621]")
        # #140: order 2, tile ~22,67 — north desert (KILL)
        elif max_ord <= 2 and 20 <= tile_x <= 24 and 65 <= tile_z <= 69 and total > 3000:
            user_kill.add(lbl)
            print(f"  Kill #{lbl}: order {max_ord}, tile ({tile_x},{tile_z}), {total}px [~#140]")

    preserve_labels = touching_labels | high_order_labels | user_preserve

    # NO rivers are fully killed — everything not preserved becomes dry wadi
    # user_kill components (#621, #140) become wadis, not deleted
    kill_labels = set()  # nothing killed
    print(f"\n  Total preserved: {len(preserve_labels)}")
    print(f"  Total wadi (incl #621, #140, all small): everything else in desert")

    killed_mask = np.zeros_like(desert)  # nothing killed

    preserved_mask = np.zeros_like(desert)
    for lbl in preserve_labels:
        preserved_mask |= (river_labels == lbl)

    # Dry wadi = in desert, not preserved, not killed
    wadi_mask = desert & river_land & ~preserved_mask & ~killed_mask & ~(lake > 0)

    print(f"  Killed pixels in desert: {(killed_mask & desert).sum()}")
    print(f"  Wadi pixels: {wadi_mask.sum()}")
    print(f"  Preserved water in desert: {(preserved_mask & desert & river_land).sum()}")

    # ── Roughen river edges globally ─────────────────────────────────
    print("Roughening river edges...", flush=True)

    # Build the full river mask (all water pixels that will be rendered)
    # This includes all non-killed, non-ocean rivers
    render_river = river_land & ~killed_mask & ~is_sea
    # Don't roughen lake edges
    lake_basin = lake > 0
    basin_expand = max(1, 32 // DS)
    lake_basin_exp = binary_dilation(lake_basin, iterations=basin_expand)
    lake_wl_exp = maxfilt(lake_wl, size=basin_expand * 2 + 1)
    lake_mask = lake_basin_exp & (lake_wl_exp > 0) & (height < lake_wl_exp) & ~is_sea

    # Only roughen river pixels, not lake pixels
    river_only_mask = render_river & ~lake_mask
    roughened = roughen_river_edges(river_only_mask, seed=42,
                                     noise_scale=35.0, max_perturbation=5,
                                     edge_band=8)

    # ══════════════════════════════════════════════════════════════════
    # RENDER BEFORE/AFTER
    # ══════════════════════════════════════════════════════════════════
    print("Rendering...", flush=True)

    color_lut = _build_color_lut()

    # Hillshade
    h_smooth = gaussian_filter(height, sigma=2.0)
    gy, gx = np.gradient(h_smooth)
    shade = (-gx + gy) / (np.sqrt(gx**2 + gy**2 + 0.001) + 0.001)
    shade = np.clip(shade * 0.3 + 0.5, 0.25, 0.75)

    def make_panel(use_roughened, apply_desert_treatment):
        comp = color_lut[override].copy()
        comp = np.clip(comp.astype(np.float32) * shade[..., None] * 1.6, 0, 255).astype(np.uint8)
        comp[is_sea] = (30, 80, 160)

        # Lakes
        comp[lake_mask] = (50, 110, 190)

        if apply_desert_treatment:
            # Dry wadis — sand/sandstone mix with noise
            rng = np.random.RandomState(42)
            wadi_noise = rng.random(wadi_mask.shape).astype(np.float32)
            wadi_noise = gaussian_filter(wadi_noise, sigma=3.0)  # low-freq noise
            wadi_noise = (wadi_noise - wadi_noise.min()) / (wadi_noise.max() - wadi_noise.min())

            # Darker sandstone/sand mix — needs to contrast against desert sand (230,200,120)
            wadi_r = np.where(wadi_noise > 0.5, 170, 190).astype(np.float32)
            wadi_g = np.where(wadi_noise > 0.5, 145, 160).astype(np.float32)
            wadi_b = np.where(wadi_noise > 0.5, 90, 110).astype(np.float32)
            wadi_shade = np.clip(shade * 1.0, 0.55, 0.95)

            comp[wadi_mask, 0] = np.clip(wadi_r[wadi_mask] * wadi_shade[wadi_mask], 0, 255).astype(np.uint8)
            comp[wadi_mask, 1] = np.clip(wadi_g[wadi_mask] * wadi_shade[wadi_mask], 0, 255).astype(np.uint8)
            comp[wadi_mask, 2] = np.clip(wadi_b[wadi_mask] * wadi_shade[wadi_mask], 0, 255).astype(np.uint8)

            # River pixels: use roughened or original
            if use_roughened:
                water_px = roughened & ~lake_mask & ~is_sea & ~wadi_mask
            else:
                water_px = river_only_mask & ~wadi_mask

            # Don't paint killed rivers at all
            water_px = water_px & ~killed_mask
        else:
            # Before: all rivers as water, including ocean
            water_px = river_land & ~lake_mask
            if use_roughened:
                water_px = roughened & ~lake_mask & ~is_sea

        # Paint water
        braid = (cl == 255)
        thin = (cl > 0) & (cl < 255)

        comp[water_px & braid] = (55, 115, 195)
        comp[water_px & thin] = (65, 130, 215)
        comp[water_px & (order > 0) & ~braid & ~thin] = (80, 140, 200)
        comp[water_px & ~braid & ~thin & (order == 0)] = (100, 150, 195)

        return comp

    before = make_panel(use_roughened=False, apply_desert_treatment=False)
    after = make_panel(use_roughened=True, apply_desert_treatment=True)

    # ── Compose side-by-side ─────────────────────────────────────────
    gap = 16
    canvas_w = out_w * 2 + gap
    canvas_h = out_h + 60
    canvas = np.full((canvas_h, canvas_w, 3), 20, dtype=np.uint8)

    y_off = 40
    canvas[y_off:y_off + out_h, :out_w] = before
    canvas[y_off:y_off + out_h, out_w + gap:] = after

    img = Image.fromarray(canvas, "RGB")
    draw = ImageDraw.Draw(img)

    draw.text((out_w // 2 - 30, 8), "BEFORE", fill=(255, 255, 255))
    draw.text((out_w + gap + out_w // 2 - 30, 8), "AFTER", fill=(255, 255, 255))
    draw.text((out_w // 2 - 100, 22), "(current: all rivers water, geometric edges)",
              fill=(180, 180, 180))
    draw.text((out_w + gap + out_w // 2 - 120, 22),
              "(desert treatment + roughened edges + ocean cut)",
              fill=(180, 180, 180))

    # Tile grid
    tile_px = TILE // DS
    for panel_x_off in [0, out_w + gap]:
        for i in range(TX_MAX - TX_MIN + 1):
            x = panel_x_off + i * tile_px
            draw.line([(x, y_off), (x, y_off + out_h - 1)], fill=(40, 40, 40), width=1)
        for j in range(TZ_MAX - TZ_MIN + 1):
            y = y_off + j * tile_px
            draw.line([(panel_x_off, y), (panel_x_off + out_w - 1, y)],
                      fill=(40, 40, 40), width=1)

    # Legend
    legend_y = y_off + out_h + 5
    x = 10
    for lbl, color in [("Preserved water", (65, 130, 215)), ("Braid/lake", (50, 110, 190)),
                        ("Dry wadi", (194, 175, 125)), ("Killed", (230, 200, 120)),
                        ("Desert", (230, 200, 120)), ("Ocean", (30, 80, 160))]:
        draw.rectangle([(x, legend_y), (x + 12, legend_y + 12)], fill=color)
        draw.text((x + 16, legend_y), lbl, fill=(200, 200, 200))
        x += 120

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT), quality=95)
    elapsed = time.perf_counter() - t0
    print(f"\nSaved: {OUTPUT}  ({canvas_w}x{canvas_h}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
