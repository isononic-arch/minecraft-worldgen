"""
diag_river_bed_depth.py — Inspect bed cache + painted river depth at the
broken-river tiles.  Confirms whether painted river cells at/below sea
level kill `depth_at_cell > 0.05`, which is the gate that suppresses
water_y_field assignment in river_carver_v2.py:1014.

Reads:
  masks/_bed_cache_v19.pkl   (river_bed_8k, river_water_y_8k, paint_smooth_8k)
  masks/height.tif (50k)     to compute surface_y per pixel via _LUT
  Each tile's painted-river mask via apply_hydro_region_overlay

For each problem tile + a known-good control:
  - n_painted_pixels (50k)
  - bed_y stats at painted cells (from bed cache, mapped to MC Y)
  - surface_y stats at painted cells (from height + spline)
  - depth = surface_y - bed_y, stats
  - n_pixels with depth > 0.05 (would carve)
  - n_pixels with depth <= 0.05 (would NOT carve -> water_y_field stays at -1)
  - n_pixels above SEA_LEVEL, at sea level, below sea level
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np

TILE_SIZE = 512
SEA_LEVEL = 63
WORLD_50K = 50000
SCALE_8K = 8192 / 50000.0


def load_bed_cache():
    p = Path("masks/_bed_cache_v19.pkl")
    print(f"Loading bed cache ({p.stat().st_size / 1024**2:.0f} MB)...")
    with open(p, "rb") as f:
        d = pickle.load(f)
    return d


def world_to_8k(x_50k: int, z_50k: int) -> tuple[int, int]:
    return int(x_50k * SCALE_8K), int(z_50k * SCALE_8K)


def analyse_tile(
    tile_x: int,
    tile_z: int,
    bed_cache: dict,
    cfg: dict,
    masks_dir: Path,
) -> None:
    from core.tile_streamer import read_tile
    from core.hydro_region_overlay import apply_hydro_region_overlay
    from core.gaea_gap_sampler import build_gap_config
    from core import column_generator as col_gen

    col_off = tile_x * TILE_SIZE
    row_off = tile_z * TILE_SIZE
    w = h = TILE_SIZE

    # Read masks + apply hydro_region overlay so masks["hydro_centerline"] is set
    gap_cfg = build_gap_config(cfg.get("gaea_gaps", {}), masks_dir)
    masks = read_tile(
        masks_dir=masks_dir,
        col_off=col_off,
        row_off=row_off,
        width=w,
        height=h,
        gap_config=gap_cfg,
    )
    apply_hydro_region_overlay(masks, masks_dir, col_off, row_off, w)

    # Map this tile to 8k bed-cache coords
    x0_8k, z0_8k = world_to_8k(col_off, row_off)
    x1_8k, z1_8k = world_to_8k(col_off + w, row_off + h)
    x1_8k = min(x1_8k + 1, 8192)
    z1_8k = min(z1_8k + 1, 8192)

    river_bed_8k = bed_cache.get("river_bed_8k")
    river_water_y_8k = bed_cache.get("river_water_y_8k")
    paint_smooth_8k = bed_cache.get("paint_smooth_8k")

    bed_tile_8k = river_bed_8k[z0_8k:z1_8k, x0_8k:x1_8k] if river_bed_8k is not None else None
    wy_tile_8k = river_water_y_8k[z0_8k:z1_8k, x0_8k:x1_8k] if river_water_y_8k is not None else None
    paint_tile_8k = paint_smooth_8k[z0_8k:z1_8k, x0_8k:x1_8k] if paint_smooth_8k is not None else None

    print(f"\n=== tile ({tile_x},{tile_z}) ===")
    print(f"  8k region: rows {z0_8k}..{z1_8k}, cols {x0_8k}..{x1_8k} "
          f"({z1_8k-z0_8k} x {x1_8k-x0_8k})")

    if paint_tile_8k is not None:
        paint_painted_8k = paint_tile_8k > 0.01
        print(f"  8k paint_smooth: max={paint_tile_8k.max():.3f}  "
              f"painted_px(>0.01)={int(paint_painted_8k.sum())}")
    if bed_tile_8k is not None:
        print(f"  8k river_bed: min={bed_tile_8k.min():.3f}  "
              f"max={bed_tile_8k.max():.3f}  "
              f"mean(nonzero)={bed_tile_8k[bed_tile_8k > 0].mean() if (bed_tile_8k>0).any() else 0:.3f}")
    if wy_tile_8k is not None:
        wy_set = wy_tile_8k > 0
        print(f"  8k river_water_y: max={wy_tile_8k.max():.3f}  "
              f"set_px={int(wy_set.sum())}  "
              f"mean(set)={wy_tile_8k[wy_set].mean() if wy_set.any() else 0:.3f}")

    # Compute 50k surface_y via the same _LUT used in run_pipeline.py:807-810
    height_norm = masks["height"]
    h_raw_int = np.clip(
        (height_norm * 65535.0).astype(np.int32), 0, 65535
    )
    surface_y_50k = col_gen._LUT[h_raw_int].astype(np.int32)

    # Painted-river mask from the hydro_centerline tile (set by overlay)
    hydro_cl = masks.get("hydro_centerline")
    if hydro_cl is None:
        print("  No hydro_centerline in masks — overlay didn't run?")
        return
    painted = np.asarray(hydro_cl) > 0
    n_painted = int(painted.sum())
    print(f"  50k painted (hydro_centerline > 0): {n_painted} pixels")
    if n_painted == 0:
        print("  -> no painted rivers on this tile at 50k")
        return

    # Per-pixel bed_y from bed cache, mapped to MC Y
    # The carver uses bed_cache values as MC-Y directly per its convention
    # (river_bed_8k stores MC-Y space already after S83 v17).
    # We need to look up bed_y for each 50k painted pixel via nearest sample.
    # Make a 50k bed_y view by nearest-sampling the 8k cache into the tile.
    if bed_tile_8k is None:
        print("  No bed cache values — cannot compute depth")
        return
    if bed_tile_8k.size == 0:
        print("  bed cache slice is empty")
        return

    rows_50k = np.arange(h)
    cols_50k = np.arange(w)
    # Map 50k -> 8k indices within the tile slice
    row_idx_8k = np.clip(
        (rows_50k * SCALE_8K).astype(np.int32) - z0_8k,
        0, bed_tile_8k.shape[0] - 1,
    )
    col_idx_8k = np.clip(
        (cols_50k * SCALE_8K).astype(np.int32) - x0_8k,
        0, bed_tile_8k.shape[1] - 1,
    )
    bed_y_50k = bed_tile_8k[row_idx_8k[:, None], col_idx_8k[None, :]]
    wy_50k = wy_tile_8k[row_idx_8k[:, None], col_idx_8k[None, :]] if wy_tile_8k is not None else None

    bed_at_painted = bed_y_50k[painted]
    surface_at_painted = surface_y_50k[painted]
    depth_at_painted = surface_at_painted.astype(np.float32) - bed_at_painted.astype(np.float32)
    above_sea_at_painted = surface_at_painted > SEA_LEVEL

    print(f"  painted-cell bed_y:    min={bed_at_painted.min():.2f}  "
          f"mean={bed_at_painted.mean():.2f}  max={bed_at_painted.max():.2f}")
    print(f"  painted-cell surface_y: min={surface_at_painted.min()}  "
          f"mean={surface_at_painted.mean():.2f}  max={surface_at_painted.max()}")
    print(f"  painted-cell depth (surface-bed):  "
          f"min={depth_at_painted.min():.2f}  "
          f"mean={depth_at_painted.mean():.2f}  "
          f"max={depth_at_painted.max():.2f}")

    n_above_sea = int(above_sea_at_painted.sum())
    n_at_sea = int((surface_at_painted == SEA_LEVEL).sum())
    n_below_sea = int((surface_at_painted < SEA_LEVEL).sum())
    print(f"  painted-cell elev: above_sea={n_above_sea}  "
          f"at_sea={n_at_sea}  below_sea={n_below_sea}")

    n_carved = int((depth_at_painted > 0.05).sum())
    n_not_carved = int((depth_at_painted <= 0.05).sum())
    print(f"  >>> depth>0.05 (CARVES, sets water_y): {n_carved}")
    print(f"  >>> depth<=0.05 (NO CARVE, no water_y):  {n_not_carved}")
    if n_painted > 0:
        pct = 100.0 * n_not_carved / n_painted
        print(f"  >>> {pct:.1f}% of painted cells fail the carve gate")

    if wy_50k is not None:
        wy_at_painted = wy_50k[painted]
        n_wy_set = int((wy_at_painted > 0).sum())
        print(f"  river_water_y_8k set on {n_wy_set} of {n_painted} painted cells")


def main() -> int:
    import json
    cfg_path = Path("config/thresholds.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    bed_cache = load_bed_cache()
    print("\nBed cache contents:")
    for k, v in bed_cache.items():
        if isinstance(v, np.ndarray):
            print(f"  {k}: {v.shape} {v.dtype}  "
                  f"min={float(v.min()) if v.size else '-':<8}  "
                  f"max={float(v.max()) if v.size else '-':<8}")
        else:
            print(f"  {k}: {type(v).__name__}")
    masks_dir = Path("masks/")
    tiles = [(13, 82), (51, 53), (33, 7), (60, 69)]  # last is a working control
    for tx, tz in tiles:
        try:
            analyse_tile(tx, tz, bed_cache, cfg, masks_dir)
        except Exception as exc:
            print(f"\n=== tile ({tx},{tz}) FAILED: {exc!r} ===")
            import traceback
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
