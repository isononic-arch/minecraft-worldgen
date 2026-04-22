"""
S62 companion paint — apply the same 3 patches to override_vectorized.png so
the upscale composite preserves them.

The vectorized PNG is NOT a boundary-only overlay as the script docs imply —
it's effectively a full zone map with pre-smoothed borders.  The upscale
composite `np.where(vec_arr > 0, vec_arr, base_arr)` uses vec_arr wherever
non-zero, which is ~99.99% of the map.  Without also patching the vectorized
file, zones 80 + 240 get entirely overwritten during upscale.

This reuses the EXACT same blob/corridor computation as paint_s62_patches.py
(same seeds) so the results are pixel-identical on the patch regions.
"""

from __future__ import annotations
import numpy as np
from PIL import Image
from pathlib import Path
from opensimplex import OpenSimplex
from scipy.ndimage import binary_dilation, distance_transform_edt, zoom as _zoom
import rasterio

REPO = Path(__file__).resolve().parents[1]
VEC_PNG    = REPO / "override_vectorized.png"
VEC_BACKUP = REPO / "override_vectorized_pre_s62.png"
HYDRO_ORD  = REPO / "masks" / "hydro_order.tif"
OVERRIDE   = REPO / "masks" / "override.tif"   # old (pre-patch) 50k — safe to read, we only use for dry mask

ZONE_FRESHWATER_FEN    = 240
ZONE_RIPARIAN_WOODLAND = 80
DRY_ZONES = {90, 130, 140, 150, 190, 200, 210}


def noisy_blob_mask(shape, cx, cy, base_r, lobe_amp, lobe_scale, dither_band, seed):
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dy = yy - cy; dx = xx - cx
    r = np.sqrt(dx*dx + dy*dy)
    sim_a = OpenSimplex(seed=seed)
    sim_b = OpenSimplex(seed=seed + 1)
    step = 4
    gy = np.arange(0, h, step); gx = np.arange(0, w, step)
    coarse = np.zeros((len(gy), len(gx)), dtype=np.float32)
    for gy_i, y in enumerate(gy):
        for gx_i, x in enumerate(gx):
            coarse[gy_i, gx_i] = (
                sim_a.noise2(x / lobe_scale, y / lobe_scale) * lobe_amp +
                sim_b.noise2(x / (lobe_scale * 0.4),
                             y / (lobe_scale * 0.4)) * (lobe_amp * 0.3)
            )
    warp = _zoom(coarse, (h / coarse.shape[0], w / coarse.shape[1]), order=1)[:h, :w]
    warped_r = base_r + warp
    inner  = r < warped_r * (1.0 - dither_band)
    in_band = (r >= warped_r * (1.0 - dither_band)) & (r < warped_r)
    band_frac = np.clip((r - warped_r * (1.0 - dither_band)) / (warped_r * dither_band), 0, 1)
    rng = np.random.default_rng(seed + 100)
    dither = rng.random(shape) > band_frac
    return inner | (in_band & dither)


print(f"Backing up {VEC_PNG.name} -> {VEC_BACKUP.name}...", flush=True)
import shutil; shutil.copy(VEC_PNG, VEC_BACKUP)
dst = np.array(Image.open(VEC_PNG))
H8K, W8K = dst.shape
PX_PER_BLOCK = W8K / 50000.0
print(f"  shape={dst.shape}, mode=L")

# FEN #1
print("Patch 1 (FEN @ tile 30,88)...", flush=True)
cx1_w, cz1_w = 30 * 512 + 256, 88 * 512 + 256
cx1 = cx1_w * PX_PER_BLOCK; cz1 = cz1_w * PX_PER_BLOCK
BOX1 = 120
y0, y1 = max(0, int(cz1) - BOX1), min(H8K, int(cz1) + BOX1)
x0, x1 = max(0, int(cx1) - BOX1), min(W8K, int(cx1) + BOX1)
mask1 = noisy_blob_mask((y1-y0, x1-x0), cx1-x0, cz1-y0, 30, 9, 22, 0.28, 240_1)
keep  = (dst[y0:y1, x0:x1] != 0)  # preserve ocean (==0); overwrite all else
dst[y0:y1, x0:x1] = np.where(mask1 & keep, ZONE_FRESHWATER_FEN, dst[y0:y1, x0:x1])

