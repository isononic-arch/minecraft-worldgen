"""
diag_painted_rivers.py — Render the user-painted hydro_region.png in
isolation against a faint terrain hillshade so the painted river network
is verifiable at a glance.

Usage: py tools/diag_painted_rivers.py --out memory/painted_rivers.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
import matplotlib.pyplot as plt
from PIL import Image

SEA_LEVEL_RAW = 17050


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    p.add_argument("--display-size", type=int, default=4096)
    args = p.parse_args()

    masks_dir = Path(args.masks)
    DS = args.display_size

    # Painted rivers
    print(f"Reading hydro_region.png ...", file=sys.stderr)
    paint = np.array(
        Image.open(masks_dir / "hydro_region.png").convert("L").resize(
            (DS, DS), Image.NEAREST),
        dtype=np.uint8,
    )
    print(f"  paint shape: {paint.shape}, "
          f"river cells (id=2): {(paint==2).sum():,}", file=sys.stderr)

    # Terrain hillshade
    print("Reading height.tif (downsampled)...", file=sys.stderr)
    with rasterio.open(masks_dir / "height.tif") as src:
        h_raw = src.read(1, out_shape=(DS, DS), resampling=Resampling.bilinear)

    # Convert to MC Y for shading
    gaea_in = np.array([0, SEA_LEVEL_RAW, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    height = np.interp(h_raw.ravel(), gaea_in, mc_y_out
                        ).reshape(h_raw.shape).astype(np.float32)
    norm = np.clip((height + 64) / (448 + 64), 0, 1)

    # Faint terrain backdrop
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1)
    base = base * 0.45  # dim the backdrop so paint pops

    # Ocean band
    ocean = h_raw <= SEA_LEVEL_RAW
    base[ocean] = [0.05, 0.10, 0.20]

    # Painted rivers — vivid yellow
    river_mask = paint == 2
    base[river_mask] = [1.0, 0.85, 0.15]

    rgb = (base * 255).astype(np.uint8)
    Image.fromarray(rgb).save(args.out, optimize=True)
    print(f"Saved {args.out}", file=sys.stderr)

    # Also write a smaller preview
    out_2k = args.out.replace(".png", "_2k.png")
    img2k = Image.fromarray(rgb)
    img2k.thumbnail((2000, 2000), Image.LANCZOS)
    img2k.save(out_2k, optimize=True)
    print(f"Saved {out_2k}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
