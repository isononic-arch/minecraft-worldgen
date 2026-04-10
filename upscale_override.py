#!/usr/bin/env python3
"""
upscale_override.py — Upscale override_final.png to 50k x 50k Tiled BigTIFF
Writes in row chunks to avoid loading the full 50k array into RAM.
Usage: py upscale_override.py
"""

import numpy as np
from PIL import Image
import rasterio
from rasterio.windows import Window
import os

INPUT     = r"C:\Users\nicho\minecraft-worldgen\override_final.png"
OUTPUT    = r"C:\Users\nicho\minecraft-worldgen\masks\override.tif"
TARGET    = 50000
CHUNK_PX  = 512   # rows per chunk — keeps RAM under ~100MB per pass

os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

print(f"Loading {INPUT}...")
img = Image.open(INPUT).convert("L")
orig_w, orig_h = img.size
print(f"Original size: {orig_w}x{orig_h}")
print(f"Target size:   {TARGET}x{TARGET}")

scale = TARGET / orig_w  # assumes square

print(f"Writing {OUTPUT} in {CHUNK_PX}px chunks...")
with rasterio.open(
    OUTPUT,
    "w",
    driver="GTiff",
    height=TARGET,
    width=TARGET,
    count=1,
    dtype=np.uint8,
    compress="deflate",
    tiled=True,
    blockxsize=512,
    blockysize=512,
    bigtiff="YES",
) as dst:
    row = 0
    while row < TARGET:
        chunk_h = min(CHUNK_PX, TARGET - row)

        src_row_start = int(row / scale)
        src_row_end   = min(orig_h, int((row + chunk_h) / scale) + 1)

        src_crop  = img.crop((0, src_row_start, orig_w, src_row_end))
        dst_chunk = src_crop.resize((TARGET, chunk_h), Image.NEAREST)
        data      = np.array(dst_chunk, dtype=np.uint8)

        window = Window(col_off=0, row_off=row, width=TARGET, height=chunk_h)
        dst.write(data[np.newaxis, :, :], window=window)

        row += chunk_h
        print(f"  {row / TARGET * 100:.0f}%  (row {row}/{TARGET})", end="\r")

print(f"\nDone. Output: {OUTPUT}")
size_mb = os.path.getsize(OUTPUT) / 1024 / 1024
print(f"File size: {size_mb:.1f} MB")
