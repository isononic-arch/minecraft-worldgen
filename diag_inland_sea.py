import rasterio, numpy as np
from rasterio.windows import Window

# Adjust these coords to hit the southern inland sea area
cx, cy = 12000, 35000  # rough estimate - adjust as needed
with rasterio.open(r'C:\Users\nicho\minecraft-worldgen\masks\height.tif') as src:
    data = src.read(1, window=Window(cx, cy, 2000, 2000))

print(f"Min raw: {data.min()}")
print(f"Max raw: {data.max()}")
print(f"Pixels < 17050 (land): {(data < 17050).sum()}")
print(f"Pixels >= 17050 (ocean): {(data >= 17050).sum()}")
print(f"p50 raw: {np.percentile(data, 50):.0f}")
