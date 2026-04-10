"""Diagnostic script: tile (51, 53) forest clearing analysis."""
import numpy as np
import rasterio
from rasterio.windows import Window
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap, BoundaryNorm
from collections import OrderedDict

MASKS = r"C:\Users\nicho\minecraft-worldgen\masks"
OUT   = r"C:\Users\nicho\minecraft-worldgen\output\diag_tile_51_53_clearings.png"

# Tile window at 50k resolution
W = Window(col_off=26112, row_off=27136, width=512, height=512)

BIOME_NAMES = {
    0: "OCEAN", 10: "ARCTIC_TUNDRA", 20: "FROZEN_FLATS",
    30: "SNOWY_BOREAL_TAIGA", 40: "BOREAL_TAIGA", 50: "COASTAL_HEATH",
    60: "TEMPERATE_RAINFOREST", 70: "MIXED_FOREST", 80: "RIPARIAN_WOODLAND",
    90: "TEMPERATE_DECIDUOUS", 100: "CONTINENTAL_STEPPE",
    110: "DRY_PINE_BARRENS", 120: "SCRUBBY_HEATHLAND",
    130: "DRY_OAK_SAVANNA", 140: "DRY_WOODLAND_MAQUIS",
    150: "DESERT_STEPPE_TRANSITION", 160: "ALPINE_MEADOW",
    170: "SAND_DUNE_DESERT", 180: "KARST_BARRENS", 190: "TIDAL_MANGROVE",
    200: "SUBTROPICAL_HUMID", 210: "TROPICAL_MONSOON_FOREST",
    220: "JUNGLE_HIGHLANDS", 230: "MOSS_OLD_GROWTH", 240: "RAINFOREST_COAST",
}

FOREST_ZONES = {40, 60, 70, 90, 110, 130, 140, 200, 210, 220, 230, 240}

# Colors for biome display (rough palette)
BIOME_COLORS_RGB = {
    0: (0.1, 0.2, 0.6),       # ocean - blue
    10: (0.9, 0.95, 1.0),     # arctic tundra
    20: (0.85, 0.9, 0.95),    # frozen flats
    30: (0.3, 0.5, 0.4),      # snowy boreal
    40: (0.2, 0.45, 0.3),     # boreal taiga - dark green
    50: (0.6, 0.7, 0.5),      # coastal heath
    60: (0.1, 0.5, 0.3),      # temperate rainforest
    70: (0.3, 0.6, 0.3),      # mixed forest
    80: (0.4, 0.6, 0.5),      # riparian
    90: (0.4, 0.65, 0.25),    # temperate deciduous
    100: (0.7, 0.7, 0.4),     # steppe
    110: (0.55, 0.6, 0.35),   # dry pine
    120: (0.65, 0.6, 0.4),    # scrubby heath
    130: (0.6, 0.55, 0.3),    # dry oak savanna
    140: (0.5, 0.5, 0.3),     # maquis
    150: (0.8, 0.75, 0.5),    # desert steppe
    160: (0.5, 0.75, 0.5),    # alpine meadow
    170: (0.9, 0.85, 0.6),    # sand dune
    180: (0.8, 0.8, 0.7),     # karst
    190: (0.3, 0.55, 0.4),    # mangrove
    200: (0.35, 0.6, 0.2),    # subtropical humid
    210: (0.2, 0.55, 0.15),   # tropical monsoon
    220: (0.15, 0.5, 0.1),    # jungle highlands
    230: (0.25, 0.5, 0.25),   # moss old growth
    240: (0.2, 0.45, 0.2),    # rainforest coast
}

# ---------- Read data ----------
with rasterio.open(f"{MASKS}/override.tif") as src:
    zones = src.read(1, window=W)

with rasterio.open(f"{MASKS}/height.tif") as src:
    height = src.read(1, window=W)

with rasterio.open(f"{MASKS}/hydro_centerline.tif") as src:
    river = src.read(1, window=W)

with rasterio.open(f"{MASKS}/hydro_lake.tif") as src:
    lake = src.read(1, window=W)

with rasterio.open(f"{MASKS}/hydro_lake_wl.tif") as src:
    lake_wl = src.read(1, window=W)

total_px = zones.size

