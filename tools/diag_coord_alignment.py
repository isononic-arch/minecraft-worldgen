"""
diag_coord_alignment.py
Verifies that TIFF pixel [0,0] maps correctly to MC chunk/block space,
and confirms the override.tif Y-flip alignment against height.tif.

Prints MC world coordinates of first and last pixel of tile (32,2),
and samples both TIFs at matching positions to confirm consistency.

Usage: python tools/diag_coord_alignment.py
"""
import sys
sys.path.insert(0, r'C:\Users\nicho\minecraft-worldgen')

import numpy as np
import rasterio
from rasterio.windows import Window

MASKS_DIR  = r'C:\Users\nicho\minecraft-worldgen\masks'
TILE_X, TILE_Z = 32, 2
TILE_SIZE  = 512
WORLD_PX   = 50_000   # total TIF dimension

col_off = TILE_X * TILE_SIZE   # 16384
row_off = TILE_Z * TILE_SIZE   # 1024

print("=" * 60)
print(f"Tile ({TILE_X}, {TILE_Z})")
print("=" * 60)

# ── MC world block coordinates ────────────────────────────────────────────────
mc_x_first = col_off
mc_z_first = row_off
mc_x_last  = col_off + TILE_SIZE - 1
mc_z_last  = row_off + TILE_SIZE - 1

print(f"\n[coords] Pixel offsets:  col_off={col_off}, row_off={row_off}")
print(f"[coords] MC world X:     {mc_x_first} to {mc_x_last}")
print(f"[coords] MC world Z:     {mc_z_first} to {mc_z_last}")
print(f"[coords] Chunk X:        {mc_x_first//16} to {mc_x_last//16}")
print(f"[coords] Chunk Z:        {mc_z_first//16} to {mc_z_last//16}")
print(f"[coords] Region X:       {mc_x_first//512} to {mc_x_last//512}")
print(f"[coords] Region Z:       {mc_z_first//512} to {mc_z_last//512}")
print(f"[coords] Expected .mca:  r.{mc_x_first//512}.{mc_z_first//512}.mca")

# ── Sample height.tif at tile corners ────────────────────────────────────────
print(f"\n[height] Reading height.tif at normal row_off={row_off}...")
with rasterio.open(f'{MASKS_DIR}/height.tif') as src:
    h_tif_h, h_tif_w = src.height, src.width
    print(f"  TIF dimensions: {h_tif_w} x {h_tif_h}")

    # Four corners of the tile
    corners = [
        ("top-left",     col_off,              row_off),
        ("top-right",    col_off+TILE_SIZE-1,  row_off),
        ("bottom-left",  col_off,              row_off+TILE_SIZE-1),
        ("bottom-right", col_off+TILE_SIZE-1,  row_off+TILE_SIZE-1),
    ]
    h_vals = {}
    for label, cx, rz in corners:
        w = Window(cx, rz, 1, 1)
        v = int(src.read(1, window=w)[0, 0])
        above_sea = "LAND" if v < 17050 else "OCEAN"
        print(f"  {label:>14}: pixel({cx},{rz})  raw_h16={v:>5}  ({above_sea})")
        h_vals[label] = v

# ── Sample override.tif with and without flip ─────────────────────────────────
print(f"\n[override] TIF Y-flip check...")
flipped_row_off = WORLD_PX - row_off - TILE_SIZE   # = 48464

with rasterio.open(f'{MASKS_DIR}/override.tif') as src:
    ov_tif_h, ov_tif_w = src.height, src.width
    print(f"  TIF dimensions: {ov_tif_w} x {ov_tif_h}")

    # Read normal (unflipped)
    ov_normal = src.read(1, window=Window(col_off, row_off, TILE_SIZE, TILE_SIZE)).astype(np.uint8)
    ov_normal_zones = sorted(z for z in np.unique(ov_normal).tolist() if z > 0)

    # Read flipped (as tile_streamer does for override)
    ov_flipped = src.read(1, window=Window(col_off, flipped_row_off, TILE_SIZE, TILE_SIZE)).astype(np.uint8)
    ov_flipped = ov_flipped[::-1, :]   # flip back so we can compare semantically
    ov_flipped_zones = sorted(z for z in np.unique(ov_flipped).tolist() if z > 0)

    print(f"  Normal read  (row {row_off}):          zone values = {ov_normal_zones or '[all zeros]'}")
    print(f"  Flipped read (row {flipped_row_off}):  zone values = {ov_flipped_zones or '[all zeros]'}")

    # Check override presence vs land/ocean in height
    # If override zones are present where height says OCEAN, that's a mismatch
    with rasterio.open(f'{MASKS_DIR}/height.tif') as hsrc:
        h_tile = hsrc.read(1, window=Window(col_off, row_off, TILE_SIZE, TILE_SIZE)).astype(np.uint16)

    land_px  = int((h_tile < 17050).sum())
    ocean_px = int((h_tile >= 17050).sum())
    print(f"\n  Height tile: {land_px} land px, {ocean_px} ocean px  (out of {TILE_SIZE**2})")

    # For the FLIPPED version (as pipeline uses it):
    ov_nonzero_flipped = int((ov_flipped > 0).sum())
    ov_on_land   = int(((ov_flipped > 0) & (h_tile < 17050)).sum())
    ov_on_ocean  = int(((ov_flipped > 0) & (h_tile >= 17050)).sum())
    print(f"  Override (flipped) nonzero: {ov_nonzero_flipped} px")
    print(f"    of which on land:  {ov_on_land} px")
    print(f"    of which on ocean: {ov_on_ocean} px")
    if ov_on_ocean > 0 and ov_on_land == 0:
        print("  WARNING: override zones only appear over ocean pixels — possible misalignment!")
    elif ov_on_land > ov_on_ocean:
        print("  OK: override zones predominantly over land — alignment looks correct")
    else:
        print(f"  NOTE: mixed land/ocean overlap — review manually")

    # For NORMAL version (unflipped):
    ov_nonzero_normal = int((ov_normal > 0).sum())
    ov_on_land_n  = int(((ov_normal > 0) & (h_tile < 17050)).sum())
    ov_on_ocean_n = int(((ov_normal > 0) & (h_tile >= 17050)).sum())
    print(f"\n  Override (normal/unflipped) nonzero: {ov_nonzero_normal} px")
    print(f"    of which on land:  {ov_on_land_n} px")
    print(f"    of which on ocean: {ov_on_ocean_n} px")

print(f"\n[summary]")
print(f"  Tile ({TILE_X},{TILE_Z}) → MC blocks X=[{mc_x_first},{mc_x_last}] Z=[{mc_z_first},{mc_z_last}]")
print(f"  Region file: r.{mc_x_first//512}.{mc_z_first//512}.mca")
print(f"  Override flip: {'CONSISTENT (flipped zones land-dominant)' if ov_on_land > ov_on_ocean else 'INVESTIGATE'}")
print("=" * 60)
