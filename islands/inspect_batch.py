"""Batch-inspect island DEMs: identify (lat/lon/zoom), lore scale, land/clip
stats, and save a hillshade montage for visual ID."""
import sys, re, math
import numpy as np
from pathlib import Path
from PIL import Image
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt

LORE = 9.14

def parse(name):
    m = re.match(r"(-?\d+)_(\d+)_(-?\d+)_(\d+)_(\d+)_(\d+)_(\d+)_", name)
    if not m: return None
    return dict(lat=float(f"{m.group(1)}.{m.group(2)}"), lon=float(f"{m.group(3)}.{m.group(4)}"),
                zoom=int(m.group(5)), w=int(m.group(6)))

def lore_bpp(lat, zoom):
    dpp = (360.0/(2**zoom))/256.0
    return dpp*111.32*math.cos(math.radians(lat))*1000.0/LORE

def hs(z):
    gy,gx=np.gradient(z); sl=np.arctan(np.hypot(gx,gy)); asp=np.arctan2(-gx,gy)
    return np.clip(np.sin(math.radians(45))*np.cos(sl)+np.cos(math.radians(45))*np.sin(sl)*np.cos(math.radians(315)-asp),0,1)

files = sys.argv[1:]
fig, axes = plt.subplots(1, len(files), figsize=(4*len(files), 4))
if len(files) == 1: axes=[axes]
for ax, f in zip(axes, files):
    p = Path(f); meta = parse(p.name)
    im = Image.open(f)
    if im.mode not in ("I","I;16","L","F"): im=im.convert("I")
    z = np.asarray(im).astype(float); H,W = z.shape
    ds = z[::8,::8]
    lo = np.round(z[z<=np.percentile(z,60)]).astype(np.int64); v,c = np.unique(lo,return_counts=True)
    sea = float(v[c.argmax()])
    land = ds > sea + (ds.max()-sea)*0.02
    bpp = lore_bpp(meta["lat"], meta["zoom"]) if meta else 2.0
    clip = int((z>=z.max()-1).sum())
    print(f"\n{p.name}")
    print(f"  lat={meta['lat']:.3f} lon={meta['lon']:.3f} zoom={meta['zoom']}  "
          f"lore={bpp:.2f} blk/px  full-frame={bpp*W:.0f} blk ({bpp*W/512:.1f} tiles)")
    print(f"  land={land.mean()*100:.1f}%  sea_raw={sea:.0f}  clip@max={clip}px  "
          f"raw max={z.max():.0f}")
    shade = hs(ds); rgb=np.dstack([shade]*3); rgb[~land]=[0.10,0.16,0.26]
    ax.imshow(rgb); ax.set_title(f"{meta['lat']:.2f},{meta['lon']:.2f}", fontsize=9); ax.axis("off")
fig.tight_layout(); fig.savefig("islands/island_montage.png", dpi=80)
print("\nsaved islands/island_montage.png")
