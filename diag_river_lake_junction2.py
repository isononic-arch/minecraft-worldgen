"""
Diagnostic #2: Find the actual river-lake boundary near (462, 89) on tile (51,53).
Scan a wider area to locate river pixels.
"""
import sys, json
import numpy as np
import rasterio
from rasterio.windows import Window

TILE_X, TILE_Z = 51, 53
TILE_SIZE = 512
COL_OFF = TILE_X * TILE_SIZE
ROW_OFF = TILE_Z * TILE_SIZE
LOCAL_X, LOCAL_Z = 462, 89

MASK_DIR = r"C:\Users\nicho\minecraft-worldgen\masks"
CFG_PATH = r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json"
GAEA_IN  = [0, 11602, 26921, 35435, 43437, 65496]
MC_Y_OUT = [-64, 63, 120, 278, 398, 448]

def load_mask(name):
    with rasterio.open(f"{MASK_DIR}/{name}.tif") as src:
        return src.read(1, window=Window(COL_OFF, ROW_OFF, TILE_SIZE, TILE_SIZE))

height_raw  = load_mask("height")
hydro_order = load_mask("hydro_order")
hydro_lake  = load_mask("hydro_lake")
hydro_width = load_mask("hydro_width")
hydro_depth = load_mask("hydro_depth")
hydro_lkdep = load_mask("hydro_lkdep")
flow        = load_mask("flow")

def ensure_norm(arr):
    if arr.dtype in (np.float32, np.float64) and arr.max() <= 1.01:
        return arr.astype(np.float32)
    elif arr.dtype == np.uint8:
        return arr.astype(np.float32) / 255.0
    elif arr.dtype == np.uint16:
        return arr.astype(np.float32) / 65535.0
    else:
        return arr.astype(np.float32) / max(arr.max(), 1)

# Convert height to MC Y
h_u16 = height_raw if height_raw.dtype == np.uint16 else np.round(height_raw.astype(np.float32) * 65535).astype(np.uint16)
surface_y = np.interp(h_u16.astype(np.float64), GAEA_IN, MC_Y_OUT).astype(np.int16)
pre_carve = surface_y.copy()

# Run carver
with open(CFG_PATH) as f:
    cfg = json.load(f)
sys.path.insert(0, r"C:\Users\nicho\minecraft-worldgen")
from core.river_carver_v2 import carve_rivers

surface_carved, river_meta = carve_rivers(
    surface_y=surface_y.copy(),
    flow_tile=ensure_norm(flow),
    river_tile=np.zeros_like(surface_y),
    cfg=cfg,
    hydro_order=ensure_norm(hydro_order),
    hydro_width=ensure_norm(hydro_width),
    hydro_depth=ensure_norm(hydro_depth),
    hydro_lake=ensure_norm(hydro_lake),
    hydro_lkdep=ensure_norm(hydro_lkdep),
)

META_NAMES = {0: "none", 1: "strm", 2: "rivr", 3: "lake"}

# ── 1. Find all river pixels on the tile ──────────────────────────────────
river_mask = (river_meta == 2) | (river_meta == 1)
lake_mask  = river_meta == 3
print(f"Total river pixels: {river_mask.sum()}")
print(f"Total lake pixels:  {lake_mask.sum()}")
print(f"Total stream+river: {river_mask.sum()}")

