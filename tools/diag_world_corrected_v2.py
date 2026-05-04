"""
diag_world_corrected_v2.py — Option B + connectivity invariants:
    1. Real lakes only (terrain-intersection).
    2. All centerlines extended through orange basin to touch blue.
    3. Subtle meander applied.
    4. CONNECTIVITY ENFORCEMENT post-pass:
        a) Every lake gets at least one INLET (forced from highest
           watershed cell down to underwater if absent).
        b) Every lake gets at least one OUTLET (greedy descent from
           the spillpoint until it hits ocean or another lake's
           underwater).
        c) Every river chain ultimately drains to ocean.

Usage:
    py tools/diag_world_corrected_v2.py --out memory/world_corrected_v2.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import (
    distance_transform_edt, binary_dilation, label as _label,
)

SEA_LEVEL_RAW = 17050
SCALE = 8


def bresenham(r0, c0, r1, c1):
    cells = []
    dr = abs(r1 - r0); dc = abs(c1 - c0)
    sr = 1 if r1 >= r0 else -1
    sc = 1 if c1 >= c0 else -1
    err = dr - dc
    r, c = r0, c0
    while True:
        cells.append((r, c))
        if r == r1 and c == c1:
            break
        e2 = 2 * err
        if e2 > -dc:
            err -= dc; r += sr
        if e2 < dr:
            err += dr; c += sc
    return cells


def steepest_descent(start_r, start_c, height, sink_mask, H, W, max_steps=2000):
    """Greedy steepest-descent path. Returns list of (r,c) cells from
    start to first sink hit, or None if stuck."""
    visited = set()
    path = []
    r, c = start_r, start_c
    for _ in range(max_steps):
        if sink_mask[r, c] and (r, c) != (start_r, start_c):
            path.append((r, c))
            return path
        path.append((r, c))
        visited.add((r, c))
        cur_h = height[r, c]
        best_h = cur_h
        best_rc = None
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if not (0 <= nr < H and 0 <= nc < W):
                    continue
                if (nr, nc) in visited:
                    continue
                h = height[nr, nc]
                if h < best_h:
                    best_h = h
                    best_rc = (nr, nc)
        if best_rc is None:
            # Stuck in pit — can't descend further
            return None
        r, c = best_rc
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    p.add_argument("--meander-amp", type=float, default=1.5)
    p.add_argument("--meander-wavelength", type=int, default=64)
    p.add_argument("--inlet-search-radius", type=int, default=20,
                   help="1:8 cells around basin to search for highest-watershed source")
    p.add_argument("--river-near-lake-radius", type=int, default=8,
                   help="A lake is considered 'connected' if any centerline lies within "
                        "this many 1:8 cells of its underwater area")
    args = p.parse_args()

    masks_dir = Path(args.masks)

    print("Reading masks at 1:8 working scale...", file=sys.stderr)

    def _read_downscaled(name):
        with rasterio.open(str(masks_dir / f"{name}.tif")) as src:
            return src.read(1, out_shape=(src.height // SCALE, src.width // SCALE),
                           resampling=rasterio.enums.Resampling.nearest)

    height = _read_downscaled("height")
    lake_id = _read_downscaled("hydro_lake")
    lake_wl_norm = _read_downscaled("hydro_lake_wl").astype(np.float32)
    centerline = _read_downscaled("hydro_centerline") > 0

    H, W = height.shape

    gaea_in = np.array([0, SEA_LEVEL_RAW, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    height_blocks = np.interp(
        height.ravel(), gaea_in, mc_y_out
    ).reshape(height.shape).astype(np.float32)
    lake_wl_mc = np.interp(
        (lake_wl_norm * 65535.0).ravel(), gaea_in, mc_y_out
    ).reshape(lake_wl_norm.shape).astype(np.float32)

    ocean = height <= SEA_LEVEL_RAW
    basin = (lake_id > 0) & ~ocean
    underwater = basin & (height_blocks < lake_wl_mc)
    basin_dry = basin & ~underwater

    # Label real-lake components (terrain-intersection)
    lake_labeled, n_lakes = _label(underwater)
    print(f"  real lakes: {n_lakes} components", file=sys.stderr)

    # ── Step 3: extend centerlines through basin_dry → underwater ──
    cl_dry = centerline & basin_dry
    basin_dil = binary_dilation(basin, iterations=1)
    cl_shore_facing = centerline & basin_dil & ~basin
    targets_for_extension = cl_dry | cl_shore_facing

    if underwater.any():
        _, idx = distance_transform_edt(~underwater, return_indices=True)
        target_r_arr, target_c_arr = idx[0], idx[1]
    else:
        target_r_arr = target_c_arr = None

    extended = centerline.copy()
    if target_r_arr is not None:
        sr_arr, sc_arr = np.where(targets_for_extension)
        for sr, sc in zip(sr_arr, sc_arr):
            tr = int(target_r_arr[sr, sc])
            tc = int(target_c_arr[sr, sc])
            for (r, c) in bresenham(int(sr), int(sc), tr, tc):
                if 0 <= r < H and 0 <= c < W:
                    extended[r, c] = True
    print(f"  extended centerlines: +{extended.sum() - centerline.sum():,} cells",
          file=sys.stderr)

    # ── Step 4: connectivity audit + enforcement ──
    print("Auditing connectivity per lake...", file=sys.stderr)
    n_isolated = 0
    n_inlets_added = 0
    n_outlets_added = 0
    n_outlet_to_ocean = 0
    n_outlet_to_lake = 0
    n_outlet_stranded = 0

    # Pre-compute "centerline near each lake" via dilation of extended
    cl_dilated = binary_dilation(extended, iterations=args.river_near_lake_radius)

    sink_mask_for_descent = ocean | underwater  # centerlines should reach these

    enforced = extended.copy()

    for lid in range(1, n_lakes + 1):
        lk = lake_labeled == lid
        if not lk.any():
            continue
        lake_size = int(lk.sum())
        if lake_size < 3:
            # ignore tiny noise components
            continue
        has_river_nearby = (cl_dilated & lk).any()

        if not has_river_nearby:
            n_isolated += 1

        # ── INLET: if no river touches the lake, force one ──
        if not has_river_nearby:
            # Search radius around lake — find highest cell
            lk_dil = binary_dilation(lk, iterations=args.inlet_search_radius)
            search_zone = lk_dil & ~lk & ~ocean & ~basin  # outside basin, outside lake, on land
            if search_zone.any():
                # Highest cell in search_zone
                hs = height_blocks.copy()
                hs[~search_zone] = -np.inf
                src_flat = int(hs.argmax())
                src_r, src_c = src_flat // W, src_flat % W
                # Nearest underwater cell of THIS lake
                dist_to_lk, idx_to_lk = distance_transform_edt(
                    ~lk, return_indices=True
                )
                tr = int(idx_to_lk[0, src_r, src_c])
                tc = int(idx_to_lk[1, src_r, src_c])
                for (r, c) in bresenham(src_r, src_c, tr, tc):
                    if 0 <= r < H and 0 <= c < W:
                        enforced[r, c] = True
                n_inlets_added += 1

        # ── OUTLET: every lake should have an outflow that reaches a
        # downstream sink (ocean or another lake's underwater) ──
        # Find the spillpoint = lowest cell on the basin perimeter just
        # outside underwater (or in basin_dry adjacent to underwater).
        # Then steepest-descend from there.
        lk_dil1 = binary_dilation(lk, iterations=1)
        spill_candidates = lk_dil1 & ~lk & ~ocean
        if not spill_candidates.any():
            continue
        # Pick the lowest one
        hs = height_blocks.copy()
        hs[~spill_candidates] = np.inf
        sp_flat = int(hs.argmin())
        sp_r, sp_c = sp_flat // W, sp_flat % W

        # Sink mask for this descent: ocean | OTHER lakes' underwater
        # (exclude THIS lake to prevent immediate self-loop)
        other_underwater = underwater & ~lk
        descent_sink = ocean | other_underwater
        # Check if there's any centerline downstream of the spillpoint
        # (within river_near_lake_radius of spillpoint, on non-lake side)
        sp_neighborhood = np.zeros_like(extended)
        rr0, rr1 = max(0, sp_r - args.river_near_lake_radius), min(H, sp_r + args.river_near_lake_radius + 1)
        cc0, cc1 = max(0, sp_c - args.river_near_lake_radius), min(W, sp_c + args.river_near_lake_radius + 1)
        sp_neighborhood[rr0:rr1, cc0:cc1] = True
        outlet_present = (extended & sp_neighborhood & ~lk).any()
        if outlet_present:
            continue

        # Run steepest descent
        path = steepest_descent(
            sp_r, sp_c, height_blocks, descent_sink, H, W, max_steps=2000
        )
        if path is None:
            n_outlet_stranded += 1
            continue
        for (r, c) in path:
            enforced[r, c] = True
        n_outlets_added += 1
        end_r, end_c = path[-1]
        if ocean[end_r, end_c]:
            n_outlet_to_ocean += 1
        else:
            n_outlet_to_lake += 1

    print(f"  isolated lakes: {n_isolated}", file=sys.stderr)
    print(f"  inlets force-added: {n_inlets_added}", file=sys.stderr)
    print(f"  outlets force-added: {n_outlets_added} "
          f"(to ocean: {n_outlet_to_ocean}, to next lake: {n_outlet_to_lake})",
          file=sys.stderr)
    print(f"  outlet stranded (no descent path): {n_outlet_stranded}",
          file=sys.stderr)

    # ── Step 5: subtle meander on enforced centerlines ──
    print("Applying subtle meander...", file=sys.stderr)
    try:
        import opensimplex as _ox
        ox = _ox.OpenSimplex(seed=0xC0DEC0D)
        ws = (np.arange(W) / args.meander_wavelength).astype(np.float64)
        hs2 = (np.arange(H) / args.meander_wavelength).astype(np.float64)
        nx = ox.noise2array(ws + 100.0, hs2 + 200.0).astype(np.float32)
        ny = ox.noise2array(ws + 300.0, hs2 + 400.0).astype(np.float32)
    except ImportError:
        nx = np.zeros((H, W), dtype=np.float32)
        ny = np.zeros((H, W), dtype=np.float32)

    meandered = enforced.copy()
    cl_r, cl_c = np.where(enforced)
    AMP = args.meander_amp
    drs = np.round(ny[cl_r, cl_c] * AMP).astype(np.int32)
    dcs = np.round(nx[cl_r, cl_c] * AMP).astype(np.int32)
    new_r = np.clip(cl_r + drs, 0, H - 1)
    new_c = np.clip(cl_c + dcs, 0, W - 1)
    meandered[new_r, new_c] = True

    # ── Step 6: render ──
    print("Rendering...", file=sys.stderr)
    norm = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height_blocks)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1)

    img = base.copy()
    img[ocean] = [0.13, 0.27, 0.49]
    img[underwater] = [0.20, 0.50, 0.80]
    img[meandered] = [0.10, 0.40, 0.85]

    rgb = (img * 255).astype(np.uint8)
    Image.fromarray(rgb).save(args.out, optimize=True)
    print(f"Saved {args.out}", file=sys.stderr)

    out_2k = args.out.replace(".png", "_2k.png")
    img_2k = Image.fromarray(rgb)
    img_2k.thumbnail((2000, 2000), Image.LANCZOS)
    img_2k.save(out_2k, optimize=True)
    print(f"Saved {out_2k}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
