"""Quick multi-panel preview of a baked island mask folder."""
import sys
import numpy as np, rasterio
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
from pathlib import Path

d = Path(sys.argv[1])
_GIN = np.array([0,5000,12000,17050,18000,21000,26000,30000,35000,42000,50000,58000,65496], float)
_YOUT = np.array([-64,-45,25,63,67,78,110,145,180,360,490,610,700], float)
def rd(n):
    with rasterio.open(str(d/f"{n}.tif")) as s: return s.read(1)
def ds(a, n=700):
    st = max(1, max(a.shape)//n); return a[::st, ::st]

h = ds(rd("height")).astype(float); mcy = np.interp(h, _GIN, _YOUT); land = mcy > 63.4
ov = ds(rd("override")); rk = ds(rd("rock_gap")); sn = ds(rd("snow_gap")); be = ds(rd("beach"))
# hillshade
gy, gx = np.gradient(mcy); sl = np.arctan(np.hypot(gx, gy)); asp = np.arctan2(-gx, gy)
hs = np.clip(np.sin(np.radians(45))*np.cos(sl)+np.cos(np.radians(45))*np.sin(sl)*np.cos(np.radians(315)-asp),0,1)

# zone -> color
ZC = {0:(20,30,50),10:(170,180,120),20:(40,110,60),30:(60,120,90),35:(120,160,150),40:(150,190,160),
      50:(200,210,210),55:(235,240,245),70:(70,150,90),90:(180,170,90),100:(190,190,170),120:(80,140,70),
      150:(170,140,90),160:(40,160,80),170:(220,200,130),200:(190,170,110),210:(150,150,90),220:(60,170,110),230:(40,120,90)}
ovc = np.zeros((*ov.shape,3),np.uint8)
for z,c in ZC.items(): ovc[ov==z]=c

fig, ax = plt.subplots(2,3, figsize=(16,11))
ax[0,0].imshow(np.where(land, hs, np.nan), cmap='terrain'); ax[0,0].set_title('height (hillshade, land)')
ax[0,1].imshow(ovc); ax[0,1].set_title('override (biome zones)')
ax[0,2].imshow(land, cmap='Blues_r'); ax[0,2].set_title(f'land ({land.mean()*100:.1f}%)')
ax[1,0].imshow(rk, cmap='Reds'); ax[1,0].set_title(f'rock_gap ({rk.mean()*100:.1f}%)')
ax[1,1].imshow(sn, cmap='gray'); ax[1,1].set_title(f'snow_gap ({sn.mean()*100:.2f}%)')
ax[1,2].imshow(be, cmap='YlOrBr'); ax[1,2].set_title('beach')
for a in ax.ravel(): a.axis('off')
fig.suptitle(f"{d.name}  (zones present: {sorted(set(np.unique(ov))-{0})})", fontsize=12)
fig.tight_layout(); fig.savefig(d/"preview.png", dpi=85)
print("saved", d/"preview.png", " zones:", sorted(set(np.unique(rd('override')))-{0}))
