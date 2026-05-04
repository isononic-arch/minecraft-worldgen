"""
wire_descent_to_centerline.py — Replace hydro_centerline.tif and friends
with the descent + flow-accumulation network from diag_world_descent_v4.

Pipeline:
    1. Load cached filled DEM (or compute Priority-Flood if absent).
    2. Run descent from a dense grid of sources, count visits per cell
       = flow accumulation.
    3. Threshold: river_mask = accum >= 200.
    4. INLET enforcement: for every real lake with no river adjacent,
       descend from the highest neighbouring cell using the filled DEM.
    5. Compute per-cell width from accumulation (sqrt-scaled).
    6. Write hydro_centerline.tif / hydro_width.tif / hydro_order.tif at
       50k via NEAREST upscale. Back up the existing TIFs first.

Usage:
    py tools/wire_descent_to_centerline.py
"""
from __future__ import annotations

import argparse
import heapq
import sys
import shutil
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine
from scipy.ndimage import label as _label, binary_dilation, zoom as _zoom

SEA_LEVEL_RAW = 17050
SCALE = 8
WORLD_PX = 50_000

NBRS = [(-1, -1), (-1, 0), (-1, 1),
        ( 0, -1),          ( 0, 1),
        ( 1, -1), ( 1, 0), ( 1, 1)]


def priority_flood_fill(height, sink_seed_mask, H, W):
    filled = height.astype(np.float32, copy=True)
    visited = np.zeros((H, W), dtype=bool)
    pq = []
    sr, sc = np.where(sink_seed_mask)
    for r, c in zip(sr, sc):
        heapq.heappush(pq, (float(filled[r, c]), int(r), int(c)))
        visited[r, c] = True
    n_filled = 0
    t0 = time.time(); last = t0
    while pq:
        h, r, c = heapq.heappop(pq)
        for dr, dc in NBRS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < H and 0 <= nc < W): continue
            if visited[nr, nc]: continue
            visited[nr, nc] = True
            nh = float(filled[nr, nc])
            if nh < h:
                filled[nr, nc] = h; nh = h; n_filled += 1
            heapq.heappush(pq, (nh, int(nr), int(nc)))
        if time.time() - last > 5:
            print(f"    PF: queue={len(pq):,} pits raised={n_filled:,}",
                  file=sys.stderr); last = time.time()
    print(f"    PF done in {time.time() - t0:.1f}s | {n_filled:,} raised",
          file=sys.stderr)
    return filled


