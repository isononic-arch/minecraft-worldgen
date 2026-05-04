"""
diag_v34_compare.py — Side-by-side preview of two carve approaches:

  A. Skeleton + EDT-width only      (no paint floor, smooth Bresenham)
  B. Skeleton + EDT-width + paint   (bilinear paint mask union → guaranteed
                                      coverage with smoothed boundary)

Usage:
    py tools/diag_v34_compare.py --tile-x 51 --tile-z 53 \
        --out memory/v34_AB_compare.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import distance_transform_edt, map_coordinates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import hydro_region_overlay as hro
from core.hydro_region_overlay import apply_hydro_region_overlay

TILE = 512
PAINT = 8192
WORLD = 50000
SEA = 17050


def render_carve(masks, h_raw, lake_mask, ocean):
    cl = masks["hydro_centerline"] > 0
    w = masks["hydro_width"]
    if cl.any():
        dist, idx = distance_transform_edt(~cl, return_indices=True)
        carve = dist <= w[idx[0], idx[1]]
    else:
        carve = np.zeros((TILE, TILE), dtype=bool)

    gaea_in = np.array([0, SEA, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    hb = np.interp(h_raw.ravel(), gaea_in, mc_y_out
                    ).reshape(h_raw.shape).astype(np.float32)
    norm = np.clip((hb + 64)/(448+64), 0, 1)
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(hb)
    light = np.clip(0.5 + 0.5 * (-gx-gy)/30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1) * 0.5
    base[ocean] = [0.05, 0.10, 0.20]
    base[lake_mask] = [0.20, 0.55, 0.75]
    base[carve] = [0.95, 0.60, 0.20]
    base[cl]    = [1.0, 0.95, 0.10]
    return (base * 255).astype(np.uint8), int(cl.sum()), int(carve.sum())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tile-x", type=int, default=51)
    p.add_argument("--tile-z", type=int, default=53)
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    masks_dir = Path(args.masks)
    tx, tz = args.tile_x, args.tile_z
    col_off, row_off = tx*TILE, tz*TILE
    win = Window(col_off, row_off, TILE, TILE)
    with rasterio.open(masks_dir / "height.tif") as src:
        h_raw = src.read(1, window=win)
    with rasterio.open(masks_dir / "slope.tif") as src:
        sl = src.read(1, window=win).astype(np.float32)
    if sl.max() > 1.5:
        sl /= 65535.0
    with rasterio.open(masks_dir / "hydro_lake.tif") as src:
        lk = src.read(1, window=win)
    with rasterio.open(masks_dir / "hydro_lake_wl.tif") as src:
        wl = src.read(1, window=win).astype(np.float32)
    if wl.max() <= 1.5:
        wl *= 65535.0
    h_norm = h_raw.astype(np.float32) / 65535.0

    gaea_in = np.array([0, SEA, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    hb = np.interp(h_raw.ravel(), gaea_in, mc_y_out
                    ).reshape(h_raw.shape).astype(np.float32)
    lake_wl_mc = np.interp(wl.ravel(), gaea_in, mc_y_out
                            ).reshape(wl.shape).astype(np.float32)
    ocean = h_raw <= SEA
    lake_mask = (lk > 0) & (hb < lake_wl_mc) & ~ocean

    def fresh_masks():
        return {
            "hydro_centerline": np.zeros((TILE, TILE), dtype=np.float32),
            "hydro_order": np.zeros((TILE, TILE), dtype=np.float32),
            "hydro_width": np.zeros((TILE, TILE), dtype=np.float32),
            "hydro_depth": np.zeros((TILE, TILE), dtype=np.float32),
            "hydro_lake": np.zeros((TILE, TILE), dtype=np.float32),
            "hydro_lkdep": np.zeros((TILE, TILE), dtype=np.float32),
            "height": h_norm, "slope": sl,
        }

    # ── Approach A: pure skeleton + EDT width (current code) ──
    print("Approach A (skeleton + EDT-width, no paint floor)...",
          file=sys.stderr)
    masks_A = fresh_masks()
    apply_hydro_region_overlay(masks_A, masks_dir, col_off, row_off, TILE)
    img_A, cl_A, carve_A = render_carve(masks_A, h_raw, lake_mask, ocean)
    print(f"  centerline={cl_A:,} carve={carve_A:,}", file=sys.stderr)

    # ── Approach B: skeleton + EDT-width + bilinear paint floor ──
    # Monkey-patch the rasterizer to add the bilinear paint mask floor
    print("Approach B (skeleton + bilinear paint mask floor)...",
          file=sys.stderr)
    original_fn = hro._rasterize_river_edges_tile

    def _rasterize_with_paint_floor(co, ro, ts):
        out, width = original_fn(co, ro, ts)
        if hro._river_width_8k_cache is None:
            return out, width
        scale_to_8k = hro._REGION_PX / hro._WORLD_PX
        rows_f = (np.arange(ts, dtype=np.float64) + ro) * scale_to_8k
        cols_f = (np.arange(ts, dtype=np.float64) + co) * scale_to_8k
        rg, cg = np.meshgrid(rows_f, cols_f, indexing="ij")
        coords = np.stack([rg, cg])
        # Bilinear sample of binary paint mask
        paint_mask_8k = (hro._river_width_8k_cache > 0).astype(np.float32)
        paint_bilinear_f = map_coordinates(paint_mask_8k, coords, order=1,
                                            mode="constant", cval=0.0)
        paint_bilinear = paint_bilinear_f > 0.5
        edt_bilinear_8k = map_coordinates(hro._river_width_8k_cache, coords,
                                          order=1, mode="constant", cval=0.0)
        scale_8k_to_50k = hro._WORLD_PX / hro._REGION_PX
        edt_blocks = edt_bilinear_8k * scale_8k_to_50k
        if paint_bilinear.any():
            out |= paint_bilinear
            width = np.maximum(width, edt_blocks * paint_bilinear.astype(np.float32))
        return out, width

    hro._rasterize_river_edges_tile = _rasterize_with_paint_floor
    masks_B = fresh_masks()
    apply_hydro_region_overlay(masks_B, masks_dir, col_off, row_off, TILE)
    hro._rasterize_river_edges_tile = original_fn
    img_B, cl_B, carve_B = render_carve(masks_B, h_raw, lake_mask, ocean)
    print(f"  centerline={cl_B:,} carve={carve_B:,}", file=sys.stderr)

    fig, axes = plt.subplots(1, 2, figsize=(14, 7), facecolor="white")
    axes[0].imshow(img_A, interpolation="nearest")
    axes[0].set_title(
        f"A — Skeleton + EDT-width (no floor)\n"
        f"  cl={cl_A:,}  carve={carve_A:,}", fontsize=11)
    axes[1].imshow(img_B, interpolation="nearest")
    axes[1].set_title(
        f"B — Skeleton + bilinear paint floor\n"
        f"  cl={cl_B:,}  carve={carve_B:,}", fontsize=11)
    for ax in axes:
        ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(args.out, dpi=140, bbox_inches="tight")
    print(f"Saved {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
