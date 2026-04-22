"""
S62 — paint FRESHWATER_FEN + RIPARIAN_WOODLAND patches onto override_final.

INPUTS (read-only):
    override_final.png           8192x8192 L — protected master
    masks/hydro_order.tif        50k uint8   — Strahler stream order
    masks/override.tif           50k uint8   — biome zones (for dry biome filter)

OUTPUT:
    override_final_s62.png       8192x8192 L — master + 3 patches
    memory/s62_patch_preview_fen1.png
    memory/s62_patch_preview_fen2.png
    memory/s62_patch_preview_riparian.png

Patches:
    1. FRESHWATER_FEN @ tile (30, 88) — ~300 blocks wide, landward of MANGROVE_COAST
    2. FRESHWATER_FEN @ tile (8, 73)  — ~400 blocks, lush rainforest transition floodplain
    3. RIPARIAN_WOODLAND — ~100-block corridor along big river through eastern steppe
       (Strahler >= 3 in CONTINENTAL_STEPPE / DRY_PINE_BARRENS, tiles 71-90 x 48-75)

Dithering:
    - Simplex-noise warp of radial distance (lobed, non-geometric boundary)
    - Per-pixel salt-and-pepper dither at the 20% edge band
"""

from __future__ import annotations
import numpy as np
import rasterio
from PIL import Image
from pathlib import Path
from opensimplex import OpenSimplex
from scipy.ndimage import binary_dilation, distance_transform_edt

REPO = Path(__file__).resolve().parents[1]
SRC_PNG    = REPO / "override_final.png"
OUT_PNG    = REPO / "override_final_s62.png"
HYDRO_ORD  = REPO / "masks" / "hydro_order.tif"
OVERRIDE   = REPO / "masks" / "override.tif"

MEMORY = REPO / "memory"
PREVIEW_FEN1     = MEMORY / "s62_patch_preview_fen1.png"
PREVIEW_FEN2     = MEMORY / "s62_patch_preview_fen2.png"
PREVIEW_RIPARIAN = MEMORY / "s62_patch_preview_riparian.png"
PREVIEW_OVERVIEW = MEMORY / "s62_patch_preview_overview.png"

# Zone codes
ZONE_FRESHWATER_FEN    = 240
ZONE_RIPARIAN_WOODLAND = 80
DRY_ZONES = {90, 130, 140, 150, 190, 200, 210}

# Color LUT for preview (mirrors tools/world_biome_map.py BIOME_COLORS)
ZONE_RGB = {
    0:   (30, 80, 160),       # _OCEAN
    10:  (180, 200, 140),     # COASTAL_HEATH
    20:  (30, 120, 60),       # TEMPERATE_RAINFOREST
    30:  (60, 130, 90),       # BOREAL_TAIGA
    35:  (180, 200, 220),     # SNOWY_BOREAL_TAIGA
    40:  (180, 200, 220),     # BOREAL_ALPINE (reuses SBT color, distinct via layout)
    50:  (220, 230, 240),     # ARCTIC_TUNDRA
    55:  (240, 245, 255),     # FROZEN_FLATS
    60:  (80, 160, 80),       # TEMPERATE_DECIDUOUS
    70:  (20, 160, 80),       # RAINFOREST_COAST
    80:  (60, 140, 100),      # RIPARIAN_WOODLAND  ← patch target
    90:  (190, 160, 80),      # DRY_OAK_SAVANNA
    100: (180, 170, 150),     # KARST_BARRENS
    110: (160, 200, 140),     # BIRCH_FOREST
    115: (120, 180, 130),     # EASTERN_TEMPERATE_COAST
    120: (60, 140, 70),       # MIXED_FOREST
    130: (200, 180, 100),     # CONTINENTAL_STEPPE
    140: (140, 160, 100),     # DRY_PINE_BARRENS
    150: (180, 160, 120),     # SCRUBBY_HEATHLAND
    160: (20, 140, 80),       # LUSH_RAINFOREST_COAST
    170: (230, 200, 120),     # SAND_DUNE_DESERT
    190: (210, 185, 120),     # DESERT_STEPPE_TRANSITION
    200: (200, 170, 110),     # SEMI_ARID_SHRUBLAND
    210: (170, 160, 100),     # DRY_WOODLAND_MAQUIS
    220: (40, 150, 100),      # TIDAL_JUNGLE_FRINGE
    230: (50, 140, 90),       # MANGROVE_COAST
    240: (80, 150, 130),      # FRESHWATER_FEN  ← patch target
}


