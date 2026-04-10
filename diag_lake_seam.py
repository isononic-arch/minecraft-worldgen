"""
Diagnostic: investigate lake shore "wall" artifact near tile boundary
between tiles (51,52) and (51,53).

Tile Z boundary is at block Z=27136 (53*512).
We read a combined area covering both tiles:
  col 26112..26623 (tile_x=51), row 26624..27647 (tile_z=52..53)
The boundary is at row 27136, which is row index 512 of our window.
"""

import numpy as np
import rasterio
from rasterio.windows import Window

MASKS = r"C:\Users\nicho\minecraft-worldgen\masks"

# Window: col_off, row_off, width, height
# Tile (51,52): cols 26112..26623, rows 26624..27135
# Tile (51,53): cols 26112..26623, rows 27136..27647
# Combined: cols 26112..26623, rows 26624..27647
col_off = 51 * 512  # 26112
row_off = 52 * 512  # 26624
width = 512
height = 1024  # two tiles

win = Window(col_off, row_off, width, height)

# The tile boundary is at local row 512
BOUNDARY = 512

print("=" * 70)
print(f"Reading area: col [{col_off}:{col_off+width}], row [{row_off}:{row_off+height}]")
print(f"Tile boundary at global row {row_off + BOUNDARY} = local row {BOUNDARY}")
print(f"Artifact reported near block X=26553..26606, Z=27071..27190")
print(f"  -> local X = {26553-col_off}..{26606-col_off}, local Z = {27071-row_off}..{27190-row_off}")
print("=" * 70)

# --- Read the three layers ---
def read_layer(name):
    path = f"{MASKS}\\{name}"
    with rasterio.open(path) as ds:
        data = ds.read(1, window=win)
        print(f"\n{name}: dtype={data.dtype}, shape={data.shape}, "
              f"min={np.nanmin(data):.4f}, max={np.nanmax(data):.4f}, "
              f"mean={np.nanmean(data):.4f}")
    return data

lake = read_layer("hydro_lake.tif")
lkdep = read_layer("hydro_lkdep.tif")
height = read_layer("height.tif")

# --- 1. Lake mask around boundary ---
print("\n" + "=" * 70)
print("1. LAKE MASK: 20-pixel band around tile boundary (rows 502..522)")
print("=" * 70)

band_lake = lake[BOUNDARY-10:BOUNDARY+10, :]
for r in range(20):
    row_idx = BOUNDARY - 10 + r
    vals = band_lake[r, :]
    nonzero = np.count_nonzero(vals)
    marker = " <<< TILE BOUNDARY" if row_idx == BOUNDARY else ""
    print(f"  row {row_idx:4d} (global Z={row_off+row_idx:5d}): "
          f"nonzero={nonzero:3d}/{width}  min={vals.min():.2f} max={vals.max():.2f}{marker}")

# --- 2. Lake mask continuity at boundary ---
print("\n" + "=" * 70)
print("2. LAKE MASK DISCONTINUITY at boundary")
print("=" * 70)

row_above = lake[BOUNDARY-1, :]  # last row of tile 52
row_below = lake[BOUNDARY, :]    # first row of tile 53
diff = np.abs(row_above.astype(float) - row_below.astype(float))
print(f"  Row {BOUNDARY-1} (last of tile 52): nonzero={np.count_nonzero(row_above)}")
print(f"  Row {BOUNDARY}   (first of tile 53): nonzero={np.count_nonzero(row_below)}")
print(f"  Abs diff: max={diff.max():.4f}, mean={diff.mean():.6f}, "
      f"pixels_with_diff={np.count_nonzero(diff)}")

# Where does lake exist on one side but not the other?
lake_above_only = (row_above > 0) & (row_below == 0)
lake_below_only = (row_below > 0) & (row_above == 0)
print(f"  Lake ABOVE only (ends at boundary): {np.count_nonzero(lake_above_only)} pixels, "
      f"cols: {np.where(lake_above_only)[0].tolist()[:20]}...")
print(f"  Lake BELOW only (starts at boundary): {np.count_nonzero(lake_below_only)} pixels, "
      f"cols: {np.where(lake_below_only)[0].tolist()[:20]}...")

# --- 3. Height around boundary ---
print("\n" + "=" * 70)
print("3. HEIGHT: 20-pixel band around tile boundary")
print("=" * 70)

band_h = height[BOUNDARY-10:BOUNDARY+10, :]
for r in range(20):
    row_idx = BOUNDARY - 10 + r
    vals = band_h[r, :]
    marker = " <<< TILE BOUNDARY" if row_idx == BOUNDARY else ""
    print(f"  row {row_idx:4d} (global Z={row_off+row_idx:5d}): "
          f"min={vals.min():.2f} max={vals.max():.2f} mean={vals.mean():.2f}{marker}")

# --- 4. Height discontinuity at boundary ---
print("\n" + "=" * 70)
print("4. HEIGHT DISCONTINUITY at boundary")
print("=" * 70)

