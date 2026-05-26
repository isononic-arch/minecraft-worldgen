"""
diag_river_overlay_depth.py — Confirm whether apply_hydro_region_overlay
actually populates hydro_depth at the 3 broken tiles + the working control.

The carver gate that decides water_y_field is set per pixel:
    footprint = (depth_at_cell > 0.05) & ~lake_mask & above_sea
where:
    depth_at_cell = hydro_depth * 255.0   (line 1005 of river_carver_v2)

So `hydro_depth > 0.0002` (i.e., raw_u8 > 0.05 / 255 ≈ 1.96e-4) is the
real gate.  If overlay doesn't push hydro_depth past this for painted
cells, water_y_field never gets set.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np
import json

TILE_SIZE = 512


def analyse_tile(tile_x: int, tile_z: int, cfg: dict, masks_dir: Path) -> None:
    from core.tile_streamer import read_tile
    from core.hydro_region_overlay import apply_hydro_region_overlay
    from core.gaea_gap_sampler import build_gap_config

    col_off = tile_x * TILE_SIZE
    row_off = tile_z * TILE_SIZE
    w = h = TILE_SIZE

    gap_cfg = build_gap_config(cfg.get("gaea_gaps", {}), masks_dir)
    masks = read_tile(
        masks_dir=masks_dir,
        col_off=col_off,
        row_off=row_off,
        width=w,
        height=h,
        gap_config=gap_cfg,
    )
    # PRE-overlay snapshot
    hd_pre = masks.get("hydro_depth")
    hw_pre = masks.get("hydro_width")
    print(f"\n=== tile ({tile_x},{tile_z}) ===")
    if hd_pre is not None:
        print(f"  PRE  hydro_depth: nonzero={int((hd_pre > 0).sum())}  "
              f"max={float(hd_pre.max()):.4f}")
    if hw_pre is not None:
        print(f"  PRE  hydro_width: nonzero={int((hw_pre > 0).sum())}  "
              f"max={float(hw_pre.max()):.4f}")

    apply_hydro_region_overlay(masks, masks_dir, col_off, row_off, w)

    hd_post = masks.get("hydro_depth")
    hw_post = masks.get("hydro_width")
    hcl = masks.get("hydro_centerline")
    if hd_post is not None:
        print(f"  POST hydro_depth: nonzero={int((hd_post > 0).sum())}  "
              f">0.0002={int((hd_post > 0.0002).sum())}  "
              f"max={float(hd_post.max()):.4f}  "
              f"mean(nz)={float(hd_post[hd_post > 0].mean()) if (hd_post > 0).any() else 0:.4f}")
    if hw_post is not None:
        print(f"  POST hydro_width: nonzero={int((hw_post > 0).sum())}  "
              f"max={float(hw_post.max()):.4f}")
    if hcl is not None:
        print(f"  POST hydro_centerline: nonzero={int((hcl > 0).sum())}")

    # The actual carver gate:
    if hd_post is not None:
        carve_gate_px = int((hd_post * 255.0 > 0.05).sum())
        print(f"  >>> CARVE GATE (hydro_depth*255 > 0.05): "
              f"{carve_gate_px} pixels")

    # Also check lake masks
    lk = masks.get("hydro_lake")
    if lk is not None:
        lk_px = int((lk > 0).sum())
        print(f"  POST hydro_lake: nonzero={lk_px}")


def main() -> int:
    cfg = json.load(open("config/thresholds.json"))
    masks_dir = Path("masks/")
    tiles = [(13, 82), (51, 53), (33, 7), (60, 69)]
    for tx, tz in tiles:
        try:
            analyse_tile(tx, tz, cfg, masks_dir)
        except Exception as exc:
            print(f"\n=== tile ({tx},{tz}) FAILED: {exc!r} ===")
            import traceback
            traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
