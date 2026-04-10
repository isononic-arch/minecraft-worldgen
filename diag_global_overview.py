"""
diag_global_overview.py
Generates a low-res holistic view of the full 50k x 50k map:
  1. terrain_overview.png  — full map colored by MC Y elevation
  2. profile_overview.png  — cross-section profiles at 3 latitudes
  3. biome_overview.png    — global biome distribution map
All outputs written to validation_report/global/
"""
import sys, os
sys.path.insert(0, r'C:\Users\nicho\minecraft-worldgen')

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont

MASK_DIR  = r'C:\Users\nicho\minecraft-worldgen\masks'
OUT_DIR   = r'C:\Users\nicho\minecraft-worldgen\validation_report\global'
SIZE      = 1000   # downsample target (1000x1000 = 50:1 from 50k)

os.makedirs(OUT_DIR, exist_ok=True)

# ── 1. Read masks at low resolution ──────────────────────────────────────────
print("Reading masks...")

def read_u16(name):
    with rasterio.open(os.path.join(MASK_DIR, f'{name}.tif')) as src:
        return src.read(1, out_shape=(SIZE, SIZE)).astype(np.uint16)

def read_f32(name):
    return read_u16(name).astype(np.float32) / 65535.0

def read_override():
    # override.tif is uint8 (zone codes 0-255), NOT uint16
    with rasterio.open(os.path.join(MASK_DIR, 'override.tif')) as src:
        raw = src.read(1, out_shape=(SIZE, SIZE)).astype(np.uint8)
    return raw.astype(np.float32) / 255.0

height_u16 = read_u16('height')
slope_f    = read_f32('slope')
flow_f     = read_f32('flow')
erosion_f  = read_f32('erosion')

# ── 2. Apply spline LUT → MC Y ───────────────────────────────────────────────
from core.column_generator import _LUT
from scipy.ndimage import distance_transform_edt
import json as _json

surface_y = _LUT[height_u16].astype(np.int16)   # (1000, 1000)  MC Y values

SEA_LEVEL = 63

# Apply ocean depth correction (mirrors process_tile_columns_v2 step 1a)
with open(r'C:\Users\nicho\minecraft-worldgen\config\thresholds.json') as _f:
    _cfg = _json.load(_f)
_od = _cfg.get("ocean_depth", {})
_transition_px   = _od.get("transition_px", 30) / 50.0  # scale to overview pixels (50:1)
_min_ocean_depth = _od.get("min_depth", 15)

_land_mask = surface_y >= SEA_LEVEL
_dist      = distance_transform_edt(~_land_mask).astype(np.float32)
_blend     = np.clip(_dist / _transition_px, 0.0, 1.0)
_min_sy    = SEA_LEVEL - _min_ocean_depth
_corrected = np.minimum(surface_y, _min_sy).astype(np.int32)
surface_y  = np.where(
    ~_land_mask,
    np.round(surface_y * (1.0 - _blend) + _corrected * _blend).astype(np.int32),
    surface_y,
).astype(np.int16)

# ── 3. Terrain colormap ───────────────────────────────────────────────────────
print("Rendering terrain_overview.png...")

# Color ramp keyed on MC Y  (R, G, B)
# Y=-55 deep ocean  → (10,  30,  80)
# Y=-10 shallow sea → (30,  80, 160)
# Y= 63 sea level   → (65, 152, 210)  (light blue nearshore)
# Y= 65 low land    → (90, 155,  80)  (green)
# Y=100 lowland     → (110,155,  70)
# Y=150 hills       → (140,130,  70)
# Y=200 highland    → (160,120,  60)
# Y=280 mountain    → (180,170, 150)
# Y=448 peak        → (240,240, 240)

RAMP_Y   = np.array([-55, -10,  62,  63,  80, 120, 160, 220, 280, 448], dtype=np.float32)
RAMP_R   = np.array([ 10,  30,  60,  90, 100, 120, 145, 165, 195, 240], dtype=np.float32)
RAMP_G   = np.array([ 30,  80, 140, 155, 150, 140, 125, 150, 170, 240], dtype=np.float32)
RAMP_B   = np.array([ 80, 160, 210,  80,  70,  70,  60,  90, 150, 240], dtype=np.float32)

sy_f = surface_y.astype(np.float32)
r = np.interp(sy_f, RAMP_Y, RAMP_R).astype(np.uint8)
g = np.interp(sy_f, RAMP_Y, RAMP_G).astype(np.uint8)
b = np.interp(sy_f, RAMP_Y, RAMP_B).astype(np.uint8)

terrain_img = Image.fromarray(np.stack([r, g, b], axis=2), 'RGB')
terrain_img = terrain_img.resize((1000, 1000), Image.NEAREST)

# Draw sea level contour (coastline) — mark pixels within 1 block of sea level
coast = ((surface_y >= SEA_LEVEL - 1) & (surface_y <= SEA_LEVEL + 2)).astype(np.uint8)
rgba = np.stack([r, g, b, np.full_like(r, 255)], axis=2)
rgba[coast == 1] = [255, 255, 255, 255]
terrain_img = Image.fromarray(rgba, 'RGBA').convert('RGB')
terrain_img.save(os.path.join(OUT_DIR, 'terrain_overview.png'))
print(f"  saved terrain_overview.png  (Y range: {surface_y.min()} to {surface_y.max()})")

# ── 4. Cross-section profiles ─────────────────────────────────────────────────
print("Rendering profile_overview.png...")

W, H = 1000, 400
CANVAS_H = H * 3 + 40   # 3 profiles stacked
canvas = Image.new('RGB', (W, CANVAS_H), (30, 30, 40))
draw   = ImageDraw.Draw(canvas)

