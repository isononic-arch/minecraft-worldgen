"""
Diagnostic: river-lake depth discontinuity at tile (51, 53).
Problem location: world coords (26574, 109, 27225).
"""
import sys, json
import numpy as np
import rasterio
from rasterio.windows import Window

# ── Config ──────────────────────────────────────────────────────────────────
TILE_X, TILE_Z = 51, 53
TILE_SIZE = 512
COL_OFF = TILE_X * TILE_SIZE
ROW_OFF = TILE_Z * TILE_SIZE

# World coords of the known problem point
WX, WY, WZ = 26574, 109, 27225
LOCAL_X = WX - TILE_X * TILE_SIZE   # = 26574 - 26112 = 462
LOCAL_Z = WZ - TILE_Z * TILE_SIZE   # = 27225 - 27136 = 89

MASK_DIR = r"C:\Users\nicho\minecraft-worldgen\masks"
CFG_PATH = r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json"

# Height spline from thresholds.json
GAEA_IN  = [0, 11602, 26921, 35435, 43437, 65496]
MC_Y_OUT = [-64, 63, 120, 278, 398, 448]

CROSS_HALF = 15  # 30 pixels wide cross-section
CROSS_ROW = LOCAL_Z  # fixed Z, sweep X

print(f"Tile ({TILE_X},{TILE_Z}), local point: x={LOCAL_X} z={LOCAL_Z}")
print(f"Cross-section: row(z)={CROSS_ROW}, cols(x)={LOCAL_X-CROSS_HALF}..{LOCAL_X+CROSS_HALF}")
print()

# ── 1. Load masks ──────────────────────────────────────────────────────────
def load_mask(name):
    path = f"{MASK_DIR}/{name}.tif"
    with rasterio.open(path) as src:
        win = Window(COL_OFF, ROW_OFF, TILE_SIZE, TILE_SIZE)
        return src.read(1, window=win)

height_raw  = load_mask("height")      # uint16
hydro_order = load_mask("hydro_order")  # float or uint8
hydro_lake  = load_mask("hydro_lake")
hydro_width = load_mask("hydro_width")
hydro_depth = load_mask("hydro_depth")
hydro_lkdep = load_mask("hydro_lkdep")
flow        = load_mask("flow")

print(f"height dtype={height_raw.dtype}, range=[{height_raw.min()}, {height_raw.max()}]")
print(f"hydro_order dtype={hydro_order.dtype}, range=[{hydro_order.min():.4f}, {hydro_order.max():.4f}]")
print(f"hydro_lake  dtype={hydro_lake.dtype},  range=[{hydro_lake.min():.4f}, {hydro_lake.max():.4f}]")
print(f"hydro_lkdep dtype={hydro_lkdep.dtype}, range=[{hydro_lkdep.min():.4f}, {hydro_lkdep.max():.4f}]")
print()

# ── 2. Convert height to MC Y ──────────────────────────────────────────────
# Normalize height to [0,1] as tile_streamer would
if height_raw.dtype == np.uint16:
    h_norm = height_raw.astype(np.float32) / 65535.0
elif height_raw.max() <= 1.0:
    h_norm = height_raw.astype(np.float32)
else:
    h_norm = height_raw.astype(np.float32) / 65535.0

# Denorm to uint16 for spline
h_u16 = np.round(h_norm * 65535.0).astype(np.uint16)
surface_y = np.interp(h_u16.astype(np.float64), GAEA_IN, MC_Y_OUT).astype(np.int16)

pre_carve_y = surface_y.copy()

print(f"Pre-carve Y at problem point ({LOCAL_X},{CROSS_ROW}): {surface_y[CROSS_ROW, LOCAL_X]}")
print(f"Height raw at problem point: {height_raw[CROSS_ROW, LOCAL_X]}")
print()

# ── 3. Normalize masks to [0,1] as tile_streamer would ─────────────────────
# tile_streamer normalizes everything to float32 [0,1]
def ensure_norm(arr):
    """If already float [0,1], return as-is. If uint, normalize."""
    if arr.dtype in (np.float32, np.float64) and arr.max() <= 1.01:
        return arr.astype(np.float32)
    elif arr.dtype == np.uint8:
        return arr.astype(np.float32) / 255.0
    elif arr.dtype == np.uint16:
        return arr.astype(np.float32) / 65535.0
    else:
        return arr.astype(np.float32) / max(arr.max(), 1)

hydro_order_n = ensure_norm(hydro_order)
hydro_lake_n  = ensure_norm(hydro_lake)
hydro_width_n = ensure_norm(hydro_width)
hydro_depth_n = ensure_norm(hydro_depth)
hydro_lkdep_n = ensure_norm(hydro_lkdep)
flow_n        = ensure_norm(flow)

# ── 4. Run the carver ──────────────────────────────────────────────────────
with open(CFG_PATH) as f:
    cfg = json.load(f)

sys.path.insert(0, r"C:\Users\nicho\minecraft-worldgen")
from core.river_carver_v2 import carve_rivers

surface_carved, river_meta = carve_rivers(
    surface_y=surface_y.copy(),
    flow_tile=flow_n,
    river_tile=np.zeros_like(surface_y),  # unused
    cfg=cfg,
    hydro_order=hydro_order_n,
    hydro_width=hydro_width_n,
    hydro_depth=hydro_depth_n,
    hydro_lake=hydro_lake_n,
    hydro_lkdep=hydro_lkdep_n,
)

# ── 5. Print cross-section ─────────────────────────────────────────────────
META_NAMES = {0: "none", 1: "strm", 2: "rivr", 3: "lake"}

z = CROSS_ROW
x_start = max(LOCAL_X - CROSS_HALF, 0)
x_end   = min(LOCAL_X + CROSS_HALF + 1, TILE_SIZE)

