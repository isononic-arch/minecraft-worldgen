"""
diag_coord_sanity.py - Check whether the precompute view and MCA topdown
agree on coordinates by sampling heights from both and comparing.
"""
from __future__ import annotations

import sys
import struct, zlib, gzip, io
from pathlib import Path

import numpy as np
import nbtlib
import rasterio
from rasterio.windows import Window

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diag_mca_topdown import read_chunk_nbt, unpack_section


def sample_mca_heights(mca_path: str, n_samples: int = 16) -> dict:
    """Sample top-block Y at fixed grid points."""
    samples = {}
    for cz in range(0, 32, 8):
        for cx in range(0, 32, 8):
            chunk = read_chunk_nbt(mca_path, cx, cz)
            if chunk is None:
                continue
            sections = chunk.get("sections") or chunk.root.get("sections")
            secarr = {}
            for sec in sections or []:
                sy = int(sec.get("Y", 0))
                arr = unpack_section(sec)
                if arr is not None:
                    secarr[sy] = arr
            # Sample at chunk-local (8, 8)
            lx, lz = 8, 8
            top_y = -64
            for sy in sorted(secarr.keys(), reverse=True):
                arr = secarr[sy]
                for ly in range(15, -1, -1):
                    block = arr[ly][lz][lx]
                    if block is None or "air" in block:
                        continue
                    top_y = sy * 16 + ly
                    break
                if top_y > -64:
                    break
            world_x = cx * 16 + lx
            world_z = cz * 16 + lz
            samples[(world_x, world_z)] = top_y
    return samples


def main():
    tile_x, tile_z = 51, 53
    masks_dir = Path(r"C:\Users\nicho\minecraft-worldgen\masks")

    # 1. Read height from precompute mask at the tile window
    x0 = tile_x * 512
    z0 = tile_z * 512
    with rasterio.open(str(masks_dir / "height.tif")) as src:
        height_window = src.read(1, window=Window(x0, z0, 512, 512))

    # Convert raw -> MC Y
    gaea_in = np.array([0, 17050, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    height_mc_window = np.interp(height_window.ravel(), gaea_in, mc_y_out).reshape(512, 512)

    # 2. Sample heights from MCA at corresponding world coords
    mca_path = str(Path(__file__).resolve().parent.parent / "output" / f"r.{tile_x}.{tile_z}.mca")
    mca_samples = sample_mca_heights(mca_path)

    print(f"Tile ({tile_x}, {tile_z}) world origin: ({x0}, {z0})")
    print(f"Precompute window shape: {height_mc_window.shape}")
    print()
    print(f"{'world_x':>8} {'world_z':>8} {'precomp_y':>10} {'mca_y':>8} {'diff':>6}")
    for (wx, wz), mca_y in sorted(mca_samples.items()):
        # Map world coords back to window-local indices
        local_x = wx  # precompute window's row=Z, col=X
        local_z = wz
        if 0 <= local_x < 512 and 0 <= local_z < 512:
            precomp_y = float(height_mc_window[local_z, local_x])
        else:
            precomp_y = float("nan")
        diff = abs(mca_y - precomp_y) if not np.isnan(precomp_y) else float("nan")
        print(f"{wx:>8} {wz:>8} {precomp_y:>10.1f} {mca_y:>8} {diff:>6.1f}")


if __name__ == "__main__":
    main()
