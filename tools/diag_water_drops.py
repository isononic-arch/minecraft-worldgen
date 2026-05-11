"""
diag_water_drops.py -- pinpoint why painted cells don't show water in MCA.

Runs the full pipeline up to the point where river_water_y is computed, then
counts cells at each stage of the funnel:

  painted_centerline (apply_overlay set hydro_centerline > 0)
       |
       v above_sea filter
       |
       v ~lake_mask filter            <- terrain-intersection lake removes some
       |
       v water_y_field set in carver
       |
       v river_water_y > SEA_Y check  <- chunk_writer gate
       |
       v ACTUALLY GETS WATER PLACED

Reports counts at every step + classifies which cells dropped at which step.
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import tile_streamer, biome_assignment, river_carver_v2
from core.hydro_region_overlay import apply_hydro_region_overlay
import json

TILE = 512
SEA_LEVEL = 63
masks_dir = Path("C:/Users/nicho/minecraft-worldgen/masks")
cfg_path = Path("C:/Users/nicho/minecraft-worldgen/config/thresholds.json")
cfg = json.loads(cfg_path.read_text())

tx, tz = 51, 53
col_off, row_off = tx * TILE, tz * TILE

# Read masks (with hydro overlay applied)
masks = tile_streamer.read_tile(
    masks_dir=masks_dir, col_off=col_off, row_off=row_off,
    width=TILE, height=TILE,
)
apply_hydro_region_overlay(masks, masks_dir, col_off, row_off, TILE)

cl = masks["hydro_centerline"] > 0
print(f"STEP 1 -- hydro_centerline > 0 (painted+smoothed): {int(cl.sum()):,}")

# Build surface_y (NOT carved yet -- the same as what column_generator gives)
# Use height + spline to get surface_y
gaea_in = np.array([0, 17050, 45000, 65496], dtype=np.float64)
mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
h_raw = (masks["height"] * 65535.0)
surface_y = np.interp(h_raw.ravel(), gaea_in, mc_y_out
                      ).reshape(h_raw.shape).astype(np.int16)

above_sea = surface_y > SEA_LEVEL
print(f"STEP 2 -- above_sea (surface > {SEA_LEVEL}): {int(above_sea.sum()):,}")
print(f"STEP 2a -- cl & above_sea: {int((cl & above_sea).sum()):,}")

# Now carve_rivers. This computes lake_mask, footprint, water_y_field internally.
result = river_carver_v2.carve_rivers(
    surface_y=surface_y,
    flow_tile=masks["flow"],
    river_tile=masks.get("river", np.zeros_like(masks["flow"])),
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
    tile_x=tx,
    tile_z=tz,
)
carved_y, river_meta, conn_mask, water_y_field = result

print(f"\nSTEP 3 -- water_y_field > 0 (carver set water): "
      f"{int((water_y_field > 0).sum()):,}")
print(f"STEP 3a -- water_y_field > 0 AND painted: "
      f"{int(((water_y_field > 0) & cl).sum()):,}")

# How many painted cells DROPPED at the carver?
dropped_carver = cl & (water_y_field <= 0)
print(f"\nDROPPED at carver (painted but water_y_field<=0): "
      f"{int(dropped_carver.sum()):,}")

# Why? Check lake_mask + above_sea
CHAN_LAKE = np.uint8(3)
lake_mask_meta = river_meta == CHAN_LAKE
above_sea_carved = carved_y > SEA_LEVEL
print(f"  Of those: in lake_mask (river_meta==CHAN_LAKE): "
      f"{int((dropped_carver & lake_mask_meta).sum()):,}")
print(f"  Of those: NOT above_sea: "
      f"{int((dropped_carver & ~above_sea_carved).sum()):,}")
print(f"  Of those: NOT lake AND above_sea: "
      f"{int((dropped_carver & ~lake_mask_meta & above_sea_carved).sum()):,}")

# Build river_water_y like run_pipeline does
river_water_y = np.where(water_y_field > 0,
                         water_y_field,
                         np.int16(-999)).astype(np.int16)

# Lakes get water_y from lake_water_levels in run_pipeline. Replicate.
from scipy.ndimage import label as _label_lakes
lake_meta_mask = river_meta == CHAN_LAKE
if lake_meta_mask.any():
    lake_labeled, n_lakes = _label_lakes(lake_meta_mask)
    _hydro_lake_wl = masks.get("hydro_lake_wl")
    _wl_mc_float = np.interp(
        (_hydro_lake_wl * 65535.0).ravel(),
        gaea_in, mc_y_out
    ).reshape(_hydro_lake_wl.shape).astype(np.float32)
    pre_carve_y = surface_y.copy()
    for lid in range(1, n_lakes + 1):
        lk = lake_labeled == lid
        if not lk.any(): continue
        _wl_vals = _wl_mc_float[lk]
        _wl_vals = _wl_vals[_wl_vals > -64]
        if len(_wl_vals):
            lake_water = int(np.floor(float(_wl_vals.min())))
        else:
            lake_water = int(pre_carve_y[lk].min()) + 1
        river_water_y[lk] = np.int16(lake_water)
    print(f"\n  Lakes found: {n_lakes}")

print(f"\nSTEP 4 -- river_water_y > SEA_Y ({SEA_LEVEL}) -- chunk_writer gate: "
      f"{int((river_water_y > SEA_LEVEL).sum()):,}")
print(f"STEP 4a -- > SEA_Y AND painted: "
      f"{int(((river_water_y > SEA_LEVEL) & cl).sum()):,}")
print(f"\nDROPPED at chunk_writer gate (painted, has water_y, but <= SEA_Y): "
      f"{int((cl & (river_water_y > 0) & (river_water_y <= SEA_LEVEL)).sum()):,}")

# Also: cells that have water_y > carved surface (will actually paint water)
print(f"\nSTEP 5 -- final water-bearing cells "
      f"(carved_y < river_water_y AND river_water_y > SEA_Y): "
      f"{int(((carved_y < river_water_y) & (river_water_y > SEA_LEVEL)).sum()):,}")

# Of painted cells:
final_water = (carved_y < river_water_y) & (river_water_y > SEA_LEVEL)
print(f"STEP 5a -- final water AND painted: "
      f"{int((final_water & cl).sum()):,}")

# Where did the rest go?
no_water_painted = cl & ~final_water
print(f"\nFINAL DROPPED (painted, no visible water): "
      f"{int(no_water_painted.sum()):,}")
print(f"  surface >= water_y: {int((no_water_painted & (carved_y >= river_water_y)).sum()):,}")
print(f"  water_y <= SEA_Y: {int((no_water_painted & (river_water_y <= SEA_LEVEL)).sum()):,}")
print(f"  water_y == -999 (never set): {int((no_water_painted & (river_water_y == -999)).sum()):,}")

# What's the carved_y vs river_water_y distribution at painted cells?
if cl.any():
    cy_at_paint = carved_y[cl]
    rwy_at_paint = river_water_y[cl]
    print(f"\nAt painted cells:")
    print(f"  carved_y    p10/50/90 = {np.percentile(cy_at_paint, [10,50,90])}")
    print(f"  river_water_y p10/50/90 = {np.percentile(rwy_at_paint, [10,50,90])}")
    diff = rwy_at_paint - cy_at_paint
    print(f"  (water_y - carved_y) p10/50/90 = {np.percentile(diff, [10,50,90])}")
