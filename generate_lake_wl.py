"""
generate_lake_wl.py — Build hydro_lake_wl.tif from existing hydro_lake.tif + height.tif
======================================================================================
Reads the existing 50k masks, computes per-lake spill elevation at 1:8 working
resolution, then writes a 50k float32 TIF via NEAREST upscale.

This avoids re-running the full hydrology_precompute pipeline.

Usage:
    python generate_lake_wl.py [--masks masks]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

SCALE    = 8
FULL     = 50_000
DS       = FULL // SCALE  # 6250


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--masks", default="masks", help="Masks directory")
    args = parser.parse_args()

    masks_dir = Path(args.masks)
    t0 = time.perf_counter()

    import rasterio
    from rasterio.windows import Window
    from scipy.ndimage import binary_dilation

    # ── Read at 1:8 working resolution ────────────────────────────────────
    # hydro_lake.tif is uint16 at 50k — read every 8th pixel
    lake_path   = masks_dir / "hydro_lake.tif"
    height_path = masks_dir / "height.tif"

    if not lake_path.exists() or not height_path.exists():
        print(f"ERROR: need {lake_path} and {height_path}")
        sys.exit(1)

    print("Reading hydro_lake.tif at 1:8 ...")
    with rasterio.open(str(lake_path)) as src:
        full_h, full_w = src.height, src.width
        # Downsample by reading with a decimation stride
        lake_full_row0 = src.read(1, out_shape=(full_h // SCALE, full_w // SCALE))
    lake_ds = lake_full_row0.astype(np.uint16)
    print(f"  shape: {lake_ds.shape}, lakes: {int(lake_ds.max())}")

    print("Reading height.tif at 1:8 ...")
    with rasterio.open(str(height_path)) as src:
        h_ds = src.read(1, out_shape=(full_h // SCALE, full_w // SCALE))
    h_norm = h_ds.astype(np.float32) / 65535.0
    print(f"  shape: {h_norm.shape}")

    # ── Compute spill elevation per lake ──────────────────────────────────
    n_lakes = int(lake_ds.max())
    print(f"Computing spill elevations for {n_lakes} lakes ...")

    lake_wl = np.zeros(h_norm.shape, dtype=np.float32)

    for lid in range(1, n_lakes + 1):
        rows, cols = np.where(lake_ds == lid)
        if len(rows) == 0:
            continue

        r0 = max(int(rows.min()) - 2, 0)
        r1 = min(int(rows.max()) + 3, h_norm.shape[0])
        c0 = max(int(cols.min()) - 2, 0)
        c1 = min(int(cols.max()) + 3, h_norm.shape[1])

        lid_crop = lake_ds[r0:r1, c0:c1] == lid
        h_crop   = h_norm[r0:r1, c0:c1]

        perim = binary_dilation(lid_crop) & ~lid_crop
        if not perim.any():
            continue

        spill_elev = float(h_crop[perim].min())
        lake_wl[r0:r1, c0:c1][lid_crop] = spill_elev

    wl_px = (lake_wl > 0).sum()
    print(f"  Water-level pixels: {wl_px}")
    if wl_px > 0:
        print(f"  WL range: {lake_wl[lake_wl > 0].min():.5f} - {lake_wl[lake_wl > 0].max():.5f}")

    # ── Write 50k via chunked NEAREST upscale ─────────────────────────────
    out_path = masks_dir / "hydro_lake_wl.tif"
    print(f"Writing {out_path} ({full_h}x{full_w}, float32, NEAREST) ...")

    out_h = min(lake_wl.shape[0] * SCALE, full_h)
    out_w = min(lake_wl.shape[1] * SCALE, full_w)

    profile = {
        "driver": "GTiff",
        "width": out_w,
        "height": out_h,
        "count": 1,
        "dtype": "float32",
        "compress": "lzw",
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
    }

    chunk_rows = 50
    with rasterio.open(str(out_path), "w", **profile) as dst:
        for src_row in range(0, lake_wl.shape[0], chunk_rows):
            src_end = min(src_row + chunk_rows, lake_wl.shape[0])
            chunk = lake_wl[src_row:src_end]
            up = np.repeat(np.repeat(chunk, SCALE, axis=0), SCALE, axis=1)
            up = up[:, :out_w]
            win = Window(0, src_row * SCALE, up.shape[1], up.shape[0])
            dst.write(up, 1, window=win)

    elapsed = time.perf_counter() - t0
    print(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
