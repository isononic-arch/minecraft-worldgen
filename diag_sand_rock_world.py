"""
diag_sand_rock_world.py — Full-world top-down preview of sand_dunes,
rock_exposure_tight, and snow_caps masks. Style of diag_floodplain_world.

Reads masks at 1:8 (6250x6250), composites overlays on hillshade. ~10s.
"""
from __future__ import annotations
import time, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
import rasterio
from rasterio.enums import Resampling

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
OUTPUT    = Path(r"C:\Users\nicho\minecraft-worldgen\output\sand_rock_world.png")
DS = 6250
SEA_RAW = 17050

SAND_RGB = np.array([220, 200, 140])  # tan dune
ROCK_RGB = np.array([155,  95,  60])  # terracotta
SNOW_RGB = np.array([245, 250, 255])  # snow white
WATER_RGB = np.array([50, 110, 190])
OCEAN_RGB = np.array([30, 50, 80])


def read_ds(name, resamp=Resampling.average):
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
    sand = read_ds("sand_dunes", Resampling.average).astype(np.float32) / 255.0
    rock = read_ds("rock_exposure_tight", Resampling.average).astype(np.float32) / 255.0
    snow = read_ds("snow_caps", Resampling.average).astype(np.float32) / 255.0

    print(f"  Read in {time.perf_counter()-t0:.1f}s", flush=True)

    land = height > SEA_RAW
    n_land = int(land.sum())

    # Hillshade base
    shade = hillshade(height)
    grey = (shade * 200 + 30).astype(np.uint8)

    # Land base: warm desert tint (sandy gray)
    rgb = np.zeros((DS, DS, 3), dtype=np.uint8)
    rgb[~land] = OCEAN_RGB
    rgb[land, 0] = (grey[land] * 0.95).astype(np.uint8)
    rgb[land, 1] = (grey[land] * 0.85).astype(np.uint8)
    rgb[land, 2] = (grey[land] * 0.65).astype(np.uint8)

    # Overlay sand_dunes (tan)
    sand_a = np.clip(sand * 0.85, 0.0, 0.85)  # alpha based on gradient strength
    sand_a[~land] = 0
    for i in range(3):
        rgb[:,:,i] = (rgb[:,:,i] * (1 - sand_a) + SAND_RGB[i] * sand_a).astype(np.uint8)
    n_sand = int((sand > 0.20).sum() & land.sum())
    n_sand = int(((sand > 0.20) & land).sum())

    # Overlay rock_exposure_tight (terracotta)
    rock_a = np.clip(rock * 0.85, 0.0, 0.85)
    rock_a[~land] = 0
    for i in range(3):
        rgb[:,:,i] = (rgb[:,:,i] * (1 - rock_a) + ROCK_RGB[i] * rock_a).astype(np.uint8)
    n_rock = int(((rock > 0.20) & land).sum())

    # Overlay snow_caps (white) — top layer
    snow_a = np.clip(snow * 0.95, 0.0, 0.95)
    snow_a[~land] = 0
    for i in range(3):
        rgb[:,:,i] = (rgb[:,:,i] * (1 - snow_a) + SNOW_RGB[i] * snow_a).astype(np.uint8)
    n_snow = int(((snow > 0.40) & land).sum())

    img = Image.fromarray(rgb, "RGB")
    draw = ImageDraw.Draw(img)

    # Stats overlay
    pct_sand = 100 * n_sand / max(n_land, 1)
    pct_rock = 100 * n_rock / max(n_land, 1)
    pct_snow = 100 * n_snow / max(n_land, 1)
    draw.rectangle([(5, 5), (350, 95)], fill=(0, 0, 0, 180))
    draw.text((10, 10), "Vandir Sand / Rock / Snow precompute masks",
              fill=(255, 255, 200))
    draw.text((10, 28), f"Sand dunes (>=0.20): {pct_sand:5.1f}% of land",
              fill=tuple(SAND_RGB.tolist()))
    draw.text((10, 46), f"Rock peaks (>=0.20): {pct_rock:5.1f}% of land",
              fill=tuple(ROCK_RGB.tolist()))
    draw.text((10, 64), f"Snow caps  (>=0.40): {pct_snow:5.1f}% of land",
              fill=(255, 255, 255))
    draw.text((10, 80), f"Hillshade base, 1:8 = {DS}x{DS}",
              fill=(180, 180, 180))

    OUTPUT.parent.mkdir(exist_ok=True)
    img.save(str(OUTPUT), quality=92)

    elapsed = time.perf_counter() - t0
    print(f"\nSand:  {n_sand:>10} px ({pct_sand:.1f}% of land)")
    print(f"Rock:  {n_rock:>10} px ({pct_rock:.1f}% of land)")
    print(f"Snow:  {n_snow:>10} px ({pct_snow:.1f}% of land)")
    print(f"\nSaved: {OUTPUT} ({DS}x{DS}, {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