# FEN #2
print("Patch 2 (FEN @ tile 8,73)...", flush=True)
cx2_w, cz2_w = 8 * 512 + 256, 73 * 512 + 256
cx2 = cx2_w * PX_PER_BLOCK; cz2 = cz2_w * PX_PER_BLOCK
BOX2 = 140
y0, y1 = max(0, int(cz2) - BOX2), min(H8K, int(cz2) + BOX2)
x0, x1 = max(0, int(cx2) - BOX2), min(W8K, int(cx2) + BOX2)
mask2 = noisy_blob_mask((y1-y0, x1-x0), cx2-x0, cz2-y0, 38, 12, 26, 0.25, 240_2)
keep  = (dst[y0:y1, x0:x1] != 0)
dst[y0:y1, x0:x1] = np.where(mask2 & keep, ZONE_FRESHWATER_FEN, dst[y0:y1, x0:x1])

# RIPARIAN — same 8k pipeline as paint_s62_patches.py
print("Patch 3 (RIPARIAN corridor)...", flush=True)
z_lo, z_hi = 48 * 512, 75 * 512
x_lo, x_hi = 71 * 512, 92 * 512
y0_8k = int(round(z_lo * PX_PER_BLOCK))
y1_8k = int(round(z_hi * PX_PER_BLOCK))
x0_8k = int(round(x_lo * PX_PER_BLOCK))
x1_8k = int(round(x_hi * PX_PER_BLOCK))
roi_h, roi_w = y1_8k - y0_8k, x1_8k - x0_8k

with rasterio.open(HYDRO_ORD) as hsrc:
    roi_order_50k = hsrc.read(1, window=((z_lo, z_hi), (x_lo, x_hi)))
with rasterio.open(OVERRIDE) as osrc:
    roi_ov_50k = osrc.read(1, window=((z_lo, z_hi), (x_lo, x_hi)))

big_river_in_dry_50k = (roi_order_50k >= 3) & np.isin(roi_ov_50k, list(DRY_ZONES))
fy = roi_h / big_river_in_dry_50k.shape[0]
fx = roi_w / big_river_in_dry_50k.shape[1]
big_river_8k = _zoom(big_river_in_dry_50k.astype(np.float32), (fy, fx), order=1) > 0.15
ov_8k = _zoom(roi_ov_50k, (fy, fx), order=0)
is_dry_8k = np.isin(ov_8k, list(DRY_ZONES))

BUFFER_BLOCKS = 80
buf_px = max(1, int(round(BUFFER_BLOCKS * PX_PER_BLOCK)))
dist_8k = distance_transform_edt(~big_river_8k)
sim_r = OpenSimplex(seed=80_1)
h_r, w_r = dist_8k.shape
gy = np.arange(0, h_r, 4); gx = np.arange(0, w_r, 4)
coarse_r = np.empty((len(gy), len(gx)), dtype=np.float32)
for i, y in enumerate(gy):
    for j, x in enumerate(gx):
        coarse_r[i, j] = sim_r.noise2(x / 60.0, y / 60.0) * 5
meander_8k = _zoom(coarse_r, (h_r / coarse_r.shape[0], w_r / coarse_r.shape[1]), order=1)[:h_r, :w_r]
corridor_warped = (dist_8k + meander_8k) < buf_px
band_lo = buf_px * 0.7; band_hi = buf_px * 1.0
in_band = (dist_8k + meander_8k >= band_lo) & (dist_8k + meander_8k < band_hi)
edge_frac = np.clip(((dist_8k + meander_8k) - band_lo) / (band_hi - band_lo), 0, 1)
rng = np.random.default_rng(80_2)
edge_keep = rng.random(dist_8k.shape) > edge_frac
corridor_final = (corridor_warped | (in_band & edge_keep)) & is_dry_8k

window = dst[y0_8k:y0_8k + corridor_final.shape[0], x0_8k:x0_8k + corridor_final.shape[1]]
keep = (window != 0)
window[:] = np.where(corridor_final & keep, ZONE_RIPARIAN_WOODLAND, window)

# Save
print(f"Saving {VEC_PNG}...")
Image.fromarray(dst).save(VEC_PNG)
unique, counts = np.unique(dst, return_counts=True)
for u, c in zip(unique, counts):
    if u in (80, 240) or u == 0:
        print(f"  zone {u:3d}: {c:>10d} px")
