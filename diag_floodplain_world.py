"""
diag_floodplain_world.py — Full-world top-down of all gap types at 1:8 scale.
Reads masks directly at 6250×6250 — no per-tile pipeline needed.
"""
from __future__ import annotations
import time, sys
from pathlib import Path
import numpy as np
from PIL import Image
import rasterio
from rasterio.enums import Resampling

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR  = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
OUTPUT     = Path(r"C:\Users\nicho\minecraft-worldgen\output\floodplain_world.png")
DS = 6250
SEA_RAW = 17050

GAP_COLOURS = {
    "floodplain": np.array([180, 220, 60]),
}
WATER_RGB = np.array([50, 110, 190])
LAND_RGB  = np.array([40, 40, 40])  # dark base, hillshade on top

def read_ds(name, resamp=Resampling.nearest):
    path = MASKS_DIR / f"{name}.tif"
    with rasterio.open(str(path)) as src:
        return src.read(1, out_shape=(DS, DS), resampling=resamp)

def hillshade(h):
    gy, gx = np.gradient(h.astype(np.float32))
    az, alt = np.radians(315), np.radians(45)
    hyp = np.hypot(gx, gy)
    shade = (np.cos(alt) * np.cos(np.arctan(hyp)) +
             np.sin(alt) * (gx * np.sin(az) + gy * np.cos(az)) / np.maximum(hyp, 1e-6))
    return np.clip((shade + 1) / 2, 0, 1)

def main():
    t0 = time.perf_counter()
    print("Reading masks at 1:8 ...", flush=True)

    height = read_ds("height", Resampling.average).astype(np.float32)
    floodplain = read_ds("hydro_floodplain", Resampling.nearest).astype(np.uint8)
    centerline = read_ds("hydro_centerline", Resampling.nearest).astype(np.uint8)
    hydro_order = read_ds("hydro_order", Resampling.nearest).astype(np.uint8)
    hydro_lake = read_ds("hydro_lake", Resampling.nearest)

    print(f"  Read in {time.perf_counter()-t0:.1f}s", flush=True)

    land = height > SEA_RAW
    water = (centerline > 0) | (hydro_order > 0) | (hydro_lake > 0)

    # Hillshade base
    shade = hillshade(height)
    # Land: green-tinted hillshade, Ocean: dark blue
    rgb = np.zeros((DS, DS, 3), dtype=np.uint8)
    # Ocean
    rgb[~land] = [30, 50, 80]
    # Land hillshade (green tint)
    land_grey = (shade * 180 + 40).astype(np.uint8)
    rgb[land, 0] = (land_grey[land] * 0.6).astype(np.uint8)
    rgb[land, 1] = (land_grey[land] * 0.85).astype(np.uint8)
    rgb[land, 2] = (land_grey[land] * 0.5).astype(np.uint8)

    # Water overlay
    rgb[water & land] = WATER_RGB

    # Floodplain overlay
    fp = (floodplain > 0) & land & ~water
    n_fp = fp.sum()
    n_land = land.sum()
    print(f"  Floodplain: {n_fp} px ({n_fp*100/max(n_land,1):.1f}% of land)")
    rgb[fp] = (rgb[fp] * 0.4 + GAP_COLOURS["floodplain"] * 0.6).astype(np.uint8)

    # Save full res (6250×6250) and a downsampled version
    img = Image.fromarray(rgb, "RGB")

    # Add some basic info text
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    draw.text((10, 10), f"Vandir Floodplain — {n_fp*100/max(n_land,1):.1f}% of land", fill=(255,255,255))
    draw.text((10, 26), f"Preset B_wide, global precompute, {DS}x{DS}", fill=(200,200,200))

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT), quality=95)

    elapsed = time.perf_counter() - t0
    print(f"\nSaved: {OUTPUT} ({DS}x{DS}, {elapsed:.1f}s)")

if __name__ == "__main__":
    main()
