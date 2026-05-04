"""
diag_carve_preview_3x3.py — 3x3 stitched carve-footprint preview around
a center tile. Calls apply_hydro_region_overlay per tile (so v34h
post-process applies) and renders carve+centerline+lakes+ocean.

Usage:
    py tools/diag_carve_preview_3x3.py --tile-x 51 --tile-z 53 \
        --out memory/v34h_3x3.png
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
from scipy.ndimage import distance_transform_edt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.hydro_region_overlay import apply_hydro_region_overlay

TILE = 512
SEA = 17050


def render_tile(masks_dir: Path, tx: int, tz: int) -> np.ndarray:
    col_off, row_off = tx * TILE, tz * TILE
    win = Window(col_off, row_off, TILE, TILE)
    def _read(name):
        with rasterio.open(masks_dir / f"{name}.tif") as src:
            return src.read(1, window=win)

    h_raw = _read("height")
    sl = _read("slope").astype(np.float32)
    if sl.max() > 1.5:
        sl /= 65535.0
    h_norm = h_raw.astype(np.float32) / 65535.0
    lk = _read("hydro_lake")
    wl = _read("hydro_lake_wl").astype(np.float32)
    if wl.max() <= 1.5:
        wl *= 65535.0

    masks = {
        "hydro_centerline": np.zeros((TILE, TILE), dtype=np.float32),
        "hydro_order":      np.zeros((TILE, TILE), dtype=np.float32),
        "hydro_width":      np.zeros((TILE, TILE), dtype=np.float32),
        "hydro_depth":      np.zeros((TILE, TILE), dtype=np.float32),
        "hydro_lake":       np.zeros((TILE, TILE), dtype=np.float32),
        "hydro_lkdep":      np.zeros((TILE, TILE), dtype=np.float32),
        "height":           h_norm,
        "slope":            sl,
    }
    apply_hydro_region_overlay(masks, masks_dir, col_off, row_off, TILE)

    cl = masks["hydro_centerline"] > 0
    w = masks["hydro_width"]
    if cl.any():
        dist, idx = distance_transform_edt(~cl, return_indices=True)
        carve = dist <= w[idx[0], idx[1]]
    else:
        carve = np.zeros((TILE, TILE), dtype=bool)

    gaea_in = np.array([0, SEA, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    hb = np.interp(h_raw.ravel(), gaea_in, mc_y_out
                    ).reshape(h_raw.shape).astype(np.float32)
    norm = np.clip((hb + 64)/(448+64), 0, 1)
    base = plt.get_cmap("terrain")(norm)[..., :3].astype(np.float32)
    gy, gx = np.gradient(hb)
    light = np.clip(0.5 + 0.5 * (-gx-gy)/30.0, 0.4, 1.2)
    base = np.clip(base * light[..., None], 0, 1) * 0.55

    ocean = h_raw <= SEA
    base[ocean] = [0.05, 0.10, 0.20]
    lake_wl_mc = np.interp(wl.ravel(), gaea_in, mc_y_out
                            ).reshape(wl.shape).astype(np.float32)
    underwater = (lk > 0) & (hb < lake_wl_mc) & ~ocean
    base[underwater] = [0.20, 0.55, 0.75]

    base[carve] = [0.95, 0.60, 0.20]
    base[cl]    = [1.0, 0.95, 0.10]

    return (base * 255).astype(np.uint8)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tile-x", type=int, required=True)
    p.add_argument("--tile-z", type=int, required=True)
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    masks_dir = Path(args.masks)
    cx, cz = args.tile_x, args.tile_z

    big = np.zeros((TILE * 3, TILE * 3, 3), dtype=np.uint8)
    for dz in (-1, 0, 1):
        for dx in (-1, 0, 1):
            tx, tz = cx + dx, cz + dz
            print(f"  rendering tile ({tx},{tz})...", file=sys.stderr)
            tile_rgb = render_tile(masks_dir, tx, tz)
            r0 = (dz + 1) * TILE
            c0 = (dx + 1) * TILE
            big[r0:r0+TILE, c0:c0+TILE] = tile_rgb

    fig, ax = plt.subplots(1, 1, figsize=(12, 12), facecolor="white")
    ax.imshow(big, interpolation="nearest")
    ax.set_xticks([]); ax.set_yticks([])
    for i in (TILE, 2 * TILE):
        ax.axhline(i, color="white", linewidth=0.5, alpha=0.4)
        ax.axvline(i, color="white", linewidth=0.5, alpha=0.4)
    for dz in (-1, 0, 1):
        for dx in (-1, 0, 1):
            tx, tz = cx + dx, cz + dz
            r0 = (dz + 1) * TILE
            c0 = (dx + 1) * TILE
            ax.text(c0 + 8, r0 + 24, f"({tx},{tz})",
                    color="white", fontsize=10,
                    bbox=dict(facecolor="black", alpha=0.65, pad=2,
                              edgecolor="none"))
    ax.set_title(
        f"v34h carve-preview 3x3 around ({cx},{cz}) — "
        f"orange=carve, yellow=centerline, blue=lake, navy=ocean",
        fontsize=11)
    fig.tight_layout()
    fig.savefig(args.out, dpi=140, bbox_inches="tight")
    print(f"Saved {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
