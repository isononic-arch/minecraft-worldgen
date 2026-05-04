"""
diag_world_descent_v3.py — Steepest descent + GLOBAL Priority-Flood pit fill.

Barnes 2014 Priority-Flood: process cells from ocean boundary inward,
filling every pit as we go. After filling, every land cell has a strictly
downhill path to ocean. Descent then trivially reaches ocean for every
source.

Pre-fill cost: ~30-90 sec on 6250x6250 (Python heapq). Descent step is
much faster than v2 because no per-pit flooding is needed.

Lakes: real lakes (terrain-intersection from hydro_lake_wl) are kept as
their own underwater regions and act as descent sinks. Lake outflows
descend on the FILLED DEM from the lake's spillover, so they always
reach ocean.

Usage:
    py tools/diag_world_descent_v3.py --out memory/world_descent_v3.png
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
    """Barnes 2014 Priority-Flood seeded from `sink_seed_mask`. Every
    non-seed cell is filled to the highest pass elevation along the
    path back to a seed cell, so it has a non-uphill route to a seed.

    Seeds should include BOTH ocean AND real-lake underwater cells —
    that way underwater cells aren't filled (they're already sinks)
    and the surrounding terrain has a non-uphill route to them."""
    filled = height.astype(np.float32, copy=True)
    visited = np.zeros((H, W), dtype=bool)
    pq = []

    seed_r, seed_c = np.where(sink_seed_mask)
    for r, c in zip(seed_r, seed_c):
        heapq.heappush(pq, (float(filled[r, c]), int(r), int(c)))
        visited[r, c] = True

    n_pushed = len(pq)
    n_filled = 0
    t0 = time.time()
    last_log = t0
    while pq:
        h, r, c = heapq.heappop(pq)
        for dr, dc in NBRS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < H and 0 <= nc < W):
                continue
            if visited[nr, nc]:
                continue
            visited[nr, nc] = True
            nh = float(filled[nr, nc])
            if nh < h:
                filled[nr, nc] = h
                nh = h
                n_filled += 1
            heapq.heappush(pq, (nh, int(nr), int(nc)))
            n_pushed += 1
        # progress log every 5s
        if time.time() - last_log > 5.0:
            print(f"    fill: {n_pushed:,} cells processed, "
                  f"{n_filled:,} pits raised, "
                  f"queue={len(pq):,}", file=sys.stderr)
            last_log = time.time()
    print(f"    fill done in {time.time() - t0:.1f}s | "
          f"{n_filled:,} cells raised", file=sys.stderr)
    return filled


def steepest_descent_clean(start_r, start_c, height, sink_mask, H, W,
                             max_steps=50000, visited_global=None,
                             blocked_mask=None):
    """On a pit-filled DEM, every cell has a non-uphill path to a sink.
    Plateau handling: prefer strictly-lower neighbours, then sink cells
    at equal height (so we don't drift past lakes), then any equal-height
    cell.

    blocked_mask: cells that descent must NOT enter (e.g. source lake
    when computing its outflow path)."""
    visited = set()
    path = []
    r, c = start_r, start_c
    # Pre-load blocked cells into visited so descent can't enter them
    if blocked_mask is not None:
        br, bc = np.where(blocked_mask)
        for bbr, bbc in zip(br.tolist(), bc.tolist()):
            visited.add((bbr, bbc))
    for _ in range(max_steps):
        if (r, c) in visited:
            break
        visited.add((r, c))
        path.append((r, c))
        if len(path) > 1 and sink_mask[r, c]:
            return path
        if visited_global is not None and visited_global[r, c] and len(path) > 1:
            return path

        cur_h = height[r, c]
        best_strict = None; best_strict_h = cur_h
        best_equal_sink = None
        best_equal = None
        for dr, dc in NBRS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < H and 0 <= nc < W):
                continue
            if (nr, nc) in visited:
                continue
            h = height[nr, nc]
            if h < best_strict_h:
                best_strict_h = h
                best_strict = (nr, nc)
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
    p.add_argument("--source-step", type=int, default=12)
    p.add_argument("--source-percentile", type=float, default=70.0)
    p.add_argument("--cache-fill", default="memory/_cache_filled_dem.npy",
                   help="Cache pit-filled DEM here so we don't refill on every run")
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

    # ── Priority-Flood fill ──
    cache_loaded = False
    if cache_path.exists():
        try:
            cached = np.load(cache_path)
            if cached.shape == height_blocks.shape:
                print(f"Loading cached pit-filled DEM from {cache_path}...",
                      file=sys.stderr)
                filled = cached
                cache_loaded = True
        except Exception:
            pass
    if not cache_loaded:
        print("Running Priority-Flood pit fill (seeded from ocean only — "
              "lake outflows must reach REAL ocean)...",
              file=sys.stderr)
        sink_seed = ocean
        filled = priority_flood_fill(height_blocks, sink_seed, H, W)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache_path, filled)
        print(f"  cached to {cache_path}", file=sys.stderr)

    sink_mask = ocean | underwater

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
    print(f"  {len(sources)} sources", file=sys.stderr)

    print("Descending on filled DEM...", file=sys.stderr)
    river = np.zeros((H, W), dtype=bool)
    n_to_ocean = n_to_lake = n_merged = n_stuck = 0
    for sr, sc in sources:
        path = steepest_descent_clean(sr, sc, filled, sink_mask, H, W,
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

    # Lake outflows on filled DEM
    print("Lake outflows on filled DEM (every lake → ocean)...",
          file=sys.stderr)
    lake_labeled, n_lakes = _label(underwater)
    n_out_ocean = n_out_lake = n_out_merged = n_out_stuck = 0
    for lid in range(1, n_lakes + 1):
        lk = lake_labeled == lid
        if lk.sum() < 3:
            continue
        perim = binary_dilation(lk, iterations=1) & ~lk & ~ocean & ~underwater
        if not perim.any():
            continue
        # Spillover = lowest cell on perim using FILLED DEM (so we pick
        # the natural overflow point of the basin, not a dropped basin
        # interior). Tied with original-height tiebreaker for natural
        # shoreline preference.
        hs = filled.copy(); hs[~perim] = np.inf
        sp_flat = int(hs.argmin())
        sp_r, sp_c = sp_flat // W, sp_flat % W
        # Outflow MUST reach ocean (not just next-lake) per user invariant.
        # Block source lake AND all other lakes from being descent sinks.
        # Other lakes can still be CROSSED (basin_dry around them is
        # walkable on filled DEM); we just don't terminate there.
        descent_sink = ocean
        other_uw = underwater & ~lk
        path = steepest_descent_clean(sp_r, sp_c, filled, descent_sink, H, W,
                                       visited_global=river,
                                       blocked_mask=lk)
        if not path:
            continue
        end_r, end_c = path[-1]
        if ocean[end_r, end_c]:
            n_out_ocean += 1
        elif other_uw[end_r, end_c]:
            n_out_lake += 1
        elif river[end_r, end_c]:
            n_out_merged += 1
        else:
            n_out_stuck += 1
        for (r, c) in path:
            river[r, c] = True
    print(f"  outflows: ocean {n_out_ocean} | next-lake {n_out_lake} | "
          f"merged-into-river {n_out_merged} | stuck {n_out_stuck}",
          file=sys.stderr)

    # ── Inlet enforcement: any lake with no river touching it gets one ──
    print("Inlet enforcement: lakes without natural inlet...",
          file=sys.stderr)
    INLET_NEAR_RADIUS = 4   # 1:8 cells
    INLET_SEARCH_RADIUS = 25  # 1:8 cells around the lake
    n_lakes_total = 0
    n_lakes_with_inlet = 0
    n_inlets_forced = 0
    n_inlet_force_failed = 0
    river_dilated = binary_dilation(river, iterations=INLET_NEAR_RADIUS)

    for lid in range(1, n_lakes + 1):
        lk = lake_labeled == lid
        if lk.sum() < 3:
            continue
        n_lakes_total += 1
        has_inlet = (lk & river_dilated).any()
        if has_inlet:
            n_lakes_with_inlet += 1
            continue
        # Force inlet: find highest land cell within INLET_SEARCH_RADIUS
        # of the lake, NOT in any basin (so we start outside watersheds)
        search_zone = binary_dilation(lk, iterations=INLET_SEARCH_RADIUS) \
                      & ~lk & ~ocean & ~basin
        if not search_zone.any():
            # Try broader: include basin shore (orange dropped)
            search_zone = binary_dilation(lk, iterations=INLET_SEARCH_RADIUS) \
                          & ~lk & ~ocean
        if not search_zone.any():
            n_inlet_force_failed += 1
            continue
        hs = height_blocks.copy(); hs[~search_zone] = -np.inf
        src_flat = int(hs.argmax())
        src_r, src_c = src_flat // W, src_flat % W
        # Run descent — sink_mask includes `lk` so it terminates at THIS lake
        force_sink = sink_mask | lk
        path = steepest_descent_clean(src_r, src_c, filled, force_sink, H, W,
                                       visited_global=None)
        if not path:
            n_inlet_force_failed += 1
            continue
        for (r, c) in path:
            river[r, c] = True
        n_inlets_forced += 1

    print(f"  lakes total: {n_lakes_total} | "
          f"with natural inlet: {n_lakes_with_inlet} | "
          f"inlets forced: {n_inlets_forced} | "
          f"force failed: {n_inlet_force_failed}", file=sys.stderr)
    print(f"  total river cells: {river.sum():,}", file=sys.stderr)

    # ── Render ──
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
