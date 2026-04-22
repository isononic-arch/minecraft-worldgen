"""
S62 WORLD OVERVIEW — one comprehensive validation JPG.

Row 0: main biome map (with hillshade + rivers + lakes + ecology gaps + labels)
       + biome legend
Row 1: terrain | hydrology | flow
Row 2: rock gap | snow gap | sand dunes
Row 3: beach | windthrow | slope

Output: memory/s62_world_overview.jpg
"""

from __future__ import annotations
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.colors import LightSource, LinearSegmentedColormap
import rasterio
from rasterio.enums import Resampling
from pathlib import Path
from scipy.ndimage import label as _label, binary_dilation

REPO = Path(__file__).resolve().parents[1]
MASKS = REPO / "masks"
OUT_JPG = REPO / "memory" / "s62_world_overview.jpg"

ZONE_INFO: dict[int, tuple[str, tuple[int, int, int]]] = {
    0:   ("Ocean",                    (30, 80, 160)),
    10:  ("COASTAL_HEATH",            (180, 200, 140)),
    20:  ("TEMPERATE_RAINFOREST",     (30, 120, 60)),
    30:  ("BOREAL_TAIGA",             (60, 130, 90)),
    35:  ("SNOWY_BOREAL_TAIGA",       (180, 200, 220)),
    40:  ("BOREAL_ALPINE",            (150, 170, 200)),
    50:  ("ARCTIC_TUNDRA",            (220, 230, 240)),
    55:  ("FROZEN_FLATS",             (240, 245, 255)),
    60:  ("TEMPERATE_DECIDUOUS",      (80, 160, 80)),
    70:  ("RAINFOREST_COAST",         (20, 160, 80)),
    80:  ("RIPARIAN_WOODLAND",        (60, 140, 100)),
    90:  ("DRY_OAK_SAVANNA",          (190, 160, 80)),
    100: ("KARST_BARRENS",            (180, 170, 150)),
    110: ("BIRCH_FOREST",             (160, 200, 140)),
    115: ("EASTERN_TEMPERATE_COAST",  (120, 180, 130)),
    120: ("MIXED_FOREST",             (60, 140, 70)),
    130: ("CONTINENTAL_STEPPE",       (200, 180, 100)),
    140: ("DRY_PINE_BARRENS",         (140, 160, 100)),
    150: ("SCRUBBY_HEATHLAND",        (180, 160, 120)),
    160: ("LUSH_RAINFOREST_COAST",    (20, 140, 80)),
    170: ("SAND_DUNE_DESERT",         (230, 200, 120)),
    190: ("DESERT_STEPPE_TRANSITION", (210, 185, 120)),
    200: ("SEMI_ARID_SHRUBLAND",      (200, 170, 110)),
    210: ("DRY_WOODLAND_MAQUIS",      (170, 160, 100)),
    220: ("TIDAL_JUNGLE_FRINGE",      (40, 150, 100)),
    230: ("MANGROVE_COAST",           (50, 140, 90)),
    240: ("FRESHWATER_FEN",           (80, 150, 130)),
}

RES = 2500  # 1:20 downsample
print(f"Loading masks at {RES}x{RES}...", flush=True)

def _load(name, resampling=Resampling.nearest):
    path = MASKS / name
    if not path.exists():
        return None
    with rasterio.open(path) as src:
        return src.read(1, out_shape=(RES, RES), resampling=resampling)

override = _load("override.tif",           Resampling.nearest)
height   = _load("height.tif",             Resampling.bilinear)
flow     = _load("flow.tif",               Resampling.bilinear)
slope    = _load("slope.tif",              Resampling.bilinear)
hyd_ord  = _load("hydro_order.tif",        Resampling.nearest)
hyd_lake = _load("hydro_lake.tif",         Resampling.nearest)
hyd_fp   = _load("hydro_floodplain.tif",   Resampling.bilinear)
rock     = _load("rock_gap.tif",           Resampling.nearest)
snow     = _load("snow_gap.tif",           Resampling.nearest)
sand     = _load("sand_dunes.tif",         Resampling.bilinear)
beach    = _load("beach.tif",              Resampling.bilinear)
wind     = _load("wind_windthrow.tif",     Resampling.bilinear)

