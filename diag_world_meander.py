"""
diag_world_meander.py — Apply skeleton-to-spline meander to the full world
at 1:8 scale, then render biome+rivers overlay for review.

Also applies:
- Ocean river cutoff (height < sea_norm)
- Desert treatment (dry wadis in SAND_DUNE_DESERT, preserve specified components)

Works at 1:8 (6250x6250) which matches hydro_centerline.tif resolution.
The meander is applied at this scale, then the result is downsampled for display.
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
                           gaussian_filter, distance_transform_edt, label)
from scipy.interpolate import splprep, splev
from skimage.morphology import skeletonize

MASKS_DIR = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\world_meander_preview.png")

TILE = 512
GRID_N = 97
SCALE = 8  # work at 1:8 like the centerline precompute
DISPLAY_DS = 2  # further downsample for display (1:16 final)

TOTAL_PX = GRID_N * TILE  # 49664
WORK_SZ = TOTAL_PX // SCALE  # 6208

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


# ── Skeleton tracing (same as prototype) ─────────────────────────────

def trace_skeleton_branches(skel, min_len=5):
    h, w = skel.shape
    pts = set(zip(*np.where(skel)))
    if not pts:
        return []

    def neighbors(y, x):
        out = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and skel[ny, nx]:
                    out.append((ny, nx))
        return out

    neighbor_count = np.zeros_like(skel, dtype=np.uint8)
    for y, x in pts:
        neighbor_count[y, x] = len(neighbors(y, x))

    endpoints = {(y, x) for y, x in pts if neighbor_count[y, x] == 1}
    junctions = {(y, x) for y, x in pts if neighbor_count[y, x] >= 3}

    visited = np.zeros_like(skel, dtype=bool)
    branches = []

    def trace_from(sy, sx):
        path = [(sy, sx)]
        visited[sy, sx] = True
        cy, cx = sy, sx
        while True:
            nbrs = [(ny, nx) for ny, nx in neighbors(cy, cx) if not visited[ny, nx]]
            if not nbrs:
                break
            if len(path) >= 2:
                dy = cy - path[-2][0]
                dx = cx - path[-2][1]
                nbrs.sort(key=lambda p: -((p[0]-cy)*dy + (p[1]-cx)*dx))
            ny, nx = nbrs[0]
            path.append((ny, nx))
            visited[ny, nx] = True
            cy, cx = ny, nx
            if (cy, cx) in junctions and len(path) > 1:
                break
        return np.array(path)

    starts = sorted(endpoints) + sorted(junctions)
    for sy, sx in starts:
        if visited[sy, sx]:
            continue
        branch = trace_from(sy, sx)
        if len(branch) >= min_len:
            branches.append(branch)

    for y, x in pts:
        if not visited[y, x]:
            branch = trace_from(y, x)
            if len(branch) >= min_len:
                branches.append(branch)

    return branches


def add_meander_to_branch(points, amplitude=4.0, wavelength=12, seed=0):
    """At 1:8 scale, amplitudes and wavelengths are 1/8 of full res."""
    if len(points) < 4:
        return points

    total_len = 0
    seg_lens = [0.0]
    for i in range(1, len(points)):
        d = np.sqrt((points[i, 0] - points[i-1, 0])**2 +
                     (points[i, 1] - points[i-1, 1])**2)
        total_len += d
        seg_lens.append(total_len)
    seg_lens = np.array(seg_lens)

    if total_len < wavelength:
        return points

    n_ctrl = max(4, int(total_len / wavelength) + 1)
    ctrl_dists = np.linspace(0, total_len, n_ctrl)

    ctrl_pts = []
    for d in ctrl_dists:
        idx = np.searchsorted(seg_lens, d, side='right') - 1
        idx = max(0, min(idx, len(points) - 2))
        frac = (d - seg_lens[idx]) / max(seg_lens[idx+1] - seg_lens[idx], 1e-6)
        frac = np.clip(frac, 0, 1)
        y = points[idx, 0] * (1 - frac) + points[idx+1, 0] * frac
        x = points[idx, 1] * (1 - frac) + points[idx+1, 1] * frac
        ctrl_pts.append([y, x])
    ctrl_pts = np.array(ctrl_pts)

    rng = np.random.RandomState(seed)
    for i in range(1, len(ctrl_pts) - 1):
        ty = ctrl_pts[i+1, 0] - ctrl_pts[i-1, 0]
        tx = ctrl_pts[i+1, 1] - ctrl_pts[i-1, 1]
        tlen = np.sqrt(ty**2 + tx**2)
        if tlen < 1e-6:
            continue
        perp_y = -tx / tlen
        perp_x = ty / tlen

        phase = rng.uniform(-np.pi, np.pi)
        t_frac = i / len(ctrl_pts)
        offset = amplitude * np.sin(2 * np.pi * t_frac * 2.5 + phase)
        offset += rng.uniform(-amplitude * 0.3, amplitude * 0.3)

        ctrl_pts[i, 0] += perp_y * offset
        ctrl_pts[i, 1] += perp_x * offset

    try:
        tck, u = splprep([ctrl_pts[:, 1], ctrl_pts[:, 0]], s=0, k=3)
        n_eval = max(int(total_len * 2), len(points))
        u_new = np.linspace(0, 1, n_eval)
        x_new, y_new = splev(u_new, tck)
        return np.column_stack([y_new, x_new])
    except Exception:
        return points


def rebuild_channel_mask(branches_orig, branches_mean, shape,
                          original_river_mask):
    h, w = shape
    new_mask = np.zeros((h, w), dtype=bool)
    original_dist = distance_transform_edt(original_river_mask).astype(np.float32)

    rng = np.random.RandomState(123)
    width_noise = gaussian_filter(rng.randn(h, w).astype(np.float32), sigma=4)
    width_noise = (width_noise - width_noise.min()) / (width_noise.max() - width_noise.min() + 1e-9)
    width_noise = 0.7 + width_noise * 0.6

    for orig_branch, mean_branch in zip(branches_orig, branches_mean):
        n_orig = len(orig_branch)
        n_mean = len(mean_branch)

        for j in range(n_mean):
            orig_idx = min(int(j * n_orig / n_mean), n_orig - 1)
            oy = max(0, min(int(orig_branch[orig_idx, 0]), h - 1))
            ox = max(0, min(int(orig_branch[orig_idx, 1]), w - 1))

            orig_radius = max(original_dist[oy, ox], 1.0)

            my = max(0, min(int(round(mean_branch[j, 0])), h - 1))
            mx = max(0, min(int(round(mean_branch[j, 1])), w - 1))
            local_r = int(orig_radius * width_noise[my, mx])
            local_r = max(1, local_r)

            y0 = max(0, my - local_r)
            y1 = min(h, my + local_r + 1)
            x0 = max(0, mx - local_r)
            x1 = min(w, mx + local_r + 1)
            yy, xx = np.ogrid[y0-my:y1-my, x0-mx:x1-mx]
            circle = (yy**2 + xx**2) <= local_r**2
            new_mask[y0:y1, x0:x1] |= circle

    return new_mask


def main():
    t0 = time.perf_counter()
    win = Window(0, 0, TOTAL_PX, TOTAL_PX)

    def read_mask(name, resamp=Resampling.average):
        with rasterio.open(str(MASKS_DIR / f"{name}.tif")) as src:
            return src.read(1, window=win, out_shape=(WORK_SZ, WORK_SZ),
                           resampling=resamp)

    print("Reading masks at 1:8...", flush=True)
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

    sea_norm = 17050.0 / 65535.0
    is_sea = height < sea_norm
    desert = (override == 170)

    print(f"  Work size: {WORK_SZ}x{WORK_SZ}", flush=True)
    print(f"  Read done in {time.perf_counter()-t0:.1f}s", flush=True)

    # ── Build river mask (excluding ocean + lakes) ───────────────────
    any_river = (cl > 0) | (order > 0)
    river_land = any_river & ~is_sea  # OCEAN CUTOFF
    lake_basin = lake > 0
    basin_expand = max(1, 32 // SCALE)
    lake_basin_exp = binary_dilation(lake_basin, iterations=basin_expand)
    lake_wl_exp = maxfilt(lake_wl, size=basin_expand * 2 + 1)
    lake_mask = lake_basin_exp & (lake_wl_exp > 0) & (height < lake_wl_exp) & ~is_sea
    river_only = river_land & ~lake_mask

    print(f"  River pixels (land, no lake): {river_only.sum()}", flush=True)

    # ── Desert analysis ──────────────────────────────────────────────
    print("Analyzing desert components...", flush=True)
    river_labels, n_rivers = label(river_only)

    # Desert lakes — preserve all
    lake_in_desert = desert & (lake > 0)
    lake_dilated = binary_dilation(lake_in_desert, iterations=3)
    lake_feeder_labels = set(np.unique(river_labels[lake_dilated & (river_labels > 0)]))

    # High order in desert
    high_order_labels = set(np.unique(river_labels[desert & (order >= 4) & (river_labels > 0)]))

    # User-specified preserves by tile location (at 1:8 scale)
    user_preserve = set()
    user_wadi = set()  # demoted to wadi only
    for lbl in range(1, n_rivers + 1):
        comp = river_labels == lbl
        total = comp.sum()
        if total < 3:
            continue
        in_desert = (comp & desert).sum()
        if in_desert == 0:
            continue
        max_ord = order[comp].max()
        ys, xs = np.where(comp)
        tile_x = int(xs.mean() * SCALE // TILE)
        tile_z = int(ys.mean() * SCALE // TILE)

        # #495 equivalent: order 4, tile ~13,79
        if max_ord >= 4 and 11 <= tile_x <= 15 and 77 <= tile_z <= 82 and total > 500:
            user_preserve.add(lbl)
        # #104 equivalent: order 4, tile ~24,69
        elif max_ord >= 4 and 22 <= tile_x <= 26 and 67 <= tile_z <= 72 and total > 500:
            user_preserve.add(lbl)
        # #1 equivalent: order 3, tile ~19,65
        elif max_ord >= 3 and 17 <= tile_x <= 21 and 63 <= tile_z <= 67 and total > 1000:
            user_preserve.add(lbl)

    preserve_labels = lake_feeder_labels | high_order_labels | user_preserve

    # Build wadi mask: desert rivers not preserved
    preserved_mask = np.zeros_like(desert)
    for lbl in preserve_labels:
        preserved_mask |= (river_labels == lbl)
    wadi_mask = desert & river_only & ~preserved_mask & ~lake_mask

    print(f"  Preserved components: {len(preserve_labels)}")
    print(f"  Wadi pixels: {wadi_mask.sum()}")

    # ── Skeletonize + meander ────────────────────────────────────────
    print("Skeletonizing...", flush=True)
    t1 = time.perf_counter()
    skel = skeletonize(river_only)
    print(f"  Skeleton: {skel.sum()} px ({time.perf_counter()-t1:.1f}s)")

    print("Tracing branches...", flush=True)
    branches = trace_skeleton_branches(skel, min_len=3)
    print(f"  {len(branches)} branches")

    print("Adding meander...", flush=True)
    # At 1:8 scale, amplitudes are 1/8 of full res
    meandered = []
    for i, branch in enumerate(branches):
        max_ord = 1
        for pt in branch[::5]:
            y, x = int(pt[0]), int(pt[1])
            if 0 <= y < WORK_SZ and 0 <= x < WORK_SZ:
                max_ord = max(max_ord, order[y, x])

        # 1:8 scale amplitudes (full_res / 8)
        amp = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6}.get(max_ord, 2)
        wl = {1: 6, 2: 9, 3: 12, 4: 18, 5: 22}.get(max_ord, 10)

        m = add_meander_to_branch(branch, amplitude=amp, wavelength=wl, seed=i * 7)
        meandered.append(m)

    print("Rebuilding channel mask...", flush=True)
    t1 = time.perf_counter()
    new_river = rebuild_channel_mask(branches, meandered, (WORK_SZ, WORK_SZ),
                                      river_only)
    # Remove ocean
    new_river &= ~is_sea
    # Remove lake overlap
    new_river &= ~lake_mask
    print(f"  Rebuild done ({time.perf_counter()-t1:.1f}s)")
    print(f"  New river pixels: {new_river.sum()} (was {river_only.sum()})")

    # Update wadi mask for new river
    wadi_new = desert & new_river & ~preserved_mask & ~lake_mask

    # ══════════════════════════════════════════════════════════════════
    # RENDER — downsample to display resolution
    # ══════════════════════════════════════════════════════════════════
    print("Rendering...", flush=True)
    disp_sz = WORK_SZ // DISPLAY_DS

    def ds(arr, method='nearest'):
        if method == 'nearest':
            return arr[::DISPLAY_DS, ::DISPLAY_DS]
        else:
            # Average downsample for float
            from skimage.measure import block_reduce
            return block_reduce(arr, (DISPLAY_DS, DISPLAY_DS), np.mean)

    d_override = ds(override)
    d_height = ds(height, 'avg')
    d_is_sea = ds(is_sea)
    d_lake_mask = ds(lake_mask)
    d_river_orig = ds(river_only)
    d_river_new = ds(new_river)
    d_wadi_orig = ds(wadi_mask)
    d_wadi_new = ds(wadi_new)
    d_cl = ds(cl)
    d_order = ds(order)

    # Hillshade
    h_smooth = gaussian_filter(d_height, sigma=1.5)
    gy, gx = np.gradient(h_smooth)
    shade = (-gx + gy) / (np.sqrt(gx**2 + gy**2 + 0.001) + 0.001)
    shade = np.clip(shade * 0.3 + 0.5, 0.25, 0.75)

    color_lut = _build_color_lut()

    def make_panel(river_mask, wadi_m):
        comp = color_lut[d_override].copy()
        comp = np.clip(comp.astype(np.float32) * shade[..., None] * 1.6, 0, 255).astype(np.uint8)
        comp[d_is_sea] = (30, 80, 160)
        comp[d_lake_mask] = (50, 110, 190)

        # Dry wadis
        if wadi_m is not None:
            rng = np.random.RandomState(42)
            wn = gaussian_filter(rng.random(wadi_m.shape).astype(np.float32), sigma=3)
            wn = (wn - wn.min()) / (wn.max() - wn.min() + 1e-9)
            wr = np.where(wn > 0.5, 170, 190).astype(np.float32)
            wg = np.where(wn > 0.5, 145, 160).astype(np.float32)
            wb = np.where(wn > 0.5, 90, 110).astype(np.float32)
            ws = np.clip(shade * 1.0, 0.55, 0.95)
            comp[wadi_m, 0] = np.clip(wr[wadi_m] * ws[wadi_m], 0, 255).astype(np.uint8)
            comp[wadi_m, 1] = np.clip(wg[wadi_m] * ws[wadi_m], 0, 255).astype(np.uint8)
            comp[wadi_m, 2] = np.clip(wb[wadi_m] * ws[wadi_m], 0, 255).astype(np.uint8)

        water = river_mask & ~d_lake_mask & ~d_is_sea
        if wadi_m is not None:
            water = water & ~wadi_m
        comp[water] = (65, 130, 215)

        return comp

    before = make_panel(d_river_orig, None)
    after = make_panel(d_river_new, d_wadi_new)

    # Side by side
    gap = 12
    canvas_w = disp_sz * 2 + gap
    canvas_h = disp_sz + 50
    canvas = np.full((canvas_h, canvas_w, 3), 20, dtype=np.uint8)
    y_off = 40
    canvas[y_off:y_off + disp_sz, :disp_sz] = before
    canvas[y_off:y_off + disp_sz, disp_sz + gap:] = after

    img = Image.fromarray(canvas, "RGB")
    draw = ImageDraw.Draw(img)

    draw.text((disp_sz // 2 - 40, 5), "BEFORE", fill=(255, 255, 255))
    draw.text((disp_sz + gap + disp_sz // 2 - 40, 5), "AFTER", fill=(255, 255, 255))
    draw.text((disp_sz // 2 - 100, 20), "(original geometric rivers, ocean rivers)",
              fill=(180, 180, 180))
    draw.text((disp_sz + gap + disp_sz // 2 - 120, 20),
              "(meander + ocean cut + desert wadis)",
              fill=(180, 180, 180))

    # Tile grid (sparse)
    tile_px = TILE // SCALE // DISPLAY_DS
    for panel_x_off in [0, disp_sz + gap]:
        for i in range(0, GRID_N + 1, 4):
            x = panel_x_off + i * tile_px
            if x < panel_x_off + disp_sz:
                draw.line([(x, y_off), (x, y_off + disp_sz - 1)], fill=(40, 40, 40), width=1)
        for j in range(0, GRID_N + 1, 4):
            y = y_off + j * tile_px
            if y < y_off + disp_sz:
                draw.line([(panel_x_off, y), (panel_x_off + disp_sz - 1, y)],
                          fill=(40, 40, 40), width=1)

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT))
    elapsed = time.perf_counter() - t0
    print(f"\nSaved: {OUTPUT}  ({canvas_w}x{canvas_h}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