def descent_path(start_r, start_c, height, sink_mask, H, W,
                  blocked_mask=None, max_steps=80000):
    visited = set()
    if blocked_mask is not None:
        br, bc = np.where(blocked_mask)
        visited.update(zip(br.tolist(), bc.tolist()))
    path = []
    r, c = start_r, start_c
    for _ in range(max_steps):
        if (r, c) in visited: break
        visited.add((r, c))
        path.append((r, c))
        if len(path) > 1 and sink_mask[r, c]:
            return path
        cur = height[r, c]
        best = None; best_h = cur
        best_eq_sink = None; best_eq = None
        for dr, dc in NBRS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < H and 0 <= nc < W): continue
            if (nr, nc) in visited: continue
            h = height[nr, nc]
            if h < best_h:
                best_h = h; best = (nr, nc)
            elif h == cur:
                if sink_mask[nr, nc] and best_eq_sink is None:
                    best_eq_sink = (nr, nc)
                elif best_eq is None:
                    best_eq = (nr, nc)
        if best is not None: r, c = best
        elif best_eq_sink is not None: r, c = best_eq_sink
        elif best_eq is not None: r, c = best_eq
        else: return path
    return path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--cache-fill", default="memory/_cache_filled_dem_oceanonly.npy")
    p.add_argument("--source-step", type=int, default=8)
    p.add_argument("--source-percentile", type=float, default=50.0)
    p.add_argument("--river-threshold", type=int, default=200,
                   help="accum >= this is a river")
    p.add_argument("--max-width-radius", type=float, default=5.0,
                   help="MC-block radius for max-flow river (sqrt scaling)")
    args = p.parse_args()

    masks_dir = Path(args.masks)

    print("Reading masks @ 1:8...", file=sys.stderr)
    def _read(name):
        with rasterio.open(str(masks_dir / f"{name}.tif")) as src:
            return src.read(1, out_shape=(src.height // SCALE, src.width // SCALE),
                           resampling=rasterio.enums.Resampling.nearest)

    height = _read("height")
    lake_id = _read("hydro_lake")
    lake_wl_norm = _read("hydro_lake_wl").astype(np.float32)

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
    land = ~ocean
    sink_mask = ocean | underwater

    cache_path = Path(args.cache_fill)
    if cache_path.exists():
        print(f"Loading filled DEM cache...", file=sys.stderr)
        filled = np.load(cache_path)
    else:
        print("Priority-Flood (ocean-only seed)...", file=sys.stderr)
        filled = priority_flood_fill(height_blocks, ocean, H, W)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache_path, filled)

    # ── Sources ──
    print("Sources...", file=sys.stderr)
    h_thresh = float(np.percentile(height_blocks[land], args.source_percentile))
    rs, cs = np.meshgrid(np.arange(0, H, args.source_step),
                         np.arange(0, W, args.source_step), indexing="ij")
    rs = rs.ravel(); cs = cs.ravel()
    valid = (height_blocks[rs, cs] >= h_thresh) & land[rs, cs] & ~basin[rs, cs]
    sources = list(zip(rs[valid].tolist(), cs[valid].tolist()))
    print(f"  {len(sources)} sources", file=sys.stderr)

    # ── Descent + accumulation ──
    print("Descent + accum...", file=sys.stderr)
    accum = np.zeros((H, W), dtype=np.int32)
    t0 = time.time()
    n_ocean = n_lake = n_stuck = 0
    for i, (sr, sc) in enumerate(sources):
        path = descent_path(sr, sc, filled, sink_mask, H, W)
        if not path: continue
        er, ec = path[-1]
        if ocean[er, ec]: n_ocean += 1
        elif underwater[er, ec]: n_lake += 1
        else: n_stuck += 1
        for (r, c) in path:
            accum[r, c] += 1
    print(f"  done in {time.time()-t0:.1f}s | "
          f"ocean={n_ocean} lake={n_lake} stuck={n_stuck}",
          file=sys.stderr)
    print(f"  accum max={accum.max()} | >=200 cells={(accum>=200).sum():,}",
          file=sys.stderr)

    # ── Build river mask ──
    river_mask = (accum >= args.river_threshold) & ~ocean & ~underwater

    # ── Inlet enforcement: any real lake with no river adjacent gets one ──
    print("Inlet enforcement...", file=sys.stderr)
    lake_labeled, n_lakes = _label(underwater)
    river_dil = binary_dilation(river_mask, iterations=4)
    n_forced = 0
    for lid in range(1, n_lakes + 1):
        lk = lake_labeled == lid
        if lk.sum() < 3: continue
        if (lk & river_dil).any(): continue
        # No natural inlet — force one
        zone = binary_dilation(lk, iterations=25) & ~lk & ~ocean & ~basin
        if not zone.any():
            zone = binary_dilation(lk, iterations=25) & ~lk & ~ocean
        if not zone.any(): continue
        hs = height_blocks.copy(); hs[~zone] = -np.inf
        sf = int(hs.argmax()); src_r, src_c = sf // W, sf % W
        path = descent_path(src_r, src_c, filled, underwater, H, W)
        if not path: continue
        for (r, c) in path:
            river_mask[r, c] = True
            # Boost accumulation so width is consistent
            accum[r, c] = max(accum[r, c], args.river_threshold * 2)
        n_forced += 1
    print(f"  forced {n_forced} inlets", file=sys.stderr)
    river_mask = river_mask & ~ocean & ~underwater  # clean

    # ── Compute per-cell width (radius in MC blocks) ──
    # max_accum cell → max_width_radius. sqrt scaling → biggest rivers
    # not unreasonably wide.
    max_accum = max(int(accum.max()), 1)
    width_radius = np.zeros((H, W), dtype=np.float32)
    in_river = river_mask
    if in_river.any():
        # radius ranges from 1.5 (small streams) to max_width_radius (trunks)
        sqrt_norm = np.sqrt(np.clip(accum.astype(np.float32), 1, max_accum) /
                             max_accum)
        width_radius = 1.5 + (args.max_width_radius - 1.5) * sqrt_norm
        width_radius[~in_river] = 0
    width_blocks = width_radius.astype(np.uint8)
    width_blocks[in_river & (width_blocks < 2)] = 2  # min visible

    print(f"  river cells: {river_mask.sum():,} | "
          f"width: min={width_blocks[in_river].min()} "
          f"max={width_blocks[in_river].max()}", file=sys.stderr)

    # ── Write 50k TIFs (NEAREST upscale 1:8 → 50k) ──
    print("Writing 50k TIFs...", file=sys.stderr)

    # Backup
    for name in ("hydro_centerline", "hydro_width", "hydro_order"):
        src = masks_dir / f"{name}.tif"
        bk = masks_dir / f"{name}.tif.preDescent_v1"
        if src.exists() and not bk.exists():
            shutil.copy(src, bk)
            print(f"  backed up {name}.tif → {bk.name}", file=sys.stderr)

    # Build 50k arrays via NEAREST zoom
    cl_50k = _zoom(river_mask.astype(np.uint8), SCALE, order=0)
    w_50k  = _zoom(width_blocks, SCALE, order=0)
    order_50k = (cl_50k > 0).astype(np.uint8)  # all order=1

    # Trim/pad to exactly 50000x50000
    def _fit(arr, target=WORLD_PX):
        h, w = arr.shape
        if h > target: arr = arr[:target]
        if w > target: arr = arr[:, :target]
        if h < target or w < target:
            pad = np.zeros((target, target), dtype=arr.dtype)
            pad[:arr.shape[0], :arr.shape[1]] = arr
            arr = pad
        return arr

    cl_50k = _fit(cl_50k)
    w_50k = _fit(w_50k)
    order_50k = _fit(order_50k)

    profile_ref = None
    with rasterio.open(str(masks_dir / "height.tif")) as src:
        profile_ref = src.profile.copy()

    def _write(name, arr):
        out = masks_dir / f"{name}.tif"
        prof = profile_ref.copy()
        prof.update(dtype=arr.dtype, count=1, compress="lzw")
        with rasterio.open(str(out), "w", **prof) as dst:
            dst.write(arr, 1)
        print(f"  wrote {name}.tif  shape={arr.shape} dtype={arr.dtype} "
              f"sum={int(arr.sum()):,}", file=sys.stderr)

    _write("hydro_centerline", cl_50k)
    _write("hydro_width", w_50k)
    _write("hydro_order", order_50k)

    print("Done. Carver picks up these TIFs automatically on next run.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
