import rasterio
import numpy as np
from rasterio.windows import Window

with rasterio.open(r'C:\Users\nicho\minecraft-worldgen\masks\height.tif') as src:
    # Ocean pixels live around x=14000-18000, y=1000-3000
    data = src.read(1, window=Window(14000, 1000, 4096, 4096))

ocean = data[data > 17050]  # inverted polarity: >17050 = underwater
print(f"Ocean pixel count: {len(ocean)}")
print(f"Ocean raw min:  {ocean.min()}")
print(f"Ocean raw max:  {ocean.max()}")
print(f"Ocean p10: {np.percentile(ocean, 10):.0f}")
print(f"Ocean p50: {np.percentile(ocean, 50):.0f}")
print(f"Ocean p90: {np.percentile(ocean, 90):.0f}")
print(f"Ocean p99: {np.percentile(ocean, 99):.0f}")
