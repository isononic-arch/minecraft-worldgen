"""
diag_world_descent_v2.py — Steepest descent + priority-flood pit filling.

When greedy descent gets stuck in a pit, run priority-flood from the pit
to find the spillover (lowest external cell reachable by raising water
level), then continue descent from there. Loop until reach ocean or
real lake or run out of patience.

Usage:
    py tools/diag_world_descent_v2.py --out memory/world_descent_v2.png
"""
from __future__ import annotations

import argparse
import heapq
import sys
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


def steepest_descent_one_step(r, c, height, H, W, visited):
    """Move from (r,c) to its lowest neighbour. Returns (nr, nc) or None
    if no lower unvisited neighbour exists."""
    cur_h = height[r, c]
    best_h = cur_h
    best_rc = None
    for dr, dc in NBRS:
        nr, nc = r + dr, c + dc
        if not (0 <= nr < H and 0 <= nc < W):
            continue
        if (nr, nc) in visited:
            continue
        h = height[nr, nc]
        if h < best_h:
            best_h = h; best_rc = (nr, nc)
    return best_rc


def find_spillover(pit_r, pit_c, height, H, W, max_visits=20000):
    """Priority-flood from a pit. Returns (r, c) of the first external
    cell strictly below the rising water level — the spillover."""
    pit_h = height[pit_r, pit_c]
    visited = {(pit_r, pit_c)}
    pq = [(pit_h, pit_r, pit_c)]
    water = pit_h
    visits = 0
    while pq and visits < max_visits:
        h, r, c = heapq.heappop(pq)
        visits += 1
        if h > water:
            water = h
        for dr, dc in NBRS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < H and 0 <= nc < W):
                continue
            if (nr, nc) in visited:
                continue
            nh = height[nr, nc]
            if nh < water:
                # Spillover — water at level `water` overflows into this cell
                return nr, nc
            visited.add((nr, nc))
            heapq.heappush(pq, (nh, nr, nc))
    return None


def descent_with_pit_filling(start_r, start_c, height, sink_mask, H, W,
                              max_total_steps=8000, max_pit_iters=50,
                              visited_global=None):
    """Run steepest-descent. When stuck in a pit, priority-flood to find
    spillover, continue from spillover. Returns list of (r,c) cells."""
    path = []
    visited = set()
    r, c = start_r, start_c
    pit_iter = 0
    total = 0
    while total < max_total_steps and pit_iter < max_pit_iters:
        if (r, c) in visited:
            break
        visited.add((r, c))
        path.append((r, c))
        total += 1

        if total > 1 and sink_mask[r, c]:
            return path
        if visited_global is not None and visited_global[r, c] and total > 1:
            return path

        nxt = steepest_descent_one_step(r, c, height, H, W, visited)
        if nxt is None:
            # Pit detected. Look for spillover.
            spill = find_spillover(r, c, height, H, W)
            if spill is None:
                return path  # truly stuck (shouldn't happen mid-continent)
            r, c = spill
            pit_iter += 1
            continue
        r, c = nxt
    return path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    p.add_argument("--source-step", type=int, default=12)
    p.add_argument("--source-percentile", type=float, default=70.0)
    args = p.parse_args()

    masks_dir = Path(args.masks)

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
    sink_mask = ocean | underwater

    print("Selecting sources...", file=sys.stderr)
    h_thresh = float(np.percentile(height_blocks[land], args.source_percentile))
    print(f"  threshold Y {h_thresh:.0f}", file=sys.stderr)

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
    print(f"  {len(sources)} sources", file=sys.stderr)

    print("Descending with pit-filling...", file=sys.stderr)
    river = np.zeros((H, W), dtype=bool)
    n_to_ocean = n_to_lake = n_merged = n_stuck = 0
    for i, (sr, sc) in enumerate(sources):
        if i % 5000 == 0:
            print(f"  source {i}/{len(sources)} (ocean={n_to_ocean} lake={n_to_lake} "
                  f"merge={n_merged} stuck={n_stuck})", file=sys.stderr)
        path = descent_with_pit_filling(sr, sc, height_blocks, sink_mask, H, W,
                                          visited_global=river)
        if not path:
            continue
        end_r, end_c = path[-1]
        if ocean[end_r, end_c]:
            n_to_ocean += 1
        elif underwater[end_r, end_c]:
            n_to_lake += 1
        elif river[end_r, end_c]:
            n_merged += 1
        else:
            n_stuck += 1
        for (r, c) in path:
            river[r, c] = True

    print(f"  source paths: ocean {n_to_ocean} | lake {n_to_lake} | "
          f"merge {n_merged} | stuck {n_stuck}", file=sys.stderr)

    # Lake outflows
    print("Lake outflows...", file=sys.stderr)
    lake_labeled, n_lakes = _label(underwater)
    n_out_ocean = n_out_lake = n_out_stuck = 0
    for lid in range(1, n_lakes + 1):
        lk = lake_labeled == lid
        if lk.sum() < 3:
            continue
        perim = binary_dilation(lk, iterations=1) & ~lk & ~ocean & ~underwater
        if not perim.any():
            continue
        hs = height_blocks.copy(); hs[~perim] = np.inf
        sp_flat = int(hs.argmin())
        sp_r, sp_c = sp_flat // W, sp_flat % W
        other_uw = underwater & ~lk
        descent_sink = ocean | other_uw
        path = descent_with_pit_filling(sp_r, sp_c, height_blocks, descent_sink,
                                          H, W, visited_global=river)
        if not path:
            continue
        end_r, end_c = path[-1]
        if ocean[end_r, end_c]:
            n_out_ocean += 1
        elif other_uw[end_r, end_c]:
            n_out_lake += 1
        else:
            n_out_stuck += 1
        for (r, c) in path:
            river[r, c] = True

    print(f"  outflows: ocean {n_out_ocean} | next-lake {n_out_lake} | "
          f"stuck {n_out_stuck}", file=sys.stderr)
    print(f"  total river cells: {river.sum():,}", file=sys.stderr)

    # Render
    norm = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height_blocks)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1)

    img = base.copy()
    img[ocean] = [0.13, 0.27, 0.49]
    img[underwater] = [0.20, 0.50, 0.80]
    img[river] = [0.10, 0.40, 0.85]

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