h_above = height[BOUNDARY-1, :].astype(float)
h_below = height[BOUNDARY, :].astype(float)
h_diff = h_above - h_below
print(f"  Height diff (above - below): min={h_diff.min():.4f}, max={h_diff.max():.4f}, "
      f"mean={h_diff.mean():.6f}")
big_jumps = np.abs(h_diff) > 2.0
print(f"  Pixels with |jump| > 2 blocks: {np.count_nonzero(big_jumps)}")
if np.any(big_jumps):
    cols = np.where(big_jumps)[0]
    print(f"  Columns with big jumps: {cols.tolist()[:30]}...")
    for c in cols[:10]:
        print(f"    col {c}: above={h_above[c]:.2f}, below={h_below[c]:.2f}, diff={h_diff[c]:.2f}")

# --- 5. Detailed strip in the artifact zone ---
print("\n" + "=" * 70)
print("5. DETAILED STRIP: artifact zone")
print("   Reported artifact at block X=26553..26606 -> local col 441..494")
print("   Reported artifact at block Z=27071..27190 -> local row 447..566")
print("   Showing col 440:500, row 500:525 (straddling boundary at 512)")
print("=" * 70)

cx = slice(440, 500)
cz = slice(500, 525)

print("\n  Lake mask:")
strip_lake = lake[cz, cx]
for r in range(strip_lake.shape[0]):
    row_idx = 500 + r
    vals = strip_lake[r, :]
    marker = " <<< BOUNDARY" if row_idx == BOUNDARY else ""
    # Show individual values compactly
    compact = ''.join(['#' if v > 0 else '.' for v in vals])
    print(f"  z={row_idx:4d}: {compact}{marker}")

print("\n  Lake depth (lkdep):")
strip_dep = lkdep[cz, cx]
for r in range(strip_dep.shape[0]):
    row_idx = 500 + r
    vals = strip_dep[r, :]
    marker = " <<< BOUNDARY" if row_idx == BOUNDARY else ""
    vmin, vmax, vmean = vals.min(), vals.max(), vals.mean()
    nonzero = np.count_nonzero(vals)
    print(f"  z={row_idx:4d}: min={vmin:6.2f} max={vmax:6.2f} mean={vmean:6.2f} "
          f"nonzero={nonzero:2d}{marker}")

print("\n  Height values:")
strip_h = height[cz, cx]
for r in range(strip_h.shape[0]):
    row_idx = 500 + r
    vals = strip_h[r, :]
    marker = " <<< BOUNDARY" if row_idx == BOUNDARY else ""
    vmin, vmax, vmean = vals.min(), vals.max(), vals.mean()
    print(f"  z={row_idx:4d}: min={vmin:7.2f} max={vmax:7.2f} mean={vmean:7.2f}{marker}")

# --- 6. Check for sharp height cliff at artifact location ---
print("\n" + "=" * 70)
print("6. HEIGHT GRADIENT across boundary in artifact zone (col 440:500)")
print("=" * 70)

for row_idx in range(505, 520):
    above = height[row_idx, 440:500].astype(float)
    below = height[row_idx+1, 440:500].astype(float)
    grad = below - above
    print(f"  z={row_idx}->{row_idx+1}: grad min={grad.min():+.2f} max={grad.max():+.2f} "
          f"mean={grad.mean():+.4f}"
          + (" <<< BOUNDARY" if row_idx+1 == BOUNDARY else ""))

# --- 7. Check if lkdep has a seam ---
print("\n" + "=" * 70)
print("7. LAKE DEPTH DISCONTINUITY at boundary")
print("=" * 70)

dep_above = lkdep[BOUNDARY-1, :].astype(float)
dep_below = lkdep[BOUNDARY, :].astype(float)
dep_diff = dep_above - dep_below
print(f"  Depth diff (above - below): min={dep_diff.min():.4f}, max={dep_diff.max():.4f}")
big_dep = np.abs(dep_diff) > 1.0
print(f"  Pixels with |depth jump| > 1: {np.count_nonzero(big_dep)}")
if np.any(big_dep):
    cols = np.where(big_dep)[0]
    print(f"  Columns: {cols.tolist()[:30]}...")
    for c in cols[:10]:
        print(f"    col {c}: above={dep_above[c]:.2f}, below={dep_below[c]:.2f}, diff={dep_diff[c]:.2f}")

# Also check rows around boundary
print("\n  Lake depth values row-by-row near boundary (mean of cols 440:500):")
for row_idx in range(505, 520):
    vals = lkdep[row_idx, 440:500]
    marker = " <<< BOUNDARY" if row_idx == BOUNDARY else ""
    print(f"  z={row_idx:4d}: min={vals.min():6.2f} max={vals.max():6.2f} "
          f"mean={vals.mean():6.2f}{marker}")

print("\n" + "=" * 70)
print("DONE")
print("=" * 70)