# ---------- Zone histogram ----------
unique, counts = np.unique(zones, return_counts=True)
print("=" * 60)
print(f"TILE (51, 53) BIOME ZONE HISTOGRAM  [{total_px} pixels]")
print("=" * 60)
forest_px = 0
for code, cnt in sorted(zip(unique, counts), key=lambda x: -x[1]):
    pct = 100.0 * cnt / total_px
    name = BIOME_NAMES.get(code, f"UNKNOWN_{code}")
    is_forest = code in FOREST_ZONES
    tag = " [FOREST]" if is_forest else ""
    print(f"  {code:>3d}  {name:<30s}  {cnt:>7d} px  {pct:5.1f}%{tag}")
    if is_forest:
        forest_px += cnt

print(f"\nTotal forest pixels: {forest_px}  ({100.0 * forest_px / total_px:.1f}%)")

# ---------- Water stats ----------
river_mask = river > 0
lake_mask = lake > 0
water_mask = river_mask | lake_mask
river_px = int(river_mask.sum())
lake_px = int(lake_mask.sum())
water_px = int(water_mask.sum())
print(f"\nRiver pixels:  {river_px}  ({100.0 * river_px / total_px:.1f}%)")
print(f"Lake pixels:   {lake_px}  ({100.0 * lake_px / total_px:.1f}%)")
print(f"Total water:   {water_px}  ({100.0 * water_px / total_px:.1f}%)")

# ---------- Height stats ----------
print(f"\nHeight range: {height.min():.1f} - {height.max():.1f}")
print(f"Mean height:  {height.mean():.1f}")

# ---------- Forest-only clearing candidate areas ----------
forest_mask = np.isin(zones, list(FOREST_ZONES))
# Clearings go in forests away from water
clearing_candidate = forest_mask & ~water_mask
print(f"\nClearing candidate area (forest, non-water): {int(clearing_candidate.sum())} px "
      f"({100.0 * clearing_candidate.sum() / total_px:.1f}%)")

# Per-forest-biome breakdown
print("\nPer-biome clearing candidate area:")
for code in sorted(FOREST_ZONES):
    mask_biome = (zones == code) & ~water_mask
    cnt = int(mask_biome.sum())
    if cnt > 0:
        name = BIOME_NAMES.get(code, f"UNKNOWN_{code}")
        print(f"  {code:>3d}  {name:<30s}  {cnt:>7d} px  {100.0 * cnt / total_px:5.1f}%")

# ---------- Figure ----------
fig, axes = plt.subplots(1, 3, figsize=(18, 6), dpi=120)

# Panel 1: biome zones
all_codes = sorted(BIOME_COLORS_RGB.keys())
colors_list = [BIOME_COLORS_RGB[c] for c in all_codes]
bounds = all_codes + [all_codes[-1] + 10]
cmap = ListedColormap(colors_list)
norm = BoundaryNorm(bounds, cmap.N)

ax = axes[0]
im = ax.imshow(zones, cmap=cmap, norm=norm, interpolation='nearest')
ax.set_title("Biome Zones (override.tif)")
ax.set_xlabel("col"); ax.set_ylabel("row")

# Panel 2: height + water overlay
ax = axes[1]
ax.imshow(height, cmap='terrain', interpolation='nearest')
# overlay rivers in blue
if river_px > 0:
    river_vis = np.ma.masked_where(~river_mask, river)
    ax.imshow(river_vis, cmap='Blues', alpha=0.7, interpolation='nearest')
# overlay lakes in darker blue
if lake_px > 0:
    lake_vis = np.ma.masked_where(~lake_mask, np.ones_like(lake))
    ax.imshow(lake_vis, cmap='cool', alpha=0.5, interpolation='nearest')
ax.set_title("Height + Water Overlay")
ax.set_xlabel("col"); ax.set_ylabel("row")

# Panel 3: clearing candidates
ax = axes[2]
display = np.zeros((*zones.shape, 3), dtype=np.float32)
# background gray
display[:] = 0.85
# color forest areas green
display[forest_mask] = [0.3, 0.65, 0.25]
# clearing candidates brighter
display[clearing_candidate] = [0.4, 0.8, 0.3]
# water blue
display[water_mask] = [0.2, 0.3, 0.8]
# non-forest, non-water in tan
other = ~forest_mask & ~water_mask
display[other] = [0.75, 0.7, 0.55]
ax.imshow(display, interpolation='nearest')
ax.set_title("Clearing Candidates (bright green)")
ax.set_xlabel("col"); ax.set_ylabel("row")

plt.suptitle("Tile (51, 53) — Forest Clearing Diagnostic", fontsize=14, fontweight='bold')
plt.tight_layout()
plt.savefig(OUT, bbox_inches='tight')
print(f"\nSaved figure to: {OUT}")
print("Done.")