# Hillshade
hillshade = None
if height is not None:
    ls = LightSource(azdeg=315, altdeg=45)
    # HEIGHT POLARITY: LOW raw 16-bit = HIGH terrain (per CLAUDE.md)
    # Invert for correct hillshade direction.
    hillshade = ls.hillshade((65535 - height.astype(np.int32)).astype(np.float32),
                              vert_exag=2.0)

def zones_to_rgb(zones):
    rgb = np.zeros((*zones.shape, 3), dtype=np.uint8)
    for z, (_, c) in ZONE_INFO.items():
        rgb[zones == z] = c
    return rgb

biome_rgb = zones_to_rgb(override)

# Apply hillshade to biome colors — multiplicative, keep ocean flat
if hillshade is not None:
    hs = hillshade[..., None].astype(np.float32)
    ocean_mask = (override == 0)[..., None].astype(np.float32)
    shaded_factor = 0.60 + 0.40 * hs
    # Keep ocean unshaded (flat color)
    shaded_factor = np.where(ocean_mask > 0, 1.0, shaded_factor)
    biome_shaded = (biome_rgb.astype(np.float32) * shaded_factor).clip(0, 255).astype(np.uint8)
else:
    biome_shaded = biome_rgb

# Ecology gaps composite
eco = np.zeros((*override.shape, 4), dtype=np.float32)
def _overlay(mask, rgba):
    if mask is None:
        return
    if mask.dtype != bool:
        mask = mask > 0.3 if mask.dtype.kind == 'f' else mask > 0
    eco[mask] = rgba
_overlay(rock,  (160/255, 160/255, 170/255, 0.80))
_overlay(snow,  (245/255, 250/255, 255/255, 0.95))
_overlay(sand,  (230/255, 200/255, 120/255, 0.75))
_overlay(beach, (250/255, 230/255, 180/255, 0.80))
_overlay(wind,  (120/255,  80/255,  40/255, 0.55))

# Hydrology composite — dilate rivers so they're more visible at 1:20 scale
hyd_rgba = np.zeros((*override.shape, 4), dtype=np.float32)
if hyd_lake is not None:
    lakes = hyd_lake > 0
    hyd_rgba[lakes] = (0.10, 0.30, 0.70, 1.00)
if hyd_ord is not None:
    # Dilate 1 px so thin rivers survive display
    for s in range(1, 6):
        m = hyd_ord == s
        if m.sum() == 0:
            continue
        m_d = binary_dilation(m, iterations=1) if s < 3 else m
        col_strength = 0.30 + 0.14 * s
        hyd_rgba[m_d] = (0.15 + 0.03 * s, 0.40 + 0.04 * s, 0.85, col_strength)
if hyd_fp is not None:
    fp = hyd_fp > 0.3
    fp = fp & ~(hyd_rgba[..., 3] > 0)
    hyd_rgba[fp] = (0.35, 0.55, 0.85, 0.30)

# Region labels — one per biome, on largest contiguous component
labels = []
for z, (name, _) in ZONE_INFO.items():
    if z == 0:
        continue
    mask = override == z
    if not mask.any():
        continue
    lab, nlab = _label(mask)
    if nlab == 0:
        continue
    counts = np.bincount(lab.ravel())[1:]
    best = int(np.argmax(counts)) + 1
    blob_size = int(counts[best - 1])
    # Skip tiny components
    if blob_size < 800:
        continue
    ys, xs = np.where(lab == best)
    cy, cx = int(np.median(ys)), int(np.median(xs))
    labels.append((cx, cy, name, blob_size))

# Figure layout — 4 rows × 3 cols + legend on the right
print("Composing figure...", flush=True)
fig = plt.figure(figsize=(24, 28), dpi=150, facecolor='white')
gs = fig.add_gridspec(
    nrows=4, ncols=4,
    height_ratios=[5.0, 2.0, 2.0, 2.0],
    width_ratios=[3.5, 3.5, 3.5, 1.5],
    hspace=0.18, wspace=0.08,
    left=0.02, right=0.98, top=0.95, bottom=0.02,
)