if river_mask.any():
    rz, rx = np.where(river_mask)
    # Find closest river pixel to our problem point
    dists = np.sqrt((rx - LOCAL_X)**2 + (rz - LOCAL_Z)**2)
    closest_idx = np.argmin(dists)
    cr_x, cr_z = rx[closest_idx], rz[closest_idx]
    print(f"\nClosest river pixel to ({LOCAL_X},{LOCAL_Z}): ({cr_x},{cr_z}), dist={dists[closest_idx]:.1f}")
    print(f"  preY={pre_carve[cr_z,cr_x]}, postY={surface_carved[cr_z,cr_x]}, carve={pre_carve[cr_z,cr_x]-surface_carved[cr_z,cr_x]}")
    print(f"  hydro_order={hydro_order[cr_z,cr_x]}, meta={META_NAMES[river_meta[cr_z,cr_x]]}")

    # Now draw a cross-section from that river pixel toward the problem point
    # and beyond into the lake
    print(f"\n{'='*110}")
    print(f"Cross-section from river pixel ({cr_x},{cr_z}) through problem ({LOCAL_X},{LOCAL_Z})")

    # Use a line from river to problem point, extended
    dx = LOCAL_X - cr_x
    dz = LOCAL_Z - cr_z
    length = max(abs(dx), abs(dz), 1)

    # Extend line 20px beyond problem point in both directions
    EXTEND = 30
    total_pts = length + 2 * EXTEND

    print(f"Direction: dx={dx}, dz={dz}, length={length}")
    print(f"{'i':>4} {'x':>4} {'z':>4} {'wX':>6} {'wZ':>6} {'preY':>5} {'postY':>5} {'carve':>5} {'meta':>4} "
          f"{'ord':>4} {'lake':>6} {'lkdep':>5} {'h_raw':>6}")
    print("-" * 110)

    for i in range(-EXTEND, length + EXTEND + 1):
        t = i / max(length, 1)
        x = int(round(cr_x + dx * t))
        z = int(round(cr_z + dz * t))
        if 0 <= x < TILE_SIZE and 0 <= z < TILE_SIZE:
            pre  = pre_carve[z, x]
            post = surface_carved[z, x]
            cdep = pre - post
            meta = river_meta[z, x]
            marker = ""
            if x == LOCAL_X and z == LOCAL_Z:
                marker = " <-- PROBLEM"
            elif x == cr_x and z == cr_z:
                marker = " <-- RIVER"
            print(f"{i:4d} {x:4d} {z:4d} {x+COL_OFF:6d} {z+ROW_OFF:6d} "
                  f"{pre:5d} {post:5d} {cdep:5d} {META_NAMES.get(meta,'?'):>4s} "
                  f"{hydro_order[z,x]:4d} {hydro_lake[z,x] if hydro_lake.dtype==np.uint16 else int(hydro_lake[z,x]*65535):6d} "
                  f"{hydro_lkdep[z,x]:5d} {height_raw[z,x]:6d}{marker}")
else:
    print("NO RIVER PIXELS ON THIS TILE")
    # Check raw hydro_order
    print(f"hydro_order nonzero pixels: {(hydro_order > 0).sum()}")
    print(f"hydro_lake nonzero pixels: {(hydro_lake > 0).sum()}")

# ── 2. Check neighboring tiles for river pixels ──────────────────────────
print(f"\n{'='*110}")
print("Checking hydro_order on neighboring tiles for river presence:")
for dtx, dtz in [(-1,0),(1,0),(0,-1),(0,1)]:
    tx, tz = TILE_X+dtx, TILE_Z+dtz
    co, ro = tx*512, tz*512
    try:
        with rasterio.open(f"{MASK_DIR}/hydro_order.tif") as src:
            ho = src.read(1, window=Window(co, ro, 512, 512))
        n = (ho > 0).sum()
        print(f"  Tile ({tx},{tz}): {n} river pixels, max order={ho.max()}")
    except:
        print(f"  Tile ({tx},{tz}): read error")

# ── 3. Check the actual hydro masks at the problem row more carefully ─────
print(f"\n{'='*110}")
print(f"Raw hydro_order along z={LOCAL_Z}, full tile width scan for nonzero:")
nonzero_x = np.where(hydro_order[LOCAL_Z, :] > 0)[0]
if len(nonzero_x) > 0:
    print(f"  River at x={nonzero_x.tolist()}, orders={hydro_order[LOCAL_Z, nonzero_x].tolist()}")
else:
    print("  No river pixels at this z row")
    # Scan all rows
    for zz in range(0, TILE_SIZE, 8):
        nz = np.where(hydro_order[zz, :] > 0)[0]
        if len(nz) > 0:
            print(f"  z={zz}: river at x={nz[:5].tolist()}... ({len(nz)} px), orders={hydro_order[zz, nz[:5]].tolist()}")

# ── 4. Lake edge detail ──────────────────────────────────────────────────
print(f"\n{'='*110}")
print("Lake boundary — scanning for lake edge along each row near problem z:")
for zz in range(max(LOCAL_Z-5, 0), min(LOCAL_Z+6, TILE_SIZE)):
    lake_row = river_meta[zz, :] == 3
    if lake_row.any():
        idxs = np.where(lake_row)[0]
        lo, hi = idxs[0], idxs[-1]
        # Check if there's a non-lake pixel just outside
        pre_lo = pre_carve[zz, lo] if lo > 0 else -1
        post_lo = surface_carved[zz, lo]
        pre_outside = pre_carve[zz, max(lo-1,0)]
        post_outside = surface_carved[zz, max(lo-1,0)]
        meta_outside = META_NAMES.get(river_meta[zz, max(lo-1,0)], '?')
        print(f"  z={zz}: lake x=[{lo}..{hi}] | outside(x={lo-1}): preY={pre_outside} postY={post_outside} meta={meta_outside} | "
              f"inside(x={lo}): preY={pre_lo} postY={post_lo} step={post_outside - post_lo}")

print("\nDone.")
