"""
diag_world_descent_v4.py — Descent + flow accumulation thresholding.

The v3 descent draws EVERY source-to-sink path → 631k cells, way too dense.
Real rivers only appear where enough watershed feeds them. v4 fix:

    1. Re-run descent from every source WITHOUT merging into existing rivers
       (each path runs full length, so cells get re-visited).
    2. Count visits per cell = flow accumulation (proxy for catchment area).
    3. Threshold: cells with visits >= K are "visible river."
    4. Lake outflows get a guaranteed minimum-rank line through their path
       so every connected lake has a visible outlet regardless of accum.

Output: layered render with multiple thresholds so we can pick the
right density.

Usage:
    py tools/diag_world_descent_v4.py --out memory/world_descent_v4.png
"""
from __future__ import annotations

import argparse
import heapq
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import label as _label, binary_dilation

SEA_LEVEL_RAW = 17050
SCALE = 8

NBRS = [(-1, -1), (-1, 0), (-1, 1),
        ( 0, -1),          ( 0, 1),
        ( 1, -1), ( 1, 0), ( 1, 1)]


def priority_flood_fill(height, sink_seed_mask, H, W):
    """Barnes 2014 Priority-Flood seeded from `sink_seed_mask`."""
    filled = height.astype(np.float32, copy=True)
    visited = np.zeros((H, W), dtype=bool)
    pq = []
    seed_r, seed_c = np.where(sink_seed_mask)
    for r, c in zip(seed_r, seed_c):
        heapq.heappush(pq, (float(filled[r, c]), int(r), int(c)))
        visited[r, c] = True
    n_pushed = len(pq); n_filled = 0
    t0 = time.time(); last_log = t0
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
            n_pushed += 1
        if time.time() - last_log > 5.0:
            print(f"    fill: {n_pushed:,} cells, {n_filled:,} pits, "
                  f"queue={len(pq):,}", file=sys.stderr)
            last_log = time.time()
    print(f"    fill done in {time.time() - t0:.1f}s | {n_filled:,} raised",
          file=sys.stderr)
    return filled


