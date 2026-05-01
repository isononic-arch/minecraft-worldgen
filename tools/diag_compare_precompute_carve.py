"""
diag_compare_precompute_carve.py - Side-by-side comparison of:
  LEFT  — what the precompute thinks the river network looks like
          (height-shaded + hydro_centerline + hydro_width footprint + lakes)
  RIGHT — what the actual carved MCA produced (top-down PNG)

Usage:
    py tools/diag_compare_precompute_carve.py --tile-x 30 --tile-z 49
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window
import matplotlib.pyplot as plt
from PIL import Image
from scipy.ndimage import distance_transform_edt

TILE_SIZE = 512
SEA_LEVEL_RAW = 17050
SAND_DUNE_DESERT_ZONE = 170


def render_precompute_view(
    masks_dir: Path, tx: int, tz: int
) -> np.ndarray:
    """Render a 512x512 view of precompute outputs for tile (tx, tz)."""
    x0 = tx * TILE_SIZE
    z0 = tz * TILE_SIZE
    win = Window(x0, z0, TILE_SIZE, TILE_SIZE)

    def _read(name, dtype=None):
        with rasterio.open(str(masks_dir / f"{name}.tif")) as src:
            arr = src.read(1, window=win)
        if dtype is not None and arr.dtype != dtype:
            arr = arr.astype(dtype)
        return arr

    height = _read("height")  # uint16
    centerline = _read("hydro_centerline")  # uint8 (0/1+)
    width_blocks = _read("hydro_width").astype(np.float32)  # uint8 -> float
    lake = _read("hydro_lake").astype(np.uint16)
    lake_wl_norm = _read("hydro_lake_wl").astype(np.float32)  # normalised [0,1]
    override = _read("override", dtype=np.uint8) if (masks_dir / "override.tif").exists() else None

    # Convert raw height -> MC Y for shading
    gaea_in = np.array([0, SEA_LEVEL_RAW, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    height_blocks = np.interp(height.ravel(), gaea_in, mc_y_out).reshape(height.shape).astype(np.float32)

    # Base: terrain colormap with hillshade
    norm = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    bg = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(height_blocks)
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    bg = np.clip(bg * light[..., None], 0, 1)

    OCEAN = np.array([0.13, 0.27, 0.49])
    LAKE = np.array([0.34, 0.62, 0.78])
    RIVER = np.array([0.20, 0.42, 0.66])
    DUNE = np.array([0.94, 0.83, 0.55])

    ocean = height <= SEA_LEVEL_RAW
    bg[ocean] = OCEAN
    if override is not None:
        bg[override == SAND_DUNE_DESERT_ZONE] = DUNE

    # Compute footprint from centerline + width via EDT (this is what the
    # carver does internally to determine river extent).
    cl_mask = centerline > 0
    if cl_mask.any():
        dist, (iy, ix) = distance_transform_edt(~cl_mask, return_indices=True)
        nearest_w = width_blocks[iy, ix]
        # Carver treats `width` as RADIUS — footprint = dist <= nearest_w
        footprint = dist <= nearest_w
    else:
        footprint = np.zeros_like(cl_mask)

    bg[footprint & ~ocean & (lake == 0)] = RIVER
    # IMPORTANT: lakes render via terrain-intersection (carver's behaviour),
    # not by painting the full lake_id mask.  hydro_lake.tif marks BASIN
    # extents (which can be larger than the actual water surface when
    # terrain rises above the spill elevation in parts of the basin).
    # Compare CLAUDE.md hard rule: "Shoreline = terrain intersection
    # (height < spill_elevation). NEVER morph/blur/spline/gaussian on
    # hydro_lake mask."
    lake_wl_raw = (lake_wl_norm * 65535.0).astype(np.float64)
    lake_wl_mc = np.interp(lake_wl_raw.ravel(), gaea_in, mc_y_out).reshape(lake_wl_raw.shape).astype(np.float32)
    underwater = (lake > 0) & (height_blocks < lake_wl_mc) & ~ocean
    bg[underwater] = LAKE

    rgb = (bg * 255).astype(np.uint8)
    return rgb


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tile-x", type=int, required=True)
    p.add_argument("--tile-z", type=int, required=True)
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--mca", help="Path to carved .mca topdown PNG", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()

    masks_dir = Path(args.masks)

    print(f"Rendering precompute view for tile ({args.tile_x}, {args.tile_z})...",
          file=sys.stderr)
    pre_rgb = render_precompute_view(masks_dir, args.tile_x, args.tile_z)

    mca_img = np.asarray(Image.open(args.mca).convert("RGB"))

    fig, axes = plt.subplots(1, 2, figsize=(14, 7), facecolor="white")
    axes[0].imshow(pre_rgb, interpolation="nearest")
    axes[0].set_title(f"PRECOMPUTE view\n({args.tile_x}, {args.tile_z}) — height + centerline + width + lakes",
                      fontsize=11)
    axes[0].set_xticks([]); axes[0].set_yticks([])

    axes[1].imshow(mca_img, interpolation="nearest")
    axes[1].set_title(f"CARVED MCA top-down\n({args.tile_x}, {args.tile_z}) — actual block surface",
                      fontsize=11)
    axes[1].set_xticks([]); axes[1].set_yticks([])

    fig.suptitle(f"S80 v2 precompute vs carved MCA — tile ({args.tile_x}, {args.tile_z})",
                 fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(args.out, dpi=140, bbox_inches="tight")
    print(f"Saved {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