def to_rgb(zones: np.ndarray) -> np.ndarray:
    """Convert (H,W) uint8 zone codes to (H,W,3) RGB."""
    rgb = np.zeros((*zones.shape, 3), dtype=np.uint8)
    for z, c in ZONE_RGB.items():
        mask = zones == z
        if mask.any():
            rgb[mask] = c
    return rgb


def noisy_blob_mask(shape, cx, cy, base_r, lobe_amp, lobe_scale, dither_band, seed):
    """
    Mask of a blob at (cx, cy) with radius `base_r`, warped by simplex lobes
    (amp=lobe_amp, wavelength=lobe_scale in pixels).  The outer `dither_band`
    fraction of the radius is salt-and-pepper softened.
    """
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dy = yy - cy
    dx = xx - cx
    r  = np.sqrt(dx*dx + dy*dy)

    # Simplex-warped radius.  Two octaves — big lobes + small bumps.
    sim_a = OpenSimplex(seed=seed)
    sim_b = OpenSimplex(seed=seed + 1)
    warp  = np.zeros_like(r)
    step  = 4  # sample every 4 px then upsample (speed — simplex is scalar)
    grid_y = np.arange(0, h, step)
    grid_x = np.arange(0, w, step)
    coarse = np.zeros((len(grid_y), len(grid_x)), dtype=np.float32)
    for gy_i, gy in enumerate(grid_y):
        for gx_i, gx in enumerate(grid_x):
            coarse[gy_i, gx_i] = (
                sim_a.noise2(gx / lobe_scale, gy / lobe_scale) * lobe_amp +
                sim_b.noise2(gx / (lobe_scale * 0.4),
                             gy / (lobe_scale * 0.4)) * (lobe_amp * 0.3)
            )
    # Bilinear upsample back to full resolution.
    from scipy.ndimage import zoom
    warp = zoom(coarse, (h / coarse.shape[0], w / coarse.shape[1]), order=1)[:h, :w]

    warped_r = base_r + warp
    # Inner solid region.
    inner = r < warped_r * (1.0 - dither_band)
    # Outer dither band.
    in_band = (r >= warped_r * (1.0 - dither_band)) & (r < warped_r)
    # Probability of being "in" at each pixel in the band: 1 at inner edge, 0 at outer.
    band_frac = (r - warped_r * (1.0 - dither_band)) / (warped_r * dither_band)
    band_frac = np.clip(band_frac, 0.0, 1.0)
    rng = np.random.default_rng(seed + 100)
    dither = rng.random(shape) > band_frac  # True = painted (more true near inner)
    out = inner | (in_band & dither)
    return out


print(f"Loading override_final.png...", flush=True)
src = np.array(Image.open(SRC_PNG))
assert src.shape == (8192, 8192), f"expected 8192x8192, got {src.shape}"
dst = src.copy()
H8K, W8K = src.shape
PX_PER_BLOCK = W8K / 50000.0  # ≈ 0.1638  (1 block == 0.164 px at 8k)
BLOCK_PER_PX = 50000.0 / W8K  # ≈ 6.10

def world_to_8k(world_xy: tuple[float, float]) -> tuple[float, float]:
    wx, wz = world_xy
    return wx * PX_PER_BLOCK, wz * PX_PER_BLOCK

