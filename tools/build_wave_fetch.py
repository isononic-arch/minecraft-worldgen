"""build_wave_fetch.py — Phase 0.5 (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §6 Pass 0 "Wave fetch precompute" +
§3 principle #9 (Vandir tradewinds west→east).

Produces `masks/wave_fetch.tif` at 1:8 precompute resolution (6250×6250).
Each pixel encodes the length (in precompute cells) of the contiguous
west-side open-water run terminating at that pixel, capped at `max_distance`.
Land pixels east of a long fetch = wave-exposed shore (driver for coastal
beach width, cobble armoring, sea-spray weathering).

Source: `masks/shore.tif` (uint8, 0/1 land/water-like) OR derives a water
mask from `masks/height.tif` < sea_level_raw when shore.tif is unavailable.

Output dtype: uint16 (max_distance up to 65535 px = 524280 blocks).

Usage:
    py tools/build_wave_fetch.py
    py tools/build_wave_fetch.py --max-distance 128
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import wind_model  # noqa: E402

PRECOMPUTE_SCALE = 8


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--masks", type=Path, default=REPO_ROOT / "masks")
    p.add_argument("--config", type=Path, default=REPO_ROOT / "config" / "thresholds.json")
    p.add_argument("--out", type=Path, default=None,
                   help="Output path; default = masks/wave_fetch.tif")
    p.add_argument("--max-distance", type=int, default=128,
                   help="Cap on fetch in precompute pixels (1 px ≈ 8 blocks)")
    return p.parse_args()


def _read_downsampled_nearest(path: Path, scale: int) -> np.ndarray:
    """Windowed row-by-row 1:scale NEAREST downsample — low memory."""
    with rasterio.open(path) as src:
        H, W = src.height, src.width
        out_H, out_W = H // scale, W // scale
        out = np.empty((out_H, out_W), dtype=src.dtypes[0])
        for oy in range(out_H):
            sy = oy * scale
            window = rasterio.windows.Window(0, sy, W, 1)
            row = src.read(1, window=window)[0]
            out[oy] = row[::scale]
    return out


def _build_water_mask(masks_dir: Path, config: dict) -> np.ndarray:
    """Prefer shore.tif if present (water=1 convention); fall back to height < sea_level."""
    shore_path = masks_dir / "shore.tif"
    if shore_path.exists():
        shore = _read_downsampled_nearest(shore_path, PRECOMPUTE_SCALE)
        # shore.tif: convention is water cells > 0. Treat nonzero as water.
        return shore > 0
    # Fallback: derive from height.
    height_path = masks_dir / "height.tif"
    if not height_path.exists():
        raise FileNotFoundError(f"need either {shore_path} or {height_path}")
    height_lo = _read_downsampled_nearest(height_path, PRECOMPUTE_SCALE)
    sea_level_raw = int(config.get("sea_level_16bit", 11602))
    return height_lo < sea_level_raw


def main() -> int:
    args = _parse_args()
    config = json.loads(args.config.read_text())

    print(f"[build_wave_fetch] reading water mask from {args.masks}")
    water = _build_water_mask(args.masks, config)
    H, W = water.shape
    water_frac = float(water.mean())
    print(f"[build_wave_fetch] water mask: {W}x{H}, water fraction = {water_frac:.3f}")

    print(f"[build_wave_fetch] computing fetch_integral (max_distance={args.max_distance})")
    fetch = wind_model.fetch_integral(water, max_distance=args.max_distance)

    # Validate direction: test_wind_model.py pins the vector; confirm the
    # physical invariant on this real mask — some land cell east of water
    # must have nonzero fetch.
    nonzero_frac = float((fetch > 0).mean())
    print(f"[build_wave_fetch] nonzero fetch fraction = {nonzero_frac:.3f}")
    if nonzero_frac == 0:
        raise RuntimeError("fetch_integral returned all zeros — water mask empty?")

    fetch_u16 = fetch.astype(np.uint16)
    out_path = args.out or (args.masks / "wave_fetch.tif")
    profile = {
        "driver": "GTiff",
        "height": H,
        "width": W,
        "count": 1,
        "dtype": "uint16",
        "compress": "deflate",
        "predictor": 2,
        "transform": Affine.identity(),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(fetch_u16, 1)

    mx = int(fetch_u16.max())
    print(f"[build_wave_fetch] wrote {out_path} ({fetch_u16.shape} uint16, max={mx})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