print("=" * 100)
print(f"Cross-section at z={z} (world Z={WZ}), x={x_start}..{x_end-1}")
print(f"{'x':>4} {'wX':>6} {'preY':>5} {'postY':>5} {'carve':>5} {'meta':>4} "
      f"{'ord':>4} {'lake':>6} {'lkdep':>5} {'width':>5} {'depth':>5} {'h_raw':>6}")
print("-" * 100)

# Denorm for display
order_u8 = np.round(hydro_order_n * 255).astype(np.uint8)
lake_u16 = np.round(hydro_lake_n * 65535).astype(np.uint16)
lkdep_u8 = np.round(hydro_lkdep_n * 255).astype(np.uint8)
width_u8 = np.round(hydro_width_n * 255).astype(np.uint8)
depth_u8 = np.round(hydro_depth_n * 255).astype(np.uint8)

for x in range(x_start, x_end):
    pre  = pre_carve_y[z, x]
    post = surface_carved[z, x]
    cdep = pre - post
    meta = river_meta[z, x]
    marker = " <-- PROBLEM" if x == LOCAL_X else ""
    print(f"{x:4d} {x + COL_OFF:6d} {pre:5d} {post:5d} {cdep:5d} {META_NAMES.get(meta,'?'):>4s} "
          f"{order_u8[z,x]:4d} {lake_u16[z,x]:6d} {lkdep_u8[z,x]:5d} "
          f"{width_u8[z,x]:5d} {depth_u8[z,x]:5d} {height_raw[z,x]:6d}{marker}")

print()

# ── 6. Also print a vertical slice (vary z at fixed x = LOCAL_X) ──────────
print("=" * 100)
print(f"Vertical slice at x={LOCAL_X} (world X={WX}), z={LOCAL_Z-CROSS_HALF}..{LOCAL_Z+CROSS_HALF}")
print(f"{'z':>4} {'wZ':>6} {'preY':>5} {'postY':>5} {'carve':>5} {'meta':>4} "
      f"{'ord':>4} {'lake':>6} {'lkdep':>5} {'width':>5} {'depth':>5}")
print("-" * 100)

z_start = max(LOCAL_Z - CROSS_HALF, 0)
z_end   = min(LOCAL_Z + CROSS_HALF + 1, TILE_SIZE)
for zz in range(z_start, z_end):
    pre  = pre_carve_y[zz, LOCAL_X]
    post = surface_carved[zz, LOCAL_X]
    cdep = pre - post
    meta = river_meta[zz, LOCAL_X]
    marker = " <-- PROBLEM" if zz == LOCAL_Z else ""
    print(f"{zz:4d} {zz + ROW_OFF:6d} {pre:5d} {post:5d} {cdep:5d} {META_NAMES.get(meta,'?'):>4s} "
          f"{order_u8[zz,LOCAL_X]:4d} {lake_u16[zz,LOCAL_X]:6d} {lkdep_u8[zz,LOCAL_X]:5d} "
          f"{width_u8[zz,LOCAL_X]:5d} {depth_u8[zz,LOCAL_X]:5d}{marker}")

# ── 7. Summary stats at the junction ──────────────────────────────────────
print()
print("=" * 100)
print("SUMMARY — Junction Analysis")
print("=" * 100)

# Find where river meets lake along the cross-section row
river_pixels = []
lake_pixels = []
for x in range(x_start, x_end):
    m = river_meta[z, x]
    if m == 2: river_pixels.append(x)
    elif m == 3: lake_pixels.append(x)

if river_pixels:
    rx = river_pixels[-1]  # last river pixel (closest to lake)
    print(f"Last river pixel at x={rx}: preY={pre_carve_y[z,rx]}, postY={surface_carved[z,rx]}, carve={pre_carve_y[z,rx]-surface_carved[z,rx]}")
if lake_pixels:
    lx = lake_pixels[0]  # first lake pixel (closest to river)
    print(f"First lake pixel at x={lx}: preY={pre_carve_y[z,lx]}, postY={surface_carved[z,lx]}, carve={pre_carve_y[z,lx]-surface_carved[z,lx]}")
if river_pixels and lake_pixels:
    rx, lx = river_pixels[-1], lake_pixels[0]
    step = surface_carved[z, rx] - surface_carved[z, lx]
    print(f"Floor step (river→lake): {step} blocks ({surface_carved[z,rx]} → {surface_carved[z,lx]})")
    print(f"Gap between last river and first lake pixel: {lx - rx} pixels")

# Also check vertical slice
print()
river_z = []
lake_z = []
for zz in range(z_start, z_end):
    m = river_meta[zz, LOCAL_X]
    if m == 2: river_z.append(zz)
    elif m == 3: lake_z.append(zz)

if river_z:
    rz = river_z[-1] if river_z[-1] > LOCAL_Z else river_z[0]
    print(f"Nearest river z={rz}: preY={pre_carve_y[rz,LOCAL_X]}, postY={surface_carved[rz,LOCAL_X]}, carve={pre_carve_y[rz,LOCAL_X]-surface_carved[rz,LOCAL_X]}")
if lake_z:
    lz = lake_z[0] if lake_z[0] < LOCAL_Z else lake_z[-1]
    print(f"Nearest lake z={lz}: preY={pre_carve_y[lz,LOCAL_X]}, postY={surface_carved[lz,LOCAL_X]}, carve={pre_carve_y[lz,LOCAL_X]-surface_carved[lz,LOCAL_X]}")
if river_z and lake_z:
    step = surface_carved[rz, LOCAL_X] - surface_carved[lz, LOCAL_X]
    print(f"Floor step (river→lake) on vertical slice: {step} blocks")

print("\nDone.")