# ─── Patch 1 — FRESHWATER_FEN at tile (30, 88) ──────────────────────────
# Tile center world = (15616, 45312).  ~300 block radius.
print("Painting FRESHWATER_FEN #1 (mangrove-adjacent)...", flush=True)
cx1_w, cz1_w = 30 * 512 + 256, 88 * 512 + 256
cx1, cz1 = world_to_8k((cx1_w, cz1_w))
# Use a local window for performance (blob is only ~60 px across at 8k).
BOX1 = 120  # ±120 px around center
y0, y1 = int(cz1) - BOX1, int(cz1) + BOX1
x0, x1 = int(cx1) - BOX1, int(cx1) + BOX1
y0, y1 = max(0, y0), min(H8K, y1)
x0, x1 = max(0, x0), min(W8K, x1)
local_cx1, local_cy1 = cx1 - x0, cz1 - y0
local_shape = (y1 - y0, x1 - x0)
fen1_mask = noisy_blob_mask(
    local_shape, local_cx1, local_cy1,
    base_r=30,           # 30 px @ 8k ≈ 183 blocks radius → ~366 block diameter
    lobe_amp=9,          # up to ±9 px warp (55 blocks)
    lobe_scale=22,       # large lobes
    dither_band=0.28,    # outer 28% dithered
    seed=240_1,
)
# Don't overwrite ocean
preserve = (dst[y0:y1, x0:x1] == 0)
dst[y0:y1, x0:x1] = np.where(fen1_mask & ~preserve,
                              ZONE_FRESHWATER_FEN, dst[y0:y1, x0:x1])

# ─── Patch 2 — FRESHWATER_FEN at tile (8, 73) ───────────────────────────
print("Painting FRESHWATER_FEN #2 (lush rainforest transition)...", flush=True)
cx2_w, cz2_w = 8 * 512 + 256, 73 * 512 + 256
cx2, cz2 = world_to_8k((cx2_w, cz2_w))
BOX2 = 140
y0, y1 = max(0, int(cz2) - BOX2), min(H8K, int(cz2) + BOX2)
x0, x1 = max(0, int(cx2) - BOX2), min(W8K, int(cx2) + BOX2)
local_cx2, local_cy2 = cx2 - x0, cz2 - y0
fen2_mask = noisy_blob_mask(
    (y1 - y0, x1 - x0), local_cx2, local_cy2,
    base_r=38,           # ~230 blocks radius → ~460 block diameter
    lobe_amp=12,
    lobe_scale=26,
    dither_band=0.25,
    seed=240_2,
)
preserve = (dst[y0:y1, x0:x1] == 0)
dst[y0:y1, x0:x1] = np.where(fen2_mask & ~preserve,
                              ZONE_FRESHWATER_FEN, dst[y0:y1, x0:x1])

# ─── Patch 3 — RIPARIAN_WOODLAND corridor (eastern steppe river) ────────
# Work at 8k resolution throughout — avoids 50k memory blow-up.
print("Computing RIPARIAN_WOODLAND corridor at 8k resolution...", flush=True)
from scipy.ndimage import zoom as _zoom
# ROI: tiles 71..92 x 48..75 (world z=24576..38400, x=36352..47104)
z_lo, z_hi = 48 * 512, 75 * 512
x_lo, x_hi = 71 * 512, 92 * 512
# Convert ROI to 8k px coords
y0_8k = int(round(z_lo * PX_PER_BLOCK))
y1_8k = int(round(z_hi * PX_PER_BLOCK))
x0_8k = int(round(x_lo * PX_PER_BLOCK))
x1_8k = int(round(x_hi * PX_PER_BLOCK))
roi_h, roi_w = y1_8k - y0_8k, x1_8k - x0_8k
print(f"  ROI at 8k: {roi_h} x {roi_w} px  (world: {z_hi-z_lo} x {x_hi-x_lo} blocks)")