MC_Y_MIN, MC_Y_MAX = -64, 448
Y_RANGE = MC_Y_MAX - MC_Y_MIN

def y_to_px(mc_y, panel_top, panel_h):
    """Map MC Y to pixel row within a panel (top=high, bottom=low)."""
    frac = (mc_y - MC_Y_MIN) / Y_RANGE
    return panel_top + int((1.0 - frac) * (panel_h - 2))

PROFILE_ROWS = [SIZE // 6, SIZE // 2, SIZE * 5 // 6]
LABELS = ['North third', 'Mid (equator)', 'South third']
COLOURS = [(80, 180, 120), (200, 160, 80), (120, 160, 220)]

for pi, (row_idx, label, col) in enumerate(zip(PROFILE_ROWS, LABELS, COLOURS)):
    panel_top = pi * (H + 13)
    panel_bot = panel_top + H

    # Background
    draw.rectangle([(0, panel_top), (W-1, panel_bot)], fill=(20, 24, 35))

    # Sea level line
    sl_px = y_to_px(SEA_LEVEL, panel_top, H)
    draw.line([(0, sl_px), (W-1, sl_px)], fill=(0, 180, 220), width=1)

    # Bedrock line
    bk_px = y_to_px(-64, panel_top, H)
    draw.line([(0, bk_px), (W-1, bk_px)], fill=(60, 60, 60), width=1)

    # Terrain profile
    profile = surface_y[row_idx, :]   # (1000,)
    pts = []
    for x, mc_y in enumerate(profile):
        pts.append((x, y_to_px(int(mc_y), panel_top, H)))

    # Filled silhouette
    poly = [(0, panel_bot)] + pts + [(W-1, panel_bot)]
    fill_col = tuple(max(0, c - 60) for c in col)
    draw.polygon(poly, fill=fill_col)
    # Outline
    for i in range(len(pts)-1):
        draw.line([pts[i], pts[i+1]], fill=col, width=2)

    # Label
    draw.text((6, panel_top + 4), f"{label}  (map row {row_idx*50})", fill=(200, 200, 200))

    # Y axis ticks
    for tick_y in range(-50, 450, 50):
        tp = y_to_px(tick_y, panel_top, H)
        draw.line([(0, tp), (4, tp)], fill=(120,120,120))
        draw.text((6, tp - 5), f"Y{tick_y}", fill=(100,100,100))

draw.text((W//2 - 80, CANVAS_H - 12), "sea level = cyan  |  X axis = west→east", fill=(120,120,120))
canvas.save(os.path.join(OUT_DIR, 'profile_overview.png'))
print(f"  saved profile_overview.png")

# ── 5. Biome overview ─────────────────────────────────────────────────────────
print("Running biome assignment on downsampled map...")

from core.biome_assignment import assign_biomes
from core.noise_fields import load_noise_generators
import json

with open(r'C:\Users\nicho\minecraft-worldgen\config\thresholds.json') as f:
    cfg = json.load(f)

override_f32 = read_override()

# Build minimal noise_fields (no real generators needed for overview — pass None, assign_biomes may handle)
try:
    noise_gens = load_noise_generators(cfg)
except Exception:
    noise_gens = None

# assign_biomes signature: (height, slope, flow, erosion, override, noise_fields, cfg, tile_x, tile_y)
# For global overview pass tile coords 0,0 — biome bands may be slightly off but good enough
height_f32 = height_u16.astype(np.float32) / 65535.0

try:
    biome_grid = assign_biomes(
        height_f32, slope_f, flow_f, erosion_f,
        override_f32,
        noise_gens, cfg, 0, 0
    )
    print("  biome assignment OK")
except Exception as e:
    print(f"  biome assignment failed ({e}) — using elevation-only fallback")
    biome_grid = None

from core.preview_renderer import BIOME_COLORS, BLOCK_COLORS
from core.column_generator import _BIOME_BASE_BLOCKS

def _grid_to_rgb(grid, color_map, default=(128, 128, 128)):
    """Vectorized: object ndarray of strings → (H,W,3) uint8 RGB."""
    out = np.full((*grid.shape, 3), default, dtype=np.uint8)
    for name, col in color_map.items():
        mask = grid == name
        if mask.any():
            out[mask] = col
    return out

if biome_grid is not None:
    # ── Biome overview (vectorized) ───────────────────────────────────────────
    biome_img_arr = _grid_to_rgb(biome_grid, BIOME_COLORS)
    Image.fromarray(biome_img_arr, 'RGB').save(os.path.join(OUT_DIR, 'biome_overview.png'))
    print(f"  saved biome_overview.png")

    # ── Surface block overview ────────────────────────────────────────────────
    # Map each pixel biome → base surface block string
    surf_blk_grid = np.full(biome_grid.shape, "stone", dtype=object)
    for biome_name, (surf_blk, _sub) in _BIOME_BASE_BLOCKS.items():
        mask = biome_grid == biome_name
        if mask.any():
            surf_blk_grid[mask] = surf_blk
    # Ocean / sub-sea pixels → water colour
    ocean_mask = biome_grid == "_OCEAN"
    surf_blk_grid[ocean_mask] = "water"

    block_img_arr = _grid_to_rgb(surf_blk_grid, BLOCK_COLORS, default=(0x8A, 0x8A, 0x8A))
    Image.fromarray(block_img_arr, 'RGB').save(os.path.join(OUT_DIR, 'surface_block_overview.png'))
    print(f"  saved surface_block_overview.png")
else:
    print("  skipped biome_overview.png and surface_block_overview.png")

print(f"\nAll outputs -> {OUT_DIR}")
