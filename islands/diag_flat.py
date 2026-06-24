"""Diagnose flat regions / clipping on an island DEM."""
import sys
import numpy as np
from PIL import Image
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt

p = sys.argv[1]
im = Image.open(p)
if im.mode not in ("I", "I;16", "L", "F"): im = im.convert("I")
z = np.asarray(im).astype(float)
H, W = z.shape
print(f"{W}x{H}  min={z.min():.0f} max={z.max():.0f}")

# clipping signature: spikes at the extremes
vals, counts = np.unique(z, return_counts=True)
order = np.argsort(counts)[::-1]
print("most common raw values (value: count):")
for k in order[:10]:
    print(f"  {vals[k]:.0f}: {counts[k]}  ({counts[k]/z.size*100:.2f}%)")
print(f"pixels AT max ({z.max():.0f}): {int((z==z.max()).sum())}  ({(z==z.max()).mean()*100:.3f}%)")
print(f"pixels AT min ({z.min():.0f}): {int((z==z.min()).sum())}")

sea = 1115
land = z > sea + (z.max()-sea)*0.02
# flat = near-zero gradient on land (downscale a bit to ignore single-pixel noise)
ds = z[::4, ::4]
gy, gx = np.gradient(ds)
grad = np.hypot(gx, gy)
dland = ds > sea + (ds.max()-sea)*0.02
flat = dland & (grad < 1.0)         # <1 raw-unit/4px change = effectively flat
print(f"\nland pixels: {dland.sum()}   FLAT land pixels (grad<1): {flat.sum()}  "
      f"({flat.sum()/max(dland.sum(),1)*100:.1f}% of land)")
# is the flat region at high elevation (summit clip) or low?
if flat.sum():
    fz = ds[flat]
    print(f"flat-land elevation raw: min/med/max = {fz.min():.0f}/{np.median(fz):.0f}/{fz.max():.0f}")
    print(f"  (land elevation overall: med={np.median(ds[dland]):.0f} max={ds[dland].max():.0f})")

# hillshade with flat land marked
def hs(zz):
    gy,gx=np.gradient(zz); sl=np.arctan(np.hypot(gx,gy)); asp=np.arctan2(-gx,gy)
    return np.clip(np.sin(np.radians(45))*np.cos(sl)+np.cos(np.radians(45))*np.sin(sl)*np.cos(np.radians(315)-asp),0,1)
shade = hs(ds)
rgb = np.dstack([shade]*3)
rgb[~dland] = [0.10,0.16,0.26]
rgb[flat] = [1.0,0.2,0.2]            # flat land in red
Image.fromarray((rgb*255).astype(np.uint8),"RGB").save("islands/flat_diag.png")
print("saved islands/flat_diag.png (flat land = red)")