# Read order + override at 8k using rasterio out_shape resampling.
# For thin rivers: use the FULL 50k array in this ROI only, then max-pool to 8k.
with rasterio.open(HYDRO_ORD) as hsrc:
    roi_order_50k = hsrc.read(1, window=((z_lo, z_hi), (x_lo, x_hi)))
with rasterio.open(OVERRIDE) as osrc:
    roi_ov_50k = osrc.read(1, window=((z_lo, z_hi), (x_lo, x_hi)))

big_river_in_dry_50k = (roi_order_50k >= 3) & np.isin(roi_ov_50k, list(DRY_ZONES))
print(f"  ROI river-in-dry px (50k): {int(big_river_in_dry_50k.sum())}")

# Max-pool to 8k — keeps thin rivers as 1-px wide at 8k.
# Use scipy zoom with order=0 and exact factor.
factor_y = roi_h / big_river_in_dry_50k.shape[0]
factor_x = roi_w / big_river_in_dry_50k.shape[1]
# Preserve thin features: convert to float, zoom with order=1 (bilinear), threshold.
big_river_8k = _zoom(big_river_in_dry_50k.astype(np.float32),
                     (factor_y, factor_x), order=1) > 0.15
# Also downsample override for the "stay in dry biome" gate.
ov_8k = _zoom(roi_ov_50k, (factor_y, factor_x), order=0)  # nearest
is_dry_8k = np.isin(ov_8k, list(DRY_ZONES))
print(f"  ROI river-in-dry px (8k): {int(big_river_8k.sum())}")

# Dilate corridor at 8k.  80-block buffer → 80 * PX_PER_BLOCK ≈ 13 px.
BUFFER_BLOCKS = 80
buf_px = max(1, int(round(BUFFER_BLOCKS * PX_PER_BLOCK)))
corridor_8k = binary_dilation(big_river_8k, iterations=buf_px)
# Meander warp at 8k.
dist_8k = distance_transform_edt(~big_river_8k)  # units: px at 8k
sim_r = OpenSimplex(seed=80_1)
h_r, w_r = dist_8k.shape
grid_sz = 4  # px at 8k
gy = np.arange(0, h_r, grid_sz)
gx = np.arange(0, w_r, grid_sz)
coarse_r = np.empty((len(gy), len(gx)), dtype=np.float32)
for i, y in enumerate(gy):
    for j, x in enumerate(gx):
        coarse_r[i, j] = sim_r.noise2(x / 60.0, y / 60.0) * 5  # ±5 px ≈ ±30 blocks
meander_8k = _zoom(coarse_r, (h_r / coarse_r.shape[0], w_r / coarse_r.shape[1]), order=1)[:h_r, :w_r]

# Warped corridor: distance + meander < buffer.  ±5 px amplitude on 13 px buffer.
corridor_warped = (dist_8k + meander_8k) < buf_px
# Salt-and-pepper dither at the outer 30% band.
band_lo = buf_px * 0.7
band_hi = buf_px * 1.0
in_band = (dist_8k + meander_8k >= band_lo) & (dist_8k + meander_8k < band_hi)
edge_frac = np.clip(((dist_8k + meander_8k) - band_lo) / (band_hi - band_lo), 0, 1)
rng = np.random.default_rng(80_2)
edge_keep = rng.random(dist_8k.shape) > edge_frac
corridor_final = corridor_warped | (in_band & edge_keep)
# Restrict to dry biomes only.
corridor_final = corridor_final & is_dry_8k
print(f"  corridor px (8k): {int(corridor_final.sum())}")

corr_8k = corridor_final

# Place in dst at corresponding 8k window.
y0_8k = int(round(z_lo * PX_PER_BLOCK))
x0_8k = int(round(x_lo * PX_PER_BLOCK))
y1_8k = y0_8k + corr_8k.shape[0]
x1_8k = x0_8k + corr_8k.shape[1]
y1_8k = min(y1_8k, H8K); x1_8k = min(x1_8k, W8K)
cth, ctw = y1_8k - y0_8k, x1_8k - x0_8k
corr_8k = corr_8k[:cth, :ctw]

