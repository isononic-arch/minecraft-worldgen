"""
diag_meander_proto.py — Skeleton-to-spline meander prototype on tile (16,73).

Steps:
1. Skeletonize river mask to get 1px centerline
2. Trace skeleton branches as ordered point sequences
3. Subsample to control points every ~50px
4. Offset control points perpendicular to flow direction (random, scaled by order)
5. Fit cubic spline through displaced points
6. Rebuild channel mask by dilating spline to width (with noise variation)
7. Braid fills: irregular ponds between channels instead of morphological boxes

Before/after comparison.
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
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\meander_proto_16_73.png")

TILE = 512
DS = 1

TX, TZ = 16, 73
PAD = 2
TX_MIN, TX_MAX = TX - PAD, TX + PAD + 1
TZ_MIN, TZ_MAX = TZ - PAD, TZ + PAD + 1

BIOME_COLORS = {
    0:   ( 30,  80, 160), 10:  (180, 200, 140), 20:  ( 30, 120,  60),
    30:  ( 60, 130,  90), 35:  (180, 200, 220), 40:  (140, 180, 100),
    50:  (220, 230, 240), 55:  (240, 245, 255), 60:  ( 80, 160,  80),
    70:  ( 20, 160,  80), 90:  (190, 160,  80), 100: (180, 170, 150),
    110: (160, 200, 140), 115: (120, 180, 130), 120: ( 60, 140,  70),
    130: (200, 180, 100), 140: (140, 160, 100), 150: (180, 160, 120),
    160: ( 20, 140,  80), 170: (230, 200, 120), 190: (210, 185, 120),
    200: (200, 170, 110), 210: (170, 160, 100), 220: ( 40, 150, 100),
    230: ( 50, 140,  90), 240: ( 80, 150, 130),
}

def _build_color_lut():
    lut = np.full((256, 3), (128, 128, 128), dtype=np.uint8)
    for code, rgb in BIOME_COLORS.items():
        lut[code] = rgb
    return lut


# ── Skeleton tracing ─────────────────────────────────────────────────

def trace_skeleton_branches(skel):
    """
    Trace a skeleton image into ordered point sequences (branches).
    Returns list of arrays, each (N, 2) with [y, x] coordinates.
    """
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

    # Find endpoints (1 neighbor) and junctions (3+ neighbors)
    neighbor_count = np.zeros_like(skel, dtype=np.uint8)
    for y, x in pts:
        neighbor_count[y, x] = len(neighbors(y, x))

    endpoints = {(y, x) for y, x in pts if neighbor_count[y, x] == 1}
    junctions = {(y, x) for y, x in pts if neighbor_count[y, x] >= 3}

    visited = np.zeros_like(skel, dtype=bool)
    branches = []

    def trace_from(start_y, start_x):
        """Trace a single branch from a starting point."""
        path = [(start_y, start_x)]
        visited[start_y, start_x] = True
        cy, cx = start_y, start_x
        while True:
            nbrs = [(ny, nx) for ny, nx in neighbors(cy, cx)
                    if not visited[ny, nx]]
            if not nbrs:
                break
            # Pick the neighbor closest to continuing in same direction
            if len(path) >= 2:
                dy = cy - path[-2][0]
                dx = cx - path[-2][1]
                # Sort by alignment with current direction
                nbrs.sort(key=lambda p: -((p[0]-cy)*dy + (p[1]-cx)*dx))
            ny, nx = nbrs[0]
            path.append((ny, nx))
            visited[ny, nx] = True
            cy, cx = ny, nx
            # Stop at junctions (but include the junction point)
            if (cy, cx) in junctions and len(path) > 1:
                break
        return np.array(path)

    # Start from endpoints first, then junctions, then any remaining
    starts = sorted(endpoints) + sorted(junctions)
    for sy, sx in starts:
        if visited[sy, sx]:
            continue
        branch = trace_from(sy, sx)
        if len(branch) >= 10:  # skip tiny fragments
            branches.append(branch)

    # Pick up any unvisited connected segments
    for y, x in pts:
        if not visited[y, x]:
            branch = trace_from(y, x)
            if len(branch) >= 10:
                branches.append(branch)

    return branches


def add_meander_to_branch(points, amplitude=30.0, wavelength=80, seed=0):
    """
    Displace a branch's control points perpendicular to flow to create meander.

    points: (N, 2) array [y, x]
    amplitude: max perpendicular displacement in pixels
    wavelength: spacing between control points for spline fitting

    Returns: (M, 2) array of smoothly meandering points.
    """
    if len(points) < 4:
        return points

    # Subsample to control points
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

    # Pick control points at regular arc-length intervals
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

    # Compute perpendicular direction at each control point
    rng = np.random.RandomState(seed)
    for i in range(1, len(ctrl_pts) - 1):  # don't move endpoints
        # Tangent from neighbors
        ty = ctrl_pts[i+1, 0] - ctrl_pts[i-1, 0]
        tx = ctrl_pts[i+1, 1] - ctrl_pts[i-1, 1]
        tlen = np.sqrt(ty**2 + tx**2)
        if tlen < 1e-6:
            continue
        # Perpendicular (rotate 90°)
        perp_y = -tx / tlen
        perp_x = ty / tlen

        # Random offset — use smooth variation, not pure random
        # Phase-shifted sine gives correlated meander
        phase = rng.uniform(-np.pi, np.pi)
        t_frac = i / len(ctrl_pts)
        offset = amplitude * np.sin(2 * np.pi * t_frac * 2.5 + phase)
        # Add some randomness on top
        offset += rng.uniform(-amplitude * 0.3, amplitude * 0.3)

        ctrl_pts[i, 0] += perp_y * offset
        ctrl_pts[i, 1] += perp_x * offset

    # Fit smooth spline through displaced control points
    try:
        tck, u = splprep([ctrl_pts[:, 1], ctrl_pts[:, 0]], s=0, k=3)
        # Evaluate at high resolution (1px spacing)
        n_eval = max(int(total_len), len(points))
        u_new = np.linspace(0, 1, n_eval)
        x_new, y_new = splev(u_new, tck)
        return np.column_stack([y_new, x_new])
    except Exception:
        return points


def rebuild_channel_mask(branches_original, branches_meandered, shape,
                          order_map, centerline_map, original_river_mask):
    """
    Rebuild the river mask from meandered spline paths.

    Width is sampled from the ORIGINAL branch points (before displacement)
    using the distance transform of the original mask — this preserves
    the original channel width including braid fills.

    Width noise adds ±30% variation for organic character.
    """
    h, w = shape
    new_mask = np.zeros((h, w), dtype=bool)

    # Measure original river width at every point using distance transform
    # interior_dist tells us how wide the river is at each skeleton point
    original_dist = distance_transform_edt(original_river_mask).astype(np.float32)

    rng = np.random.RandomState(123)
    # Width noise field — low frequency variation
    width_noise = gaussian_filter(rng.randn(h, w).astype(np.float32), sigma=25)
    width_noise = (width_noise - width_noise.min()) / (width_noise.max() - width_noise.min() + 1e-9)
    width_noise = 0.7 + width_noise * 0.6  # [0.7, 1.3]

    for orig_branch, mean_branch in zip(branches_original, branches_meandered):
        # Sample width from original skeleton positions
        n_orig = len(orig_branch)
        n_mean = len(mean_branch)

        for j in range(n_mean):
            # Map meandered index back to original index
            orig_idx = min(int(j * n_orig / n_mean), n_orig - 1)
            oy, ox = int(orig_branch[orig_idx, 0]), int(orig_branch[orig_idx, 1])
            oy = max(0, min(oy, h - 1))
            ox = max(0, min(ox, w - 1))

            # Width from original mask distance transform
            # This is the radius to the nearest edge at the original position
            orig_radius = original_dist[oy, ox]
            # Minimum width so thin channels don't vanish
            orig_radius = max(orig_radius, 3.0)

            # Apply noise
            my, mx = int(round(mean_branch[j, 0])), int(round(mean_branch[j, 1]))
            my = max(0, min(my, h - 1))
            mx = max(0, min(mx, w - 1))
            local_r = int(orig_radius * width_noise[my, mx])
            local_r = max(2, local_r)

            # Draw filled circle
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

    col0 = TX_MIN * TILE
    row0 = TZ_MIN * TILE
    w = (TX_MAX - TX_MIN) * TILE
    h = (TZ_MAX - TZ_MIN) * TILE
    win = Window(col0, row0, w, h)
    out_h, out_w = h, w

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

    sea_norm = 17050.0 / 65535.0
    is_sea = height < sea_norm

    # Hillshade
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

    # Original river mask
    any_river = (cl > 0) | (order > 0)
    river_only = any_river & ~is_sea & ~lake_mask

    # ── Step 1: Skeletonize ──────────────────────────────────────────
    print("Skeletonizing river mask...", flush=True)
    skel = skeletonize(river_only)
    skel_px = skel.sum()
    print(f"  Skeleton: {skel_px} px from {river_only.sum()} river px")

    # ── Step 2: Trace branches ───────────────────────────────────────
    print("Tracing skeleton branches...", flush=True)
    branches = trace_skeleton_branches(skel)
    print(f"  Found {len(branches)} branches "
          f"(lengths: {sorted([len(b) for b in branches], reverse=True)[:10]})")

    # ── Step 3-4: Add meander to each branch ─────────────────────────
    print("Adding meander to branches...", flush=True)
    meandered = []
    for i, branch in enumerate(branches):
        branch_len = len(branch)
        # Scale amplitude by branch length and order
        # Longer branches get more meander
        max_order_on_branch = 1
        for pt in branch[::10]:
            y, x = int(pt[0]), int(pt[1])
            if 0 <= y < out_h and 0 <= x < out_w:
                max_order_on_branch = max(max_order_on_branch, order[y, x])

        # Amplitude: order 1=8px, 2=15px, 3=25px, 4=35px, 5=45px
        amp = {1: 8, 2: 15, 3: 25, 4: 35, 5: 45}.get(max_order_on_branch, 15)
        # Wavelength: order 1=50, 2=70, 3=100, 4=140, 5=180
        wl = {1: 50, 2: 70, 3: 100, 4: 140, 5: 180}.get(max_order_on_branch, 80)

        m = add_meander_to_branch(branch, amplitude=amp, wavelength=wl, seed=i * 7)
        meandered.append(m)
        if branch_len > 50:
            print(f"    Branch {i}: {branch_len}px, order {max_order_on_branch}, "
                  f"amp={amp}, wl={wl} -> {len(m)} meander pts")

    # ── Step 5-6: Rebuild channel mask ───────────────────────────────
    print("Rebuilding channel mask from splines...", flush=True)
    new_river = rebuild_channel_mask(branches, meandered, (out_h, out_w),
                                      order, cl, river_only)

    # ── RENDER before/after ──────────────────────────────────────────
    print("Rendering...", flush=True)
    color_lut = _build_color_lut()

    def make_panel(river_mask):
        comp = color_lut[override].copy()
        comp = np.clip(comp.astype(np.float32) * shade[..., None] * 1.6,
                       0, 255).astype(np.uint8)
        comp[is_sea] = (30, 80, 160)
        comp[lake_mask] = (50, 110, 190)
        water = river_mask & ~lake_mask & ~is_sea
        comp[water] = (65, 130, 215)
        return comp

    before = make_panel(river_only)
    after = make_panel(new_river)

    # Side by side
    gap = 8
    canvas_w = out_w * 2 + gap
    canvas_h = out_h + 40
    canvas = np.full((canvas_h, canvas_w, 3), 20, dtype=np.uint8)
    y_off = 35
    canvas[y_off:y_off + out_h, :out_w] = before
    canvas[y_off:y_off + out_h, out_w + gap:] = after

    img = Image.fromarray(canvas, "RGB")
    draw = ImageDraw.Draw(img)

    draw.text((out_w // 2 - 40, 5), "ORIGINAL", fill=(255, 255, 255))
    draw.text((out_w + gap + out_w // 2 - 60, 5), "MEANDERED", fill=(255, 255, 255))
    draw.text((out_w // 2 - 70, 18), "(straight geometric)", fill=(180, 180, 180))
    draw.text((out_w + gap + out_w // 2 - 90, 18), "(spline meander + variable width)",
              fill=(180, 180, 180))

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
