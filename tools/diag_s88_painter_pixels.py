"""
diag_s88_painter_pixels.py — count pixels each S88 painter SHOULD fire on
at the test tiles, to verify they're not silently no-op'ing.
"""
from __future__ import annotations
import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np
import rasterio
import rasterio.windows

TILE = 512


def main():
    for tx, tz, name in [(36, 15, "limestone karst"),
                          (24, 80, "arid basaltic"),
                          (89, 52, "cliff-litho"),
                          (60, 69, "painted-river")]:
        print(f"\n=== tile ({tx},{tz}) {name} ===")
        win = rasterio.windows.Window(tx * TILE, tz * TILE, TILE, TILE)
        for mask_name in ["cliff_cap", "talus_apron", "bedrock_drainage"]:
            with rasterio.open(f"masks/{mask_name}.tif") as src:
                slab = src.read(1, window=win)
            nz = int((slab > 0).sum())
            strong = int((slab > 64).sum())  # intensity_threshold
            print(f"  {mask_name:<20} nonzero={nz:>6d}  >64(threshold)={strong:>6d}")


if __name__ == "__main__":
    main()
