"""preview_masks.py — render the derived island masks to small PNGs for review."""
import sys
from pathlib import Path
import numpy as np
import rasterio
from PIL import Image

d = Path(sys.argv[1])
out = d / "preview"; out.mkdir(exist_ok=True)


def rd(name):
    with rasterio.open(str(d / f"{name}.tif")) as s:
        return s.read(1)


def save(arr, name, cmap=None):
    a = arr.astype(np.float64)
    a = (a - a.min()) / (np.ptp(a) + 1e-9)
    ds = a[::max(1, a.shape[0] // 512), ::max(1, a.shape[1] // 512)]
    g = (ds * 255).astype(np.uint8)
    Image.fromarray(g, "L").save(out / f"{name}.png")


# height -> hillshade (convert raw to MC-Y first via spline so relief is real)
_GIN = np.array([0,5000,12000,17050,18000,21000,26000,30000,35000,42000,50000,58000,65496], float)
_YOUT = np.array([-64,-45,25,63,67,78,110,145,180,360,490,610,700], float)
h = rd("height").astype(np.float64)
mcy = np.interp(h, _GIN, _YOUT)
ds = mcy[::max(1, mcy.shape[0]//512), ::max(1, mcy.shape[1]//512)]
gy, gx = np.gradient(ds)
slp = np.arctan(np.hypot(gx, gy)); asp = np.arctan2(-gx, gy)
import numpy as _np
az, alt = _np.radians(315), _np.radians(45)
hs = _np.clip(_np.sin(alt)*_np.cos(slp) + _np.cos(alt)*_np.sin(slp)*_np.cos(az-asp), 0, 1)
Image.fromarray((hs*255).astype(np.uint8), "L").save(out / "height_hillshade.png")
Image.fromarray(((ds > 63)*255).astype(np.uint8), "L").save(out / "land.png")

save(rd("slope"), "slope")
flow = rd("flow").astype(np.float64)
save(np.log1p(flow), "flow_log")
save(rd("erosion"), "erosion")
print(f"previews -> {out}")
print(f"height MC-Y: min={mcy.min():.0f} max={mcy.max():.0f}  "
      f"land>63: {(mcy>63).mean()*100:.2f}%   slope max={rd('slope').max()}  "
      f"flow max={rd('flow').max()}")