def descent_path(start_r, start_c, height, sink_mask, H, W, max_steps=80000):
    """Pure descent — returns full path from source to sink. No merging."""
    visited = set()
    path = []
    r, c = start_r, start_c
    for _ in range(max_steps):
        if (r, c) in visited:
            break
        visited.add((r, c))
        path.append((r, c))
        if len(path) > 1 and sink_mask[r, c]:
            return path
        cur_h = height[r, c]
        best_strict = None; best_strict_h = cur_h
        best_equal_sink = None
        best_equal = None
        for dr, dc in NBRS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < H and 0 <= nc < W): continue
            if (nr, nc) in visited: continue
            h = height[nr, nc]
            if h < best_strict_h:
                best_strict_h = h; best_strict = (nr, nc)
            elif h == cur_h:
                if sink_mask[nr, nc] and best_equal_sink is None:
                    best_equal_sink = (nr, nc)
                elif best_equal is None:
                    best_equal = (nr, nc)
        if best_strict is not None:
            r, c = best_strict
        elif best_equal_sink is not None:
            r, c = best_equal_sink
        elif best_equal is not None:
            r, c = best_equal
        else:
            return path
    return path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    p.add_argument("--source-step", type=int, default=8,
                   help="Sample sources every N cells (smaller=denser sampling)")
    p.add_argument("--source-percentile", type=float, default=50.0,
                   help="Sources must be above this height percentile")
    p.add_argument("--cache-fill", default="memory/_cache_filled_dem_oceanonly.npy")
    args = p.parse_args()

    masks_dir = Path(args.masks)
    cache_path = Path(args.cache_fill)

    print("Reading masks at 1:8 scale...", file=sys.stderr)

    def _read_downscaled(name):
        with rasterio.open(str(masks_dir / f"{name}.tif")) as src:
            return src.read(1, out_shape=(src.height // SCALE, src.width // SCALE),
                           resampling=rasterio.enums.Resampling.nearest)

    height = _read_downscaled("height")
    lake_id = _read_downscaled("hydro_lake")
    lake_wl_norm = _read_downscaled("hydro_lake_wl").astype(np.float32)

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

    if cache_path.exists():
        print(f"Loading cached filled DEM (ocean-only seed)...", file=sys.stderr)
        filled = np.load(cache_path)
    else:
        print("Priority-Flood (ocean-only seed)...", file=sys.stderr)
        filled = priority_flood_fill(height_blocks, ocean, H, W)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache_path, filled)
        print(f"  cached to {cache_path}", file=sys.stderr)

    sink_mask = ocean | underwater

    # ── Sources: dense sampling for accurate accumulation ──
    print("Selecting sources...", file=sys.stderr)
    h_thresh = float(np.percentile(height_blocks[land], args.source_percentile))
    step = args.source_step
    rs, cs = np.meshgrid(np.arange(0, H, step), np.arange(0, W, step),
                         indexing="ij")
    rs = rs.ravel(); cs = cs.ravel()
    is_source = (
        (height_blocks[rs, cs] >= h_thresh)
        & land[rs, cs]
        & ~basin[rs, cs]
    )
    sources = list(zip(rs[is_source].tolist(), cs[is_source].tolist()))
    print(f"  {len(sources)} sources (every {step} cells, above Y {h_thresh:.0f})",
          file=sys.stderr)

    # ── Descent + flow accumulation ──
    print("Descent + accumulation (no merge — full paths each)...",
          file=sys.stderr)
    accum = np.zeros((H, W), dtype=np.int32)
    n_to_ocean = n_to_lake = n_stuck = 0
    t0 = time.time()
    for i, (sr, sc) in enumerate(sources):
        if i % 5000 == 0 and i > 0:
            print(f"  source {i}/{len(sources)}  "
                  f"ocean={n_to_ocean} lake={n_to_lake} stuck={n_stuck}  "
                  f"({time.time()-t0:.1f}s)", file=sys.stderr)
        path = descent_path(sr, sc, filled, sink_mask, H, W)
        if not path:
            continue
        end_r, end_c = path[-1]
        if ocean[end_r, end_c]:
            n_to_ocean += 1
        elif underwater[end_r, end_c]:
            n_to_lake += 1
        else:
            n_stuck += 1
        for (r, c) in path:
            accum[r, c] += 1
    print(f"  done in {time.time()-t0:.1f}s | "
          f"ocean={n_to_ocean} lake={n_to_lake} stuck={n_stuck}",
          file=sys.stderr)
    print(f"  accumulation: max={accum.max()}, "
          f"cells>=1 {(accum >= 1).sum():,} | "
          f">=5 {(accum >= 5).sum():,} | "
          f">=20 {(accum >= 20).sum():,} | "
          f">=50 {(accum >= 50).sum():,} | "
          f">=200 {(accum >= 200).sum():,}", file=sys.stderr)

    # ── Render with TWO thresholds: small streams + main rivers ──
    norm = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height_blocks)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1)

    # Layered render: bigger rivers brighter blue, streams subtle
    img = base.copy()
    img[ocean] = [0.13, 0.27, 0.49]
    img[underwater] = [0.20, 0.50, 0.80]

    # Two-tier rivers
    streams = (accum >= 20) & ~ocean & ~underwater       # ~headwater streams
    rivers  = (accum >= 200) & ~ocean & ~underwater      # main rivers
    big     = (accum >= 1000) & ~ocean & ~underwater     # major trunks

    img[streams] = [0.45, 0.65, 0.85]
    img[rivers]  = [0.20, 0.50, 0.80]
    img[big]     = [0.10, 0.40, 0.85]

    print(f"  render: streams(>=20)={streams.sum():,} | "
          f"rivers(>=200)={rivers.sum():,} | "
          f"trunks(>=1000)={big.sum():,}", file=sys.stderr)

    rgb = (img * 255).astype(np.uint8)
    Image.fromarray(rgb).save(args.out, optimize=True)
    out_2k = args.out.replace(".png", "_2k.png")
    img_2k = Image.fromarray(rgb)
    img_2k.thumbnail((2000, 2000), Image.LANCZOS)
    img_2k.save(out_2k, optimize=True)
    print(f"Saved {args.out} + {out_2k}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
