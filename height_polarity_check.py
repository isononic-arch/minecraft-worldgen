#!/usr/bin/env python3
"""
height_polarity_check.py — Vandir ground-truth polarity diagnostic
===================================================================
Samples real pixel values from Erosion2_Out.tif at known geographic
locations to definitively settle the height mask polarity question.

Run with:
    py height_polarity_check.py

Reads: C:\Gaea Stuff\Erosion2_Out.tif  (or pass --tif path)
Also reads the 50k upscaled masks\height.tif if available.
"""

import argparse
import sys
from pathlib import Path
import numpy as np
import rasterio
from rasterio.windows import Window

SEA_THRESHOLD = 17050  # locked from step0_output.json

def sample(src, row_frac, col_frac, label):
    """Sample a single pixel at fractional position and report."""
    h, w = src.height, src.width
    row = int(row_frac * h)
    col = int(col_frac * w)
    win = Window(col, row, 1, 1)
    val = int(src.read(1, window=win).flat[0])
    is_ocean_by_threshold = val > SEA_THRESHOLD
    print(f"  {label:<35} row={row:5d} col={col:5d}  raw={val:5d}  "
          f"{'OCEAN' if is_ocean_by_threshold else 'LAND ':5s} (threshold {SEA_THRESHOLD})")
    return val

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tif", default=r"C:\Gaea Stuff\Erosion2_Out.tif")
    args = parser.parse_args()

    tif = Path(args.tif)
    if not tif.exists():
        print(f"ERROR: {tif} not found")
        sys.exit(1)

    print("=" * 70)
    print("  Height polarity diagnostic")
    print(f"  File: {tif}")
    print("=" * 70)

    with rasterio.open(tif) as src:
        print(f"\n  Image size: {src.width} x {src.height}")
        print(f"  Dtype: {src.dtypes[0]}")
        print(f"  Min/Max: {src.read(1).min()} / {src.read(1).max()}")
        print()

        print("Sampling known geographic locations:")
        print("(These are approximate fractions — adjust if needed)")
        print()

        # Sample corners and centre
        vals = {}
        vals["top-left corner (ocean?)"]      = sample(src, 0.02, 0.02, "top-left corner (ocean?)")
        vals["top-right corner (ocean?)"]     = sample(src, 0.02, 0.98, "top-right corner (ocean?)")
        vals["bottom-left corner (ocean?)"]   = sample(src, 0.98, 0.02, "bottom-left corner (ocean?)")
        vals["bottom-right corner (ocean?)"]  = sample(src, 0.98, 0.98, "bottom-right corner (ocean?)")
        vals["centre of image"]               = sample(src, 0.50, 0.50, "centre of image")

        print()
        print("Sampling a strip across the image to find land/ocean boundary:")
        print()
        row_frac = 0.3  # pick a row that crosses land
        prev_ocean = None
        for col_pct in range(0, 101, 5):
            col_frac = col_pct / 100
            h, w = src.height, src.width
            row = int(row_frac * h)
            col = int(col_frac * w)
            win = Window(col, row, 1, 1)
            val = int(src.read(1, window=win).flat[0])
            is_ocean = val > SEA_THRESHOLD
            marker = " <-- BOUNDARY" if (prev_ocean is not None and is_ocean != prev_ocean) else ""
            print(f"  col {col_pct:3d}%  raw={val:5d}  {'OCEAN' if is_ocean else 'LAND ':5s}{marker}")
            prev_ocean = is_ocean

    print()
    print("=" * 70)
    print("INTERPRETATION:")
    print()
    print("Look at the boundary rows above. Open your Gaea heightmap preview")
    print("or the coastal_reference.png and check:")
    print()
    print("  Q1: At the boundary column, does the image show LAND transitioning")
    print("      to OCEAN, or OCEAN to LAND (reading left to right)?")
    print()
    print("  Q2: Are the corner pixels (which should be ocean/black in the")
    print("      Gaea preview) showing HIGH or LOW raw values?")
    print()
    print("Expected if NOT inverted (high raw = high terrain):")
    print("  - Ocean corners → low raw values (near 0)")
    print("  - Mountain peaks → high raw values (near 65535)")
    print()
    print("Expected if INVERTED (low raw = high terrain — current assumption):")
    print("  - Ocean corners → high raw values (near 65535)")  
    print("  - Mountain peaks → low raw values (near 0)")
    print()
    print("Report back the corner values and which direction the boundary")
    print("transitions and we can lock the polarity definitively.")
    print("=" * 70)

if __name__ == "__main__":
    main()
