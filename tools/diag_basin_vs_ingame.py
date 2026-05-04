"""
diag_basin_vs_ingame.py — Side-by-side: carved MCA (in-game lake) vs raw
basin from hydro_lake.tif (precompute basin extent).

The precompute basin can extend dozens of MC blocks beyond visible water
because it represents the watershed cell volume; visible in-game water is
only `terrain < lake_wl` (terrain intersection).

Usage:
    py tools/diag_basin_vs_ingame.py --tile-x 51 --tile-z 53 \
        --mca output/r.51.53.mca --out memory/basin_vs_ingame_51_53.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
import matplotlib.pyplot as plt
from PIL import Image

TILE_SIZE = 512
SEA_LEVEL_RAW = 17050


def render_basin_view(masks_dir: Path, tx: int, tz: int) -> np.ndarray:
    """Render basin extent (raw hydro_lake.tif > 0) + height hillshade
    + terrain-intersection underwater highlight, so the gap between
    raw basin and visible water is obvious."""
    x0 = tx * TILE_SIZE
    z0 = tz * TILE_SIZE
    win = Window(x0, z0, TILE_SIZE, TILE_SIZE)

    def _read(name):
        with rasterio.open(str(masks_dir / f"{name}.tif")) as src:
            return src.read(1, window=win)

    height = _read("height")  # uint16
    lake_id = _read("hydro_lake").astype(np.uint16)
    lake_wl_norm = _read("hydro_lake_wl").astype(np.float32)

    # Terrain in MC Y for shading
    gaea_in = np.array([0, SEA_LEVEL_RAW, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    height_blocks = np.interp(
        height.ravel(), gaea_in, mc_y_out
    ).reshape(height.shape).astype(np.float32)
    lake_wl_mc = np.interp(
        (lake_wl_norm * 65535.0).ravel(), gaea_in, mc_y_out
    ).reshape(lake_wl_norm.shape).astype(np.float32)

    # Base hillshade + terrain colormap
    norm = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    bg = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height_blocks)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    bg = np.clip(bg * light[..., None], 0, 1)

    OCEAN = np.array([0.13, 0.27, 0.49])
    BASIN_DRY = np.array([0.95, 0.55, 0.20])  # ORANGE — basin extent ABOVE water (dry shore)
    UNDERWATER = np.array([0.20, 0.50, 0.80])  # BLUE — actual visible water (terrain<wl)

    # Ocean
    ocean = height <= SEA_LEVEL_RAW
    bg[ocean] = OCEAN

    # Layer 1: BASIN extent in ORANGE (the precompute basin)
    basin_mask = (lake_id > 0) & ~ocean
    bg[basin_mask] = BASIN_DRY

    # Layer 2: VISIBLE water in BLUE (the terrain-intersection — what
    # actually shows up in game)
    underwater = basin_mask & (height_blocks < lake_wl_mc)
    bg[underwater] = UNDERWATER

    return (bg * 255).astype(np.uint8)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tile-x", type=int, required=True)
    p.add_argument("--tile-z", type=int, required=True)
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--mca-png", required=True,
                   help="Pre-rendered top-down PNG of the carved .mca file")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    masks_dir = Path(args.masks)

    print(f"Rendering basin view for ({args.tile_x}, {args.tile_z})...",
          file=sys.stderr)
    basin_rgb = render_basin_view(masks_dir, args.tile_x, args.tile_z)

    mca_img = np.asarray(Image.open(args.mca_png).convert("RGB"))

    fig, axes = plt.subplots(1, 2, figsize=(14, 7), facecolor="white")

    axes[0].imshow(mca_img, interpolation="nearest")
    axes[0].set_title(
        f"ACTUAL IN-GAME ({args.tile_x},{args.tile_z})\n"
        "carved MCA top-down (terrain-intersection water)",
        fontsize=11
    )
    axes[0].set_xticks([]); axes[0].set_yticks([])

    axes[1].imshow(basin_rgb, interpolation="nearest")
    axes[1].set_title(
        f"PRECOMPUTE BASIN ({args.tile_x},{args.tile_z})\n"
        "ORANGE = raw hydro_lake.tif basin (dry shore)\n"
        "BLUE = terrain<wl (visible water)",
        fontsize=11
    )
    axes[1].set_xticks([]); axes[1].set_yticks([])

    fig.suptitle(
        f"In-game lake vs precompute basin — tile ({args.tile_x},{args.tile_z})",
        fontsize=13
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(args.out, dpi=140, bbox_inches="tight")
    print(f"Saved {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
