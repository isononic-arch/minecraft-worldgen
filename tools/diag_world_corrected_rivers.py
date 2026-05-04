"""
diag_world_corrected_rivers.py — "Option B" rerun:
Take the current centerlines, programmatically extend each one through
orange basin shore cells until it touches blue (terrain-intersection)
underwater. Apply a small per-cell meander perturbation. Render the
corrected world topdown so we can see the geometry the in-game carver
SHOULD be receiving.

Pipeline (all done at 1:8 working scale, no MCA touched):
    1. Read height, hydro_lake (basin), hydro_lake_wl, hydro_centerline
       at 1:8 working scale.
    2. real_lake_mask = basin & (height < lake_wl)   (blue)
       basin_dry     = basin & ~real_lake_mask        (orange)
    3. Extend centerlines: for each centerline cell adjacent to basin_dry,
       trace a Bresenham line from that cell through basin_total to the
       nearest underwater cell. All cells on that line become part of
       extended_centerline.
    4. Add subtle meander: per-cell ±1 displacement using opensimplex.
       Smooth wavelength (~64 1:8 cells = 512 MC blocks) so it looks
       like gentle bends, not zigzag. Reserved smoothing for in-game.
    5. Render hillshade + real lake + corrected rivers.

Usage:
    py tools/diag_world_corrected_rivers.py --out memory/world_corrected_rivers.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import distance_transform_edt, binary_dilation

SEA_LEVEL_RAW = 17050
SCALE = 8


def bresenham(r0: int, c0: int, r1: int, c1: int):
    """Return all (r, c) cells from (r0,c0) to (r1,c1) inclusive."""
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    p.add_argument("--meander-amp", type=float, default=1.5,
                   help="Max perpendicular displacement in 1:8 cells")
    p.add_argument("--meander-wavelength", type=int, default=64,
                   help="Noise wavelength in 1:8 cells (gentle bends)")
    args = p.parse_args()

    masks_dir = Path(args.masks)

    print("Reading 50k masks at 1:8 working scale...", file=sys.stderr)

    def _read_downscaled(name):
        with rasterio.open(str(masks_dir / f"{name}.tif")) as src:
            return src.read(1, out_shape=(src.height // SCALE, src.width // SCALE),
                           resampling=rasterio.enums.Resampling.nearest)

    height = _read_downscaled("height")
    lake_id = _read_downscaled("hydro_lake")
    lake_wl_norm = _read_downscaled("hydro_lake_wl").astype(np.float32)
    centerline = _read_downscaled("hydro_centerline") > 0

    H, W = height.shape
    print(f"  shape: {H}x{W}", file=sys.stderr)

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

    print(f"  basin total: {basin.sum():,} | "
          f"underwater: {underwater.sum():,} | "
          f"dry shore: {basin_dry.sum():,}",
          file=sys.stderr)
    print(f"  centerline cells: {centerline.sum():,}", file=sys.stderr)

    # ── Step 3: extend centerlines through basin_dry to underwater ──
    print("Extending centerlines through basin shore to real water...",
          file=sys.stderr)
    # Cells of interest: centerline cells INSIDE basin_dry (these are
    # already on shore, need to be pushed into water). And centerline
    # cells in non-basin terrain that are ADJACENT to basin (shore-facing
    # edges, the carver currently terminates these here).
    cl_dry = centerline & basin_dry
    # 3x3 dilation of basin → cells just outside basin
    basin_dil = binary_dilation(basin, iterations=1)
    cl_shore_facing = centerline & basin_dil & ~basin

    targets_for_extension = cl_dry | cl_shore_facing
    print(f"  cl in dry shore: {cl_dry.sum():,} | "
          f"cl shore-facing: {cl_shore_facing.sum():,}",
          file=sys.stderr)

    # For each target cell, find nearest underwater cell
    if underwater.any():
        _, idx = distance_transform_edt(~underwater, return_indices=True)
        target_r_arr = idx[0]
        target_c_arr = idx[1]
    else:
        target_r_arr = target_c_arr = None

    extended = centerline.copy()
    if target_r_arr is not None:
        sr_arr, sc_arr = np.where(targets_for_extension)
        n_lines = 0
        for sr, sc in zip(sr_arr, sc_arr):
            tr = int(target_r_arr[sr, sc])
            tc = int(target_c_arr[sr, sc])
            line = bresenham(int(sr), int(sc), tr, tc)
            for (r, c) in line:
                if 0 <= r < H and 0 <= c < W:
                    extended[r, c] = True
            n_lines += 1
        print(f"  Drew {n_lines} extension lines", file=sys.stderr)

    # ── Step 4: subtle meander on extended centerlines ──
    print("Applying subtle meander to centerlines...", file=sys.stderr)
    try:
        import opensimplex as _ox
        ox = _ox.OpenSimplex(seed=0xC0DEC0D)
        ws = (np.arange(W) / args.meander_wavelength).astype(np.float64)
        hs = (np.arange(H) / args.meander_wavelength).astype(np.float64)
        # Two independent noise channels for x and y displacement
        nx = ox.noise2array(ws + 100.0, hs + 200.0).astype(np.float32)
        ny = ox.noise2array(ws + 300.0, hs + 400.0).astype(np.float32)
    except ImportError:
        nx = np.zeros((H, W), dtype=np.float32)
        ny = np.zeros((H, W), dtype=np.float32)
        print("  (opensimplex missing — meander disabled)", file=sys.stderr)

    # Apply meander: each centerline cell becomes a displaced cell. Keep
    # ORIGINAL centerline too (so connectivity isn't broken — the
    # meander just adds nearby companion cells to thicken/wave the
    # path). For a pure wave effect, we'd reroute paths; for visualisation
    # this approximates the look at no risk to connectivity.
    meandered = extended.copy()
    cl_r, cl_c = np.where(extended)
    AMP = args.meander_amp
    drs = np.round(ny[cl_r, cl_c] * AMP).astype(np.int32)
    dcs = np.round(nx[cl_r, cl_c] * AMP).astype(np.int32)
    new_r = np.clip(cl_r + drs, 0, H - 1)
    new_c = np.clip(cl_c + dcs, 0, W - 1)
    meandered[new_r, new_c] = True
    print(f"  centerline before extend: {centerline.sum():,}", file=sys.stderr)
    print(f"  centerline after extend:  {extended.sum():,}", file=sys.stderr)
    print(f"  centerline + meander:     {meandered.sum():,}", file=sys.stderr)

    # ── Step 5: render ──
    print("Rendering...", file=sys.stderr)

    norm = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height_blocks)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1)

    img = base.copy()
    img[ocean] = [0.13, 0.27, 0.49]
    img[underwater] = [0.20, 0.50, 0.80]   # blue real lake
    # NOTE: NO orange — basin_dry is dropped entirely from this view
    # because the corrected pipeline doesn't treat it as a water body.
    img[meandered] = [0.10, 0.40, 0.85]    # blue corrected rivers

    rgb = (img * 255).astype(np.uint8)
    Image.fromarray(rgb).save(args.out, optimize=True)
    print(f"Saved {args.out}  ({rgb.shape[1]}x{rgb.shape[0]})", file=sys.stderr)

    out_2k = args.out.replace(".png", "_2k.png")
    img_2k = Image.fromarray(rgb)
    img_2k.thumbnail((2000, 2000), Image.LANCZOS)
    img_2k.save(out_2k, optimize=True)
    print(f"Saved {out_2k}  ({img_2k.size[0]}x{img_2k.size[1]})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