# Main biome map — spans row 0, cols 0..2
ax_biome = fig.add_subplot(gs[0, 0:3])
ax_biome.imshow(biome_shaded, interpolation='nearest')
ax_biome.imshow(eco, interpolation='nearest')
ax_biome.imshow(hyd_rgba, interpolation='nearest')
for (cx, cy, name, sz) in labels:
    fontsize = 6.0 + min(4.0, (sz / 30000) * 3)  # bigger for bigger blobs
    ax_biome.text(cx, cy, name.replace("_", "\n"), ha='center', va='center',
                  fontsize=fontsize, color='black', fontweight='bold',
                  bbox=dict(boxstyle='round,pad=0.25', facecolor='white',
                            alpha=0.65, edgecolor='none'))
ax_biome.set_title(
    "Vandir — Biomes + Rivers/Lakes + Ecology Gaps  (rock=grey, snow=white, "
    "sand=tan, beach=cream, windthrow=brown, rivers/lakes=blue)",
    fontsize=12, fontweight='bold', pad=6)
ax_biome.set_xticks([]); ax_biome.set_yticks([])

# Legend — row 0 col 3
ax_legend = fig.add_subplot(gs[0, 3])
ax_legend.axis('off')
ax_legend.set_title("Biome legend", fontsize=12, fontweight='bold', pad=4, loc='left')
handles = [Patch(facecolor=np.array(c)/255, edgecolor='black', linewidth=0.4,
                 label=f"{n} ({z})")
           for z, (n, c) in ZONE_INFO.items()]
ax_legend.legend(handles=handles, loc='upper left', fontsize=8.5, frameon=False,
                 borderpad=0.5, labelspacing=0.45)

# Row 1 — terrain, hydrology-standalone, flow
def _finish(ax, title, fontsize=11):
    ax.set_title(title, fontsize=fontsize, fontweight='bold', pad=4)
    ax.set_xticks([]); ax.set_yticks([])

ax_terr = fig.add_subplot(gs[1, 0])
if height is not None:
    # Invert height so HIGH terrain = HIGH visual (classic terrain colormap)
    h_inv = 65535 - height.astype(np.int32)
    terrain_cmap = LinearSegmentedColormap.from_list(
        "terrain_v",
        [(0.10, 0.20, 0.50),       # deep ocean (low vis)
         (0.30, 0.60, 0.80),       # shallow water
         (0.55, 0.75, 0.45),       # plains
         (0.75, 0.65, 0.40),       # foothills
         (0.50, 0.40, 0.30),       # mountains
         (1.00, 1.00, 1.00)],      # peaks
    )
    ax_terr.imshow(h_inv, cmap=terrain_cmap, interpolation='bilinear')
    ax_terr.contour(h_inv, levels=12, colors='black', linewidths=0.15, alpha=0.30)
else:
    ax_terr.text(0.5, 0.5, "height.tif missing", ha='center', va='center',
                 transform=ax_terr.transAxes)
_finish(ax_terr, "Elevation (HIGH terrain = HIGH visual)")

ax_hydro = fig.add_subplot(gs[1, 1])
base = np.full((*override.shape, 3), 232, dtype=np.uint8)
if hillshade is not None:
    base = (base.astype(np.float32) * (0.55 + 0.45 * hillshade[..., None])).astype(np.uint8)
ax_hydro.imshow(base, interpolation='bilinear')
ax_hydro.imshow(hyd_rgba, interpolation='nearest')
_finish(ax_hydro, "Hydrology — rivers (Strahler) + lakes + floodplain")

ax_flow = fig.add_subplot(gs[1, 2])
if flow is not None:
    flow_log = np.log1p(flow.astype(np.float32))
    ax_flow.imshow(flow_log, cmap='viridis', interpolation='bilinear')
else:
    ax_flow.text(0.5, 0.5, "flow.tif missing", ha='center', va='center',
                 transform=ax_flow.transAxes)
_finish(ax_flow, "Flow accumulation (log)")

