import rasterio, numpy as np
from rasterio.windows import Window

with rasterio.open(r'C:\Users\nicho\minecraft-worldgen\masks\height.tif') as src:
    data = src.read(1, window=Window(14000, 1000, 8000, 8000))

ocean = data[data > 17050]
shallow = ocean[ocean < 22000]
print(f"Shallow ocean (<22000) = {100*len(shallow)/len(ocean):.1f}% of all ocean pixels")
for v in [17100, 18000, 19000, 20000, 21000, 22000]:
    pct = 100*(ocean < v).mean()
    print(f"  raw<{v}: {pct:.1f}% of ocean")
