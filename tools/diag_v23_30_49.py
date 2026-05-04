"""Run the carver on (30,49) and dump lake_mask + river_meta stats."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import numpy as np
import rasterio
from rasterio.windows import Window
from core import tile_streamer, river_carver_v2, column_generator

masks_dir = Path(r"C:/Users/nicho/minecraft-worldgen/masks")
TX, TZ = 30, 49
TILE_SIZE = 512

# Load config
with open("config/thresholds.json") as f:
    cfg = json.load(f)

# Load tile masks — read_tile expects PIXEL offsets, not tile indices
masks = tile_streamer.read_tile(masks_dir, TX * TILE_SIZE, TZ * TILE_SIZE, mask_subset=None)

print(f"Mask keys present: {sorted(masks.keys())}")
print(f"hydro_lake range: {masks['hydro_lake'].min()} - {masks['hydro_lake'].max()}")
print(f"hydro_lake_wl range: {masks['hydro_lake_wl'].min():.4f} - {masks['hydro_lake_wl'].max():.4f}")
print(f"height range: {masks['height'].min():.4f} - {masks['height'].max():.4f}")
print(f"hydro_lake nonzero cells: {(masks['hydro_lake'] > 0).sum():,}")
print(f"hydro_lake_wl nonzero cells: {(masks['hydro_lake_wl'] > 0).sum():,}")

# Compute pre_carve_y from height
gaea_in = np.array([0, 17050, 45000, 65496], dtype=np.float64)
mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
height_raw = np.round(masks["height"] * 65535.0).astype(np.uint16)
surface_y_pre = np.interp(height_raw.ravel(), gaea_in, mc_y_out).astype(np.int16).reshape(height_raw.shape)

# Replicate carver's terrain-intersection lake logic with PAD halo
PAD = 48
col_off = TX * TILE_SIZE
row_off = TZ * TILE_SIZE

with rasterio.open(masks_dir / "hydro_lake_wl.tif") as src:
    px0 = max(col_off - PAD, 0)
    pz0 = max(row_off - PAD, 0)
    px1 = min(col_off + TILE_SIZE + PAD, src.width)
    pz1 = min(row_off + TILE_SIZE + PAD, src.height)
    pad_wl_raw = src.read(1, window=Window(px0, pz0, px1 - px0, pz1 - pz0))

with rasterio.open(masks_dir / "height.tif") as src:
    pad_h_raw = src.read(1, window=Window(px0, pz0, px1 - px0, pz1 - pz0))

with rasterio.open(masks_dir / "hydro_lake.tif") as src:
    pad_lake_raw = src.read(1, window=Window(px0, pz0, px1 - px0, pz1 - pz0))

print()
print(f"PAD region shape: {pad_wl_raw.shape}")
print(f"  pad_wl_raw dtype: {pad_wl_raw.dtype}, max raw value: {pad_wl_raw.max()}")
print(f"  pad_h_raw dtype: {pad_h_raw.dtype}, range: {pad_h_raw.min()}-{pad_h_raw.max()}")
print(f"  pad_lake_raw dtype: {pad_lake_raw.dtype}, max lake ID: {pad_lake_raw.max()}")
print(f"  lake basin cells in PAD: {(pad_lake_raw > 0).sum():,}")
print(f"  wl > 0 cells in PAD: {(pad_wl_raw > 0).sum():,}")

# Convert to MC Y. NOTE: hydro_lake_wl.tif is float32 ALREADY in [0,1].
# height.tif is uint16 raw 0-65535.
pad_wl_norm = pad_wl_raw.astype(np.float32)  # already [0,1]
pad_h_norm = pad_h_raw.astype(np.float32) / 65535.0

pad_water_y = np.interp(
    (pad_wl_norm * 65535.0).ravel(),
    gaea_in, mc_y_out,
).reshape(pad_wl_norm.shape).astype(np.float32)
pad_terrain_y = np.interp(
    (pad_h_norm * 65535.0).ravel(),
    gaea_in, mc_y_out,
).reshape(pad_h_norm.shape).astype(np.float32)

pad_basin = pad_wl_norm > 0
pad_underwater = pad_basin & (pad_terrain_y < pad_water_y)
print()
print(f"pad_underwater cells: {pad_underwater.sum():,}")
print(f"pad_basin cells (wl>0): {pad_basin.sum():,}")
if pad_basin.any():
    print(f"  Of basin cells, terrain Y range: {pad_terrain_y[pad_basin].min():.1f} - {pad_terrain_y[pad_basin].max():.1f}")
    print(f"  Of basin cells, water Y range:   {pad_water_y[pad_basin].min():.1f} - {pad_water_y[pad_basin].max():.1f}")
    diff = pad_water_y[pad_basin] - pad_terrain_y[pad_basin]
    print(f"  water_y - terrain_y range: {diff.min():.1f} - {diff.max():.1f}")
    print(f"  water_y > terrain_y count: {(diff > 0).sum():,}")
    print(f"  water_y >= terrain_y count: {(diff >= 0).sum():,}")
    print(f"  water_y == terrain_y count: {(diff == 0).sum():,}")

# Crop back to tile
crop_r0 = row_off - pz0
crop_c0 = col_off - px0
lake_mask = pad_underwater[crop_r0:crop_r0+TILE_SIZE, crop_c0:crop_c0+TILE_SIZE]
print()
print(f"Tile lake_mask cells: {lake_mask.sum():,}")
above_sea = (surface_y_pre > 63)
print(f"Tile above_sea cells: {above_sea.sum():,}")
print(f"Tile lake_mask & above_sea: {(lake_mask & above_sea).sum():,}")

# Now run the actual carver and verify river_meta paints CHAN_LAKE
print()
print("=== Running carve_rivers ===")
sy_out, river_meta, conn_channel, water_y_field = river_carver_v2.carve_rivers(
    surface_y      = surface_y_pre.copy(),
    flow_tile      = masks["flow"],
    river_tile     = masks["river"],
    cfg            = cfg,
    hydro_order    = masks.get("hydro_order"),
    hydro_width    = masks.get("hydro_width"),
    hydro_depth    = masks.get("hydro_depth"),
    hydro_lake     = masks.get("hydro_lake"),
    hydro_lkdep    = masks.get("hydro_lkdep"),
    hydro_lake_wl  = masks.get("hydro_lake_wl"),
    hydro_centerline = masks.get("hydro_centerline"),
    height_norm    = masks["height"],
    masks_dir      = masks_dir,
    tile_x         = TX,
    tile_z         = TZ,
)
print(f"river_meta CHAN_LAKE  (3): {(river_meta == 3).sum():,}")
print(f"river_meta CHAN_RIVER (2): {(river_meta == 2).sum():,}")
print(f"river_meta CHAN_STREAM(1): {(river_meta == 1).sum():,}")
print(f"river_meta CHAN_NONE  (0): {(river_meta == 0).sum():,}")
print(f"sy_out < surface_y_pre: {(sy_out < surface_y_pre).sum():,} cells (carved)")
print(f"sy_out min/max: {sy_out.min()}/{sy_out.max()}")
