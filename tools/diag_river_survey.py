"""diag_river_survey.py — scope where river water is likely WRONG so we can point
local renders at the right tiles. Reads masks at 1:8 (fast).

Reports, per 512-block tile:
  - DELTA tiles: above-sea river centerline that reaches the ocean (river mouth).
  - HIGHALT tiles: river centerline whose bed MC-Y is high (mountain rivers).
  - LAKEJUNC tiles: a lake touches a river, AND the lake water level sits ABOVE
    the adjacent river water level (the user's "lakes sit higher than rivers").
  - OVERLEVEL tiles: baked river water level (hydro_river_wl) sits notably ABOVE
    the local terrain bed across many cells (the 13,80 wide-flat-wash failure).

Usage: py tools/diag_river_survey.py
"""
import sys
import numpy as np
import rasterio
from rasterio.enums import Resampling
from scipy import ndimage

sys.path.insert(0, ".")
from core import column_generator as _cg

LUT = _cg._LUT
SEA = _cg.SEA_LEVEL
FULL = 50000
SC = 8
N = FULL // SC                      # 6250
TILE = 512 // SC                    # 64 px per tile at 1:8
NT = 97
M = "masks"


def block_max(path, n=N, scale=SC):
    out = np.zeros((n, n), dtype=np.float32)
    CH = max(1, 800 // scale)
    with rasterio.open(path) as s:
        W = s.width
        WT = (W // scale) * scale
        for o0 in range(0, n, CH):
            o1 = min(o0 + CH, n)
            blk = s.read(1, window=((o0 * scale, o1 * scale), (0, WT))).astype(np.float32)
            nr = o1 - o0
            blk = blk[:nr * scale, :WT].reshape(nr, scale, WT // scale, scale)
            out[o0:o1, :WT // scale] = blk.max(axis=(1, 3))
    return out


def read_bilinear(path, n=N):
    with rasterio.open(path) as s:
        return s.read(1, out_shape=(n, n), resampling=Resampling.bilinear)


def tile_hist(mask):
    """count of True px per 64x64 tile -> (97,97)"""
    m = mask[:NT * TILE, :NT * TILE].reshape(NT, TILE, NT, TILE)
    return m.sum(axis=(1, 3))


print("[survey] reading masks @1:8 ...", flush=True)
h = read_bilinear(f"{M}/height.tif").astype(np.uint16)
bed = LUT[h].astype(np.int32)
del h
cl = block_max(f"{M}/hydro_centerline.tif") > 0
lake = block_max(f"{M}/hydro_lake.tif") > 0
# river water level + lake water level (block_max picks the level where present)
rwl = block_max(f"{M}/hydro_river_wl.tif")          # MC-Y on river footprint, -999 else
try:
    lwl = block_max(f"{M}/hydro_lake_wl.tif")        # MC-Y on lakes (float)
except Exception:
    lwl = np.full((N, N), -999.0, np.float32)

river = cl & ~lake & (bed > SEA)
print(f"[survey] river px(1:8)={int(river.sum())}  lake px={int(lake.sum())}  "
      f"rwl px={int((rwl > SEA).sum())}", flush=True)

# ---- DELTA: tiles holding BOTH a river and the coastline (river mouth) ----
ocean = bed <= SEA
river_dil_d = ndimage.binary_dilation(river, iterations=3)
coast = ocean & ndimage.binary_dilation(~ocean, iterations=2)  # shoreline band
delta = coast & river_dil_d                                    # shoreline near a river
delta_t = tile_hist(delta)

# ---- HIGHALT: river bed MC-Y high ----
for THR in (120, 150, 200):
    ha = river & (bed >= THR)
    print(f"[survey] highalt river px bed>={THR}: {int(ha.sum())}")
highalt = river & (bed >= 150)
highalt_t = tile_hist(highalt)

# ---- LAKEJUNC: ANY lake touching a river (level checked in the RENDER, since
#      the mask-level lwl != rendered lake level — the pipeline sets it) ----
river_dil = ndimage.binary_dilation(river, iterations=2)
lake_edge = lake & river_dil                    # lake cells next to a river
junc_t = tile_hist(lake_edge)
# informational mask-level compare (known to under-report vs render)
junc_hi = np.zeros((N, N), bool)
if lake_edge.any():
    rl = np.where(river & (rwl > SEA), rwl, 0).astype(np.float32)
    for _ in range(3):
        rl = np.maximum(rl, ndimage.grey_dilation(rl, size=3))
    ll = np.where(lake & (lwl > SEA), lwl, 0).astype(np.float32)
    junc_hi = lake_edge & (ll > 0) & (rl > 0) & (ll > rl + 1.0)

# ---- OVERLEVEL/PERCH: river water level above the lowest ADJACENT LAND bed
#      (pre-cleanup upper bound — the bake over-levels vs the banks here) ----
INF = np.int32(1 << 20)
land = ~river & ~lake
land_bed = np.where(land, bed, INF).astype(np.int32)
min_adj_land = np.full((N, N), INF, np.int32)
for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
    min_adj_land = np.minimum(min_adj_land, np.roll(np.roll(land_bed, dz, 0), dx, 1))
overl = river & (rwl > SEA) & (rwl > min_adj_land + 1)   # water >1 above a land bank
overl_t = tile_hist(overl)


def top_tiles(th, label, n=12, minpx=8):
    flat = [(int(th[tz, tx]), tx, tz) for tz in range(NT) for tx in range(NT)
            if th[tz, tx] >= minpx]
    flat.sort(reverse=True)
    print(f"\n=== {label}: {len(flat)} tiles (>= {minpx}px@1:8) ; top {n}:")
    for c, tx, tz in flat[:n]:
        print(f"   ({tx},{tz})  {c}px@1:8  (~{c*64} blocks)")
    return [(tx, tz) for _, tx, tz in flat[:n]]


print("\n########## SURVEY RESULTS ##########")
d = top_tiles(delta_t, "DELTA (shoreline tile with a river = river mouth)", minpx=4)
ha = top_tiles(highalt_t, "HIGHALT (river bed MC-Y>=150)")
j = top_tiles(junc_t, "LAKEJUNC (lake touches river — check level in render)", minpx=4)
ov = top_tiles(overl_t, "PERCH-CANDIDATE (river water >1 above a LAND bank, pre-cleanup)")
print(f"\n[prevalence] lake-edge px={int(lake_edge.sum())} ; "
      f"mask lake>river+1 px={int(junc_hi.sum())} "
      f"({100.0*junc_hi.sum()/max(1,lake_edge.sum()):.1f}% — under-reports vs render)")
print(f"[prevalence] river px={int(river.sum())} ; pre-cleanup perch px={int(overl.sum())} "
      f"({100.0*overl.sum()/max(1,river.sum()):.1f}% of river)")
print(f"[prevalence] perch-candidate tiles>=8px: "
      f"{int((overl_t>=8).sum())} / {int((tile_hist(river)>=8).sum())} river tiles")
