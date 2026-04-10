import rasterio
import numpy as np
from rasterio.windows import Window

with rasterio.open(r'C:\Users\nicho\minecraft-worldgen\masks\height.tif') as src:
    data = src.read(1, window=Window(14000, 1000, 4096, 4096))

land = data[data <= 17050]  # inverted: <=17050 = land
print(f"Land pixel count: {len(land)}")
print(f"Land raw min:  {land.min()}")
print(f"Land raw max:  {land.max()}")
for p in [1, 10, 25, 50, 75, 90, 99]:
    print(f"  p{p:2d}: raw={np.percentile(land, p):6.0f}  ->  MC Y ~{int(np.interp(np.percentile(land, p), [0,8000,17050], [448,200,63]))}")
