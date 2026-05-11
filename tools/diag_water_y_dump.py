"""tools/diag_water_y_dump.py — S81 step 1 diagnostic for water_y issues.

Runs the pipeline up to and including river_carver_v2 for one tile (default
51,53), captures the carver's outputs (surface_y_carved, water_y_field,
river_meta), and dumps several visualizations + a per-cell CSV-style summary
of suspicious cells.

Goal: identify the root cause of:
  1. Noisy bits sitting outside or above the trough (phantom water cells)
  2. Cross-section water_y variation (water plateau breaks across diameter)
  3. Lake-river junction water_y mismatches

Usage:
  py tools/diag_water_y_dump.py --tile-x 51 --tile-z 53

Outputs (under memory/diag_water_y/):
  - water_y_field.png            : heatmap of water_y per cell
  - water_y_minus_skel.png       : water_y - water_y_at_nearest_skeleton (cross-section variance)
  - phantom_water.png            : red where water_y > carved_surface_y + 1 (water above terrain)
  - depth_at_cell.png            : carve depth field (sigmoid output)
  - footprint_vs_carved.png      : footprint=blue, actual carve dent=green
  - summary.txt                  : counts of phantom cells, max variance, etc.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/thresholds.json")
    p.add_argument("--masks", default="masks/")
    p.add_argument("--tile-x", type=int, default=51)
    p.add_argument("--tile-z", type=int, default=53)
    p.add_argument("--out", default="memory/diag_water_y")
    args = p.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    import json
    cfg = json.loads(Path(args.config).read_text())

    import core.tile_streamer as core_tile_stream
    import core.biome_assignment as core_biome_assign
    import core.column_generator as core_col_gen
    import core.river_carver_v2 as core_river
    import core.noise_fields as core_noise
    from core.hydro_region_overlay import apply_hydro_region_overlay
    from core.gaea_gap_sampler import build_gap_config

    masks_dir = Path(args.masks)
    tile_sz = 512
    col_off = args.tile_x * tile_sz
    row_off = args.tile_z * tile_sz
    w = h = tile_sz

    print(f"[diag] tile=({args.tile_x},{args.tile_z}) col_off={col_off} row_off={row_off}")
    print("[diag] reading masks…")

    noise = core_noise.load_noise_generators(args.config)
    gap_cfg = build_gap_config(cfg.get("gaea_gaps", {}), masks_dir)
    masks = core_tile_stream.read_tile(
        masks_dir=masks_dir, col_off=col_off, row_off=row_off,
        width=w, height=h, gap_config=gap_cfg,
    )
    print("[diag] applying hydro_region_overlay…")
    apply_hydro_region_overlay(masks, masks_dir, col_off, row_off, w)

    # Capture carve depth field BEFORE the carver consumes it (for diag).
    # hydro_depth in masks is normalised float [0,1]; multiply by 255 to
    # recover the carve depth in MC blocks (matches what carver does).
    depth_at_cell = np.zeros((h, w), dtype=np.float32)
    if masks.get("hydro_depth") is not None:
        depth_at_cell = masks["hydro_depth"].astype(np.float32) * 255.0

    print("[diag] biome assignment…")
    biome_grid = core_biome_assign.assign_biomes(
        height_tile=masks["height"], slope_tile=masks["slope"],
        flow_tile=masks["flow"], erosion_tile=masks["erosion"],
        override_tile=masks["override"], noise_fields=noise, cfg=cfg,
    )

    print("[diag] column generation…")
    height_u16 = np.round(masks["height"] * 65535.0).astype(np.uint16)
    surface_y = core_col_gen.generate_columns(
        height_tile=height_u16, slope_tile=masks["slope"],
        biome_grid=biome_grid, shore_tile=masks["shore"],
        noise_fields=noise, cfg=cfg, tile_x=args.tile_x, tile_y=args.tile_z,
    )
    pre_carve_y = surface_y.copy()

    print("[diag] river carve…")
    surface_y_carved, river_meta, conn_mask, water_y_field = core_river.carve_rivers(
        surface_y=surface_y, flow_tile=masks["flow"], river_tile=masks["river"],
        cfg=cfg,
        hydro_order=masks.get("hydro_order"),
        hydro_width=masks.get("hydro_width"),
        hydro_depth=masks.get("hydro_depth"),
        hydro_lake=masks.get("hydro_lake"),
        hydro_lkdep=masks.get("hydro_lkdep"),
        hydro_lake_wl=masks.get("hydro_lake_wl"),
        hydro_centerline=masks.get("hydro_centerline"),
        height_norm=masks["height"],
        masks_dir=masks_dir,
        tile_x=args.tile_x, tile_z=args.tile_z,
    )

    # Save raw arrays for further inspection.
    np.savez_compressed(out_dir / "arrays.npz",
                        surface_y_pre=pre_carve_y,
                        surface_y_carved=surface_y_carved,
                        water_y_field=water_y_field,
                        river_meta=river_meta,
                        depth_at_cell=depth_at_cell,
                        hydro_centerline=masks.get("hydro_centerline", np.zeros((h, w), dtype=np.float32)),
                        hydro_lake=(masks.get("hydro_lake", np.zeros((h, w), dtype=np.float32)) > 0).astype(np.uint8))
    print(f"[diag] saved arrays.npz ({(out_dir / 'arrays.npz').stat().st_size//1024} KB)")

    #---──────────────────────────────────────────────────────────────────
    # Analysis 1: Phantom water cells
    # water_y > surface_y + 1 → water sits above bank
    #---──────────────────────────────────────────────────────────────────
    has_water = water_y_field > 0
    air_above_terrain = water_y_field - surface_y_carved.astype(np.int16)
    phantom = has_water & (air_above_terrain > 1)
    impossible = has_water & (air_above_terrain < 0)  # water below carved surface
    n_phantom = int(phantom.sum())
    n_impossible = int(impossible.sum())
    n_water = int(has_water.sum())

    #---──────────────────────────────────────────────────────────────────
    # Analysis 2: Cross-section water_y variance
    # For each carved cell, look at neighbors within radius=8 in the
    # direction perpendicular to the local centerline. If their water_y
    # differs by >0, the cross-section isn't flat.
    #
    # Simpler proxy: scipy.ndimage.standard_deviation in a 5x5 window
    # of water_y restricted to has_water cells. High std → cross-section
    # variation. We use a generic 5x5 window (no flow-direction).
    #---──────────────────────────────────────────────────────────────────
    from scipy.ndimage import generic_filter
    masked_wy = np.where(has_water, water_y_field.astype(np.float32), np.nan)

    def _local_range(window):
        valid = window[~np.isnan(window)]
        if valid.size < 2:
            return 0.0
        return float(valid.max() - valid.min())

    print("[diag] computing local water_y variance (this can take ~30s)…")
    local_range = generic_filter(masked_wy, _local_range, size=5, mode="constant",
                                 cval=np.nan)
    local_range = np.nan_to_num(local_range, nan=0.0)
    n_uneven = int(((local_range > 0) & has_water).sum())

    #---──────────────────────────────────────────────────────────────────
    # Analysis 3: Lake-river boundary water_y mismatch
    # Find cells within 5 of any lake cell (hydro_lake>0) where water_y
    # differs from any lake water_y in that neighborhood.
    #---──────────────────────────────────────────────────────────────────
    lake_mask = masks.get("hydro_lake")
    lake_bool = (lake_mask > 0) if lake_mask is not None else np.zeros((h, w), dtype=bool)
    lake_wl = masks.get("hydro_lake_wl")
    n_lake_mismatch = 0
    if lake_bool.any() and lake_wl is not None:
        # Lake water_y in MC = floor of (height_norm_to_mc using lake_wl)
        from core.river_carver_v2 import _height_norm_to_mc_y as _h2mc
        lake_water_y_full = np.floor(_h2mc(lake_wl, cfg)).astype(np.int16)
        from scipy.ndimage import binary_dilation
        near_lake = binary_dilation(lake_bool, iterations=5) & ~lake_bool
        # river cells near lake
        boundary_river = near_lake & has_water
        if boundary_river.any():
            # mismatch = river water_y differs from nearest lake water_y
            rb_y = water_y_field[boundary_river].astype(np.int16)
            # average lake water_y in window — for diag, use scalar
            # (one lake at this tile). Look up unique lake values.
            lake_y_vals = np.unique(lake_water_y_full[lake_bool])
            # mismatch counter: river cell where its water_y differs
            # from ANY lake water_y by >1
            mm = np.zeros_like(rb_y, dtype=bool)
            for ly in lake_y_vals:
                mm |= (np.abs(rb_y - ly) > 1)
            n_lake_mismatch = int(mm.sum())

    #---──────────────────────────────────────────────────────────────────
    # Render PNGs
    #---──────────────────────────────────────────────────────────────────
    from PIL import Image
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def _save_heatmap(arr, path, title, cmap="viridis", vmin=None, vmax=None):
        fig, ax = plt.subplots(figsize=(6, 6), dpi=100)
        im = ax.imshow(arr, cmap=cmap, vmin=vmin, vmax=vmax)
        ax.set_title(title)
        plt.colorbar(im, ax=ax, fraction=0.046)
        ax.axis("off")
        fig.tight_layout()
        fig.savefig(path, bbox_inches="tight")
        plt.close(fig)

    # 1. water_y_field heatmap
    wy_show = water_y_field.astype(np.float32)
    wy_show[water_y_field <= 0] = np.nan
    _save_heatmap(wy_show, out_dir / "water_y_field.png",
                  f"water_y_field ({n_water} cells)")

    # 2. depth_at_cell heatmap
    _save_heatmap(depth_at_cell, out_dir / "depth_at_cell.png",
                  "depth_at_cell (carve depth, blocks)",
                  cmap="magma", vmin=0)

    # 3. water_y - surface_y (positive = water above terrain)
    diff = (water_y_field.astype(np.int16) - surface_y_carved.astype(np.int16))
    diff_show = diff.astype(np.float32)
    diff_show[water_y_field <= 0] = np.nan
    _save_heatmap(diff_show, out_dir / "water_above_terrain.png",
                  f"water_y - surface_y_carved (phantoms: {n_phantom}, impossible: {n_impossible})",
                  cmap="RdBu_r", vmin=-3, vmax=3)

    # 4. local water_y range (cross-section variance proxy)
    lr_show = local_range.copy()
    lr_show[~has_water] = np.nan
    _save_heatmap(lr_show, out_dir / "water_y_local_range.png",
                  f"water_y range in 5x5 window ({n_uneven} uneven cells)",
                  cmap="hot", vmin=0, vmax=5)

    # 5. footprint vs carved-dent overlay
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[has_water] = (60, 100, 180)  # blue: water_y_field set
    actually_carved = (surface_y_carved < pre_carve_y) & has_water
    rgb[actually_carved] = (60, 180, 100)  # green: terrain actually dropped
    rgb[lake_bool] = (200, 200, 60)  # yellow: lake
    rgb[phantom] = (220, 60, 60)  # red: phantom water (above terrain)
    Image.fromarray(rgb).save(out_dir / "footprint_vs_carved.png")

    # 6. Summary text
    summary = []
    summary.append(f"Tile: ({args.tile_x},{args.tile_z})")
    summary.append(f"Total cells with water_y_field set: {n_water}  ({100*n_water/(h*w):.1f}%)")
    summary.append(f"Total lake cells:                   {int(lake_bool.sum())}")
    summary.append("")
    summary.append("---Phantom water analysis (Issue 1)---")
    summary.append(f"Phantom cells (water_y > surface_y + 1): {n_phantom}")
    summary.append(f"Impossible cells (water_y < surface_y):  {n_impossible}")
    if n_phantom > 0:
        rows, cols = np.where(phantom)
        summary.append(f"Phantom max water_above_terrain: {int(diff[phantom].max())}")
        summary.append(f"Phantom mean water_above_terrain: {float(diff[phantom].mean()):.2f}")
        summary.append(f"Sample phantom cells (first 10):")
        for i in range(min(10, len(rows))):
            r, c = rows[i], cols[i]
            summary.append(
                f"  ({r:3d},{c:3d}) water_y={water_y_field[r,c]:3d} "
                f"surface={surface_y_carved[r,c]:3d} "
                f"depth={depth_at_cell[r,c]:.2f} "
                f"pre_carve={pre_carve_y[r,c]:3d}"
            )
    summary.append("")
    summary.append("---Cross-section variance (Issue 2)---")
    summary.append(f"Cells with non-zero local water_y range (5x5): {n_uneven}")
    if n_uneven > 0:
        summary.append(f"Max local range: {float(local_range.max()):.0f} blocks")
        summary.append(f"Mean local range (uneven cells only): {float(local_range[(local_range>0)&has_water].mean()):.2f}")
    summary.append("")
    summary.append("---Lake-river junction (Issue 3)---")
    summary.append(f"Lake-adjacent river cells with mismatched water_y: {n_lake_mismatch}")
    if lake_bool.any() and lake_wl is not None:
        summary.append(f"Lake water_y values present: {lake_y_vals.tolist()}")
        if has_water.any():
            summary.append(f"River water_y range: {int(water_y_field[has_water & ~lake_bool].min())}–{int(water_y_field[has_water & ~lake_bool].max())}")
    summary.append("")
    summary.append("---Carve depth (Issue 4 baseline)---")
    if depth_at_cell.any():
        summary.append(f"Max depth_at_cell: {float(depth_at_cell.max()):.2f} blocks (current _CARVE_MAX_DEPTH=4)")
        summary.append(f"Mean depth where >0: {float(depth_at_cell[depth_at_cell > 0].mean()):.2f}")
        summary.append(f"Mean carved drop (pre - carved): {float((pre_carve_y - surface_y_carved)[has_water].mean()):.2f} blocks")

    summary_str = "\n".join(summary)
    (out_dir / "summary.txt").write_text(summary_str, encoding="utf-8")
    print()
    print(summary_str)
    print()
    print(f"[diag] outputs in {out_dir}/")


if __name__ == "__main__":
    main()
