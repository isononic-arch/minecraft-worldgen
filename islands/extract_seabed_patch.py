"""extract_seabed_patch.py — pull a representative DEEP-OCEAN relief patch from
Vandir's height.tif so island seabeds can be transplanted to match Vandir's
ocean-floor character (not a flat plate). Cached to islands/cache/.

Finds the deepest fully-ocean window, reads it at native resolution, saves the
raw patch + a preview.
"""
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window
from scipy.ndimage import uniform_filter
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT/"islands"/"cache"; CACHE.mkdir(parents=True, exist_ok=True)
PATCH_NATIVE = 1024
SCAN = 1500
_GIN = np.array([0,5000,12000,17050,18000,21000,26000,30000,35000,42000,50000,58000,65496], float)
_YOUT = np.array([-64,-45,25,63,67,78,110,145,180,360,490,610,700], float)
SEA_RAW = 17050

with rasterio.open(str(ROOT/"masks"/"height.tif")) as src:
    full_w = src.width; full_h = src.height
    scan = src.read(1, out_shape=(SCAN, SCAN), resampling=Resampling.average).astype(float)

mcy = np.interp(scan, _GIN, _YOUT)
ocean = (scan < SEA_RAW).astype(float)
win_scan = max(8, int(PATCH_NATIVE * SCAN / full_w))     # scan-px window matching native patch
frac_ocean = uniform_filter(ocean, win_scan, mode="nearest")
mean_depth = uniform_filter(mcy, win_scan, mode="nearest")
# relief = local std of MC-Y; want a TEXTURED, fully-submerged window (seamounts/slope)
m1 = uniform_filter(mcy, win_scan, mode="nearest")
m2 = uniform_filter(mcy * mcy, win_scan, mode="nearest")
relief = np.sqrt(np.clip(m2 - m1 * m1, 0, None))
cand = frac_ocean > 0.999                                 # stay fully below sea
relief_masked = np.where(cand, relief, -1)
iy, ix = np.unravel_index(np.argmax(relief_masked), relief.shape)
print(f"max-relief fully-ocean window centre (scan px)=({ix},{iy}) "
      f"mean MC-Y={mean_depth[iy,ix]:.1f} relief(std)={relief[iy,ix]:.1f}")

# map scan-centre -> native window top-left
cx_n = int(ix * full_w / SCAN) - PATCH_NATIVE // 2
cy_n = int(iy * full_h / SCAN) - PATCH_NATIVE // 2
cx_n = int(np.clip(cx_n, 0, full_w - PATCH_NATIVE))
cy_n = int(np.clip(cy_n, 0, full_h - PATCH_NATIVE))
with rasterio.open(str(ROOT/"masks"/"height.tif")) as src:
    patch_raw = src.read(1, window=Window(cx_n, cy_n, PATCH_NATIVE, PATCH_NATIVE)).astype(np.uint16)

patch_mcy = np.interp(patch_raw.astype(float), _GIN, _YOUT)
print(f"patch {PATCH_NATIVE}x{PATCH_NATIVE} at native ({cx_n},{cy_n})  "
      f"MC-Y min/med/max = {patch_mcy.min():.0f}/{np.median(patch_mcy):.0f}/{patch_mcy.max():.0f}  "
      f"relief(p5..p95)={np.percentile(patch_mcy,5):.0f}..{np.percentile(patch_mcy,95):.0f}")

np.save(CACHE/"vandir_seabed_patch.npy", patch_raw)
# preview
g = ((patch_mcy - patch_mcy.min())/(np.ptp(patch_mcy)+1e-9)*255).astype(np.uint8)
Image.fromarray(g[::2, ::2], "L").save(CACHE/"vandir_seabed_patch_preview.png")
print(f"saved {CACHE/'vandir_seabed_patch.npy'}  (+ preview)")
