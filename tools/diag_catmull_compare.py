"""Visual + numerical A/B between default (cubic_spline + blue-noise dither)
and Catmull-Rom (S60) rock_gap and snow_gap masks. Produces:

- memory/catmull_compare.png  (4-panel: rock cubic | rock catmull | snow cubic | snow catmull)
- stdout diff stats: pixel disagreement % at full-res AND in a zoom crop

Usage:
    py tools/diag_catmull_compare.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import rasterio

_WORKTREE = Path(__file__).resolve().parent.parent
MASKS = Path(r"C:/Users/nicho/minecraft-worldgen/masks")
OUT = _WORKTREE / "memory" / "catmull_compare.png"


def _read(path: Path) -> np.ndarray:
    with rasterio.open(path) as src:
        return src.read(1)


def _crop(arr: np.ndarray, tile_x: int, tile_z: int, span_tiles: int = 3) -> np.ndarray:
    y0 = tile_z * 512
    x0 = tile_x * 512
    sz = span_tiles * 512
    return arr[y0:y0+sz, x0:x0+sz]


def main() -> int:
    print("[compare] reading masks (may be slow on 50k TIFs)...", flush=True)
    rock_a = _read(MASKS / "rock_gap.tif")
    rock_b = _read(MASKS / "rock_gap_catmull.tif")
    snow_a = _read(MASKS / "snow_gap.tif")
    snow_b = _read(MASKS / "snow_gap_catmull.tif")

    for name, a, b in [("rock_gap", rock_a, rock_b), ("snow_gap", snow_a, snow_b)]:
        pct_a = 100.0 * a.sum() / a.size
        pct_b = 100.0 * b.sum() / b.size
        # Row-by-row diff to avoid the 2.3 GB bool allocation at 50k^2
        disagree = 0
        ROW_CHUNK = 1000
        for y0 in range(0, a.shape[0], ROW_CHUNK):
            y1 = min(y0 + ROW_CHUNK, a.shape[0])
            disagree += int((a[y0:y1] != b[y0:y1]).sum())
        print(f"  {name}: cubic={pct_a:.3f}%  catmull={pct_b:.3f}%  disagree={disagree:,} px ({100.0*disagree/a.size:.3f}%)", flush=True)

    # Side-by-side rendering on the (24,80) alpine tile crop (3x3 around it).
    tx, tz, span = 23, 79, 3
    r_cubic = _crop(rock_a, tx, tz, span)
    r_catm  = _crop(rock_b, tx, tz, span)
    s_cubic = _crop(snow_a, tx, tz, span)
    s_catm  = _crop(snow_b, tx, tz, span)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(12, 12))
    for ax, img, title in [
        (axes[0, 0], r_cubic, "rock_gap (cubic_spline + blue-noise)"),
        (axes[0, 1], r_catm,  "rock_gap_catmull (Catmull-Rom + blue-noise)"),
        (axes[1, 0], s_cubic, "snow_gap (cubic_spline + blue-noise)"),
        (axes[1, 1], s_catm,  "snow_gap_catmull (Catmull-Rom + blue-noise)"),
    ]:
        ax.imshow(img, cmap="gray_r", vmin=0, vmax=1, interpolation="nearest")
        ax.set_title(title, fontsize=11)
        ax.set_xticks([]); ax.set_yticks([])

    fig.suptitle(f"Catmull-Rom vs cubic_spline — tiles ({tx},{tz}) through ({tx+span-1},{tz+span-1})", fontsize=13)
    fig.tight_layout()
    OUT.parent.mkdir(exist_ok=True)
    fig.savefig(OUT, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"[compare] wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