window = dst[y0_8k:y1_8k, x0_8k:x1_8k]
preserve = (window == 0)
window_new = np.where(corr_8k & ~preserve, ZONE_RIPARIAN_WOODLAND, window)
dst[y0_8k:y1_8k, x0_8k:x1_8k] = window_new

# ─── Save master ────────────────────────────────────────────────────────
print(f"Saving modified master to {OUT_PNG}...", flush=True)
Image.fromarray(dst).save(OUT_PNG)

# ─── Previews ───────────────────────────────────────────────────────────
def preview_pair(src_arr, dst_arr, cx_px, cy_px, half_box, out_path, title):
    """2-panel preview (before | after) centered at (cx,cy) at 8k with half_box px radius."""
    y0, y1 = max(0, cy_px - half_box), min(H8K, cy_px + half_box)
    x0, x1 = max(0, cx_px - half_box), min(W8K, cx_px + half_box)
    before = to_rgb(src_arr[y0:y1, x0:x1])
    after  = to_rgb(dst_arr[y0:y1, x0:x1])
    # Stack side-by-side with 8-px gap
    gap = np.full((before.shape[0], 8, 3), 40, dtype=np.uint8)
    combined = np.hstack([before, gap, after])
    Image.fromarray(combined).save(out_path)
    print(f"  preview: {out_path}  (H={before.shape[0]}, W_each={before.shape[1]})")

print("Generating previews...", flush=True)
# Fen 1 preview — zoom box 180 px around center (≈ 1100 blocks)
preview_pair(src, dst, int(cx1), int(cz1), 180, PREVIEW_FEN1, "FEN #1")
# Fen 2 preview — zoom 200 px
preview_pair(src, dst, int(cx2), int(cz2), 200, PREVIEW_FEN2, "FEN #2")
# Riparian preview — zoom around corridor center (big box)
cen_x_w = (x_lo + x_hi) // 2
cen_z_w = (z_lo + z_hi) // 2
preview_pair(src, dst,
             int(cen_x_w * PX_PER_BLOCK), int(cen_z_w * PX_PER_BLOCK),
             1100, PREVIEW_RIPARIAN, "RIPARIAN")

# Overview — full 8192 downsampled to 1024 for quick look
before_full = to_rgb(src)
after_full  = to_rgb(dst)
gap = np.full((before_full.shape[0], 8, 3), 40, dtype=np.uint8)
combined_full = np.hstack([before_full, gap, after_full])
# Downsample to 2048 wide total
Image.fromarray(combined_full).resize((2048, 1024), Image.Resampling.NEAREST).save(PREVIEW_OVERVIEW)
print(f"  overview: {PREVIEW_OVERVIEW}")

# Summary
n_fen_new = int((dst == ZONE_FRESHWATER_FEN).sum() - (src == ZONE_FRESHWATER_FEN).sum())
n_rip_new = int((dst == ZONE_RIPARIAN_WOODLAND).sum() - (src == ZONE_RIPARIAN_WOODLAND).sum())
print(f"\n=== PAINT SUMMARY ===")
print(f"FRESHWATER_FEN pixels added: {n_fen_new} @ 8k  (~{n_fen_new * BLOCK_PER_PX**2:.0f} blocks²)")
print(f"RIPARIAN_WOODLAND pixels added: {n_rip_new} @ 8k  (~{n_rip_new * BLOCK_PER_PX**2:.0f} blocks²)")
print(f"Master saved: {OUT_PNG}")
print(f"Previews: {PREVIEW_FEN1.name}, {PREVIEW_FEN2.name}, {PREVIEW_RIPARIAN.name}, {PREVIEW_OVERVIEW.name}")
