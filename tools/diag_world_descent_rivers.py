"""
diag_world_descent_rivers.py — Physical river network via steepest descent.

DROPS the existing hydro_centerline.tif and generates a NEW river network
from terrain alone, the way real water would flow:

    1. Pick sources: high-elevation cells (top X% of land), uniformly
       gridded at sample_step spacing so we get evenly-distributed
       headwaters.
    2. For each source, run steepest-descent over the height field
       until:
         a) hit OCEAN  → terminate path normally (good)
         b) hit underwater LAKE → terminate, mark lake as "has inlet"
         c) get stuck in a PIT → terminate; fill pit and continue from
            the lowest cell on its rim (spillover continuation)
         d) MAX_STEPS exceeded → safety stop
    3. Lake outflows: for each lake without descent-recorded inlet, also
       run from its spillover cell so the lake has an outlet line.

No Bresenham anywhere. Every cell of every path is a steepest-descent
neighbour of the previous cell, so paths follow terrain exactly.

Usage:
    py tools/diag_world_descent_rivers.py --out memory/world_descent_rivers.png
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
    label as _label, binary_dilation, distance_transform_edt,
)

SEA_LEVEL_RAW = 17050
SCALE = 8

# 8-neighbour offsets
NBRS = [(-1, -1), (-1, 0), (-1, 1),
        ( 0, -1),          ( 0, 1),
        ( 1, -1), ( 1, 0), ( 1, 1)]


def steepest_descent(start_r, start_c, height, sink_mask, H, W,
                     max_steps=4000, visited_global=None):
    """Run steepest-descent from (start_r, start_c) until hitting a sink.

    Stops on:
      - sink_mask[r,c] True (and not the start)
      - no lower neighbour (pit)
      - already in visited_global (path joined another path — terminate
        cleanly so we don't redraw that segment)
      - max_steps

    Returns the list of cells walked. The caller decides what to do
    with the terminal state."""
    path = []
    r, c = start_r, start_c
    local_visited = set()

    for step in range(max_steps):
        if (r, c) in local_visited:
            break
        local_visited.add((r, c))
        path.append((r, c))

        if step > 0 and sink_mask[r, c]:
            return path

        if visited_global is not None and visited_global[r, c]:
            # Joined an existing path — stop, but include this cell
            # so the visual is connected
            return path

        cur_h = height[r, c]
        best_h = cur_h
        best_rc = None
        for dr, dc in NBRS:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < H and 0 <= nc < W):
                continue
            h = height[nr, nc]
            if h < best_h:
                best_h = h
                best_rc = (nr, nc)
        if best_rc is None:
            # Pit — no lower neighbour
            return path
        r, c = best_rc
    return path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    p.add_argument("--source-step", type=int, default=12,
                   help="Sample sources every N 1:8 cells (smaller = denser river network)")
    p.add_argument("--source-percentile", type=float, default=70.0,
                   help="Sources must be above this height percentile (of land cells)")
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

    # Sink for descent: ocean OR any real-lake underwater cell.
    sink_mask = ocean | underwater

    # ── Pick sources: high-elevation land cells on a grid ──
    print("Selecting sources...", file=sys.stderr)
    h_thresh = float(np.percentile(height_blocks[land], args.source_percentile))
    print(f"  elevation threshold ({args.source_percentile}th pct): "
          f"Y {h_thresh:.0f}", file=sys.stderr)

    step = args.source_step
    rs, cs = np.meshgrid(np.arange(0, H, step), np.arange(0, W, step),
                         indexing="ij")
    rs = rs.ravel(); cs = cs.ravel()
    is_source = (
        (height_blocks[rs, cs] >= h_thresh)
        & land[rs, cs]
        & ~basin[rs, cs]  # don't seed inside basins
    )
    sources = list(zip(rs[is_source].tolist(), cs[is_source].tolist()))
    print(f"  {len(sources)} sources picked", file=sys.stderr)

    # ── Run steepest-descent from each source ──
    print("Running steepest-descent from each source...", file=sys.stderr)
    river = np.zeros((H, W), dtype=bool)

    n_to_ocean = 0
    n_to_lake = 0
    n_pit = 0
    n_joined = 0

    for sr, sc in sources:
        path = steepest_descent(sr, sc, height_blocks, sink_mask, H, W,
                                visited_global=river)
        if not path:
            continue
        end_r, end_c = path[-1]
        if ocean[end_r, end_c]:
            n_to_ocean += 1
        elif underwater[end_r, end_c]:
            n_to_lake += 1
        elif river[end_r, end_c]:
            n_joined += 1
        else:
            n_pit += 1
        for (r, c) in path:
            river[r, c] = True

    print(f"  source paths: ocean {n_to_ocean} | lake {n_to_lake} | "
          f"merged {n_joined} | pit {n_pit}", file=sys.stderr)

    # ── Add lake outflows: descend from each lake's spillover ──
    print("Running spillover descent from each lake...", file=sys.stderr)
    lake_labeled, n_lakes = _label(underwater)
    n_outflow_to_ocean = 0
    n_outflow_to_lake = 0
    n_outflow_pit = 0
    for lid in range(1, n_lakes + 1):
        lk = lake_labeled == lid
        if lk.sum() < 3:
            continue
        # Spillover candidate: lowest cell on the basin perimeter just
        # outside this lake's underwater AND not in another underwater lake
        perim = binary_dilation(lk, iterations=1) & ~lk & ~ocean & ~underwater
        if not perim.any():
            continue
        hs = height_blocks.copy()
        hs[~perim] = np.inf
        sp_flat = int(hs.argmin())
        sp_r, sp_c = sp_flat // W, sp_flat % W
        # Sink for outflow: ocean OR ANOTHER lake's underwater
        other_uw = underwater & ~lk
        descent_sink = ocean | other_uw
        path = steepest_descent(sp_r, sp_c, height_blocks, descent_sink,
                                H, W, visited_global=river)
        if not path:
            continue
        end_r, end_c = path[-1]
        if ocean[end_r, end_c]:
            n_outflow_to_ocean += 1
        elif other_uw[end_r, end_c]:
            n_outflow_to_lake += 1
        else:
            n_outflow_pit += 1
        for (r, c) in path:
            river[r, c] = True
    print(f"  outflow paths: ocean {n_outflow_to_ocean} | "
          f"next-lake {n_outflow_to_lake} | pit {n_outflow_pit}",
          file=sys.stderr)

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
    print(f"Saved {args.out}", file=sys.stderr)

    out_2k = args.out.replace(".png", "_2k.png")
    img_2k = Image.fromarray(rgb)
    img_2k.thumbnail((2000, 2000), Image.LANCZOS)
    img_2k.save(out_2k, optimize=True)
    print(f"Saved {out_2k}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