# Secondary legend — col 3 across rows 1..3 (brief reminder of hydro codes)
ax_legend2 = fig.add_subplot(gs[1:4, 3])
ax_legend2.axis('off')
ax_legend2.set_title("Mask legend", fontsize=12, fontweight='bold', pad=4, loc='left')
mask_handles = [
    Patch(facecolor=(160/255, 160/255, 170/255), label="rock exposure (gap=5)"),
    Patch(facecolor=(245/255, 250/255, 255/255), edgecolor='grey', label="snow dusting (gap=7)"),
    Patch(facecolor=(230/255, 200/255, 120/255), label="sand dunes (gap=8)"),
    Patch(facecolor=(250/255, 230/255, 180/255), label="beach (gap=9)"),
    Patch(facecolor=(120/255,  80/255,  40/255), label="windthrow (gap=2)"),
    Patch(facecolor=(0.20, 0.40, 0.90),          label="river (hydro_order >=1)"),
    Patch(facecolor=(0.10, 0.30, 0.70),          label="lake (hydro_lake)"),
    Patch(facecolor=(0.35, 0.55, 0.85),          label="floodplain (hydro_floodplain)"),
]
ax_legend2.legend(handles=mask_handles, loc='upper left', fontsize=9, frameon=False,
                  borderpad=0.5, labelspacing=0.6)

# Rows 2-3 — gap masks (single-color tint on light background)
def _tint_panel(arr, color, title, ax):
    if arr is None:
        ax.text(0.5, 0.5, f"{title.split('(')[0].strip()}: mask missing",
                ha='center', va='center', transform=ax.transAxes)
        _finish(ax, title, fontsize=10)
        return
    m = arr > 0 if arr.dtype.kind != 'f' else arr > 0.3
    img = np.full((*arr.shape, 3), 240, dtype=np.uint8)
    if hillshade is not None:
        img = (img.astype(np.float32) * (0.70 + 0.30 * hillshade[..., None])).astype(np.uint8)
    col = tuple(int(c * 255) for c in color[:3])
    img[m] = col
    ax.imshow(img, interpolation='nearest')
    _finish(ax, f"{title}  ({int(m.sum()):,} px)", fontsize=10)

_tint_panel(rock,  (160/255, 160/255, 170/255, 1.0), "Rock exposure (gap=5)",
            fig.add_subplot(gs[2, 0]))
_tint_panel(snow,  (245/255, 250/255, 255/255, 1.0), "Snow dusting (gap=7)",
            fig.add_subplot(gs[2, 1]))
_tint_panel(sand,  (230/255, 200/255, 120/255, 1.0), "Sand dunes (gap=8)",
            fig.add_subplot(gs[2, 2]))
_tint_panel(beach, (250/255, 230/255, 180/255, 1.0), "Beach (gap=9)",
            fig.add_subplot(gs[3, 0]))
_tint_panel(wind,  (120/255,  80/255,  40/255, 1.0), "Windthrow (gap=2)",
            fig.add_subplot(gs[3, 1]))

# Slope panel (continuous)
ax_slope = fig.add_subplot(gs[3, 2])
if slope is not None:
    ax_slope.imshow(slope, cmap='magma', interpolation='bilinear')
    _finish(ax_slope, "Slope (Gaea-normalized)", fontsize=10)
else:
    ax_slope.text(0.5, 0.5, "slope.tif missing", ha='center', va='center',
                  transform=ax_slope.transAxes)
    _finish(ax_slope, "Slope", fontsize=10)

fig.suptitle("Vandir — World Overview (S62, 1:20 scale)",
             fontsize=20, fontweight='bold', y=0.985)

print(f"Saving {OUT_JPG}...", flush=True)
fig.savefig(OUT_JPG, dpi=150, bbox_inches='tight', facecolor='white',
            pil_kwargs={'quality': 90, 'optimize': True})
plt.close(fig)

import os
sz = os.path.getsize(OUT_JPG) / 1024 / 1024
print(f"\n=== DONE ===")
print(f"Output: {OUT_JPG}  ({sz:.1f} MB)")
print(f"Biomes labeled: {len(labels)}")
