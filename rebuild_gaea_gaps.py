"""
rebuild_gaea_gaps.py — Stitch Gaea slope + dusting tiles, upscale to 50k,
threshold with blue-noise dither, write `masks/rock_gap.tif` + `masks/snow_gap.tif`.

Source: `C:/Gaea Stuff/Slope/Slope_Out_y{0-7}_x{0-7}.tif` (64 tiles, each
1024x1024 uint16, no edge overlap — verified at plan time) and
`C:/Gaea Stuff/Dusting/Dusting_Out_y{0-7}_x{0-7}.tif` (same layout).

Pipeline:
  1. Stitch 8x8 grid → 8192x8192 uint16 (in RAM, ~128MB per mask).
  2. Write intermediate 8k TIF for inspection (`masks/gaea_slope_8k.tif`,
     `masks/gaea_dusting_8k.tif`).
  3. Upscale + threshold + blue-noise dither → 50k uint8 (`masks/rock_gap.tif`,
     `masks/snow_gap.tif`) via `core.upscale.upscale_continuous_then_threshold_dither`.

Orientation: y=0 → top row, x=0 → left column (standard image convention).
If the diag tool reveals cliffs end up on the wrong side of the world, flip
via the `FLIP_Y` / `FLIP_X` constants below — do not change the stitch loop.

Tunable threshold + dither-width constants live at the top of this file.
Iterate on them via `diag_gaea_gaps_debug.py` (~0.4s per cycle) before
committing to a run of this script (~1-2 min).

Written for S56 — see plan at
`.claude/plans/partitioned-napping-stonebraker.md`.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import rasterio

from core.upscale import upscale_continuous_then_threshold_dither


# ─── Source paths ────────────────────────────────────────────────────────────
GAEA_ROOT = Path("C:/Gaea Stuff")
SLOPE_DIR = GAEA_ROOT / "Slope"
DUSTING_DIR = GAEA_ROOT / "Dusting"

# ─── Output paths ────────────────────────────────────────────────────────────
MASKS_DIR = Path("masks")
SLOPE_8K_PATH = MASKS_DIR / "gaea_slope_8k.tif"
DUSTING_8K_PATH = MASKS_DIR / "gaea_dusting_8k.tif"
ROCK_GAP_50K_PATH = MASKS_DIR / "rock_gap.tif"
SNOW_GAP_50K_PATH = MASKS_DIR / "snow_gap.tif"

# ─── Stitch parameters ───────────────────────────────────────────────────────
GRID = 8                 # 8x8 tile grid
TILE = 1024              # per-tile side (uint16)
SRC_SIZE = GRID * TILE   # 8192
TARGET_SIZE = 50000

# Orientation flags — keep False initially; flip only if diag render shows
# cliffs on the wrong side of the world when compared to `masks/height.tif`.
FLIP_Y = False
FLIP_X = False

# ─── Threshold + dither-width constants (tunable) ────────────────────────────
# Initial guesses from source distribution probing at plan time:
#   Slope   — bimodal, p50≈12k, p90≈59k on root file
#   Dusting — long-tail sparse, low-tile max ~1000, high-tile p99≈1852
# Expect visual iteration via diag_gaea_gaps_debug.py.
#
# Band is [threshold - dither_width/2, threshold + dither_width/2].
# Outside the band, the output is fully 0 or 1. Inside, probability ramps
# 0→1 and is thresholded against the blue-noise tile.
SLOPE_THRESHOLD = 52000.0
SLOPE_DITHER_WIDTH = 18000.0

DUSTING_THRESHOLD = 1500.0
DUSTING_DITHER_WIDTH = 800.0


# ─── Stitch helper ───────────────────────────────────────────────────────────

def _stitch_tiles(tile_dir: Path, name_prefix: str) -> np.ndarray:
    """Read 64 tiles named `{prefix}_y{0-7}_x{0-7}.tif` and return an
    8192x8192 uint16 array. Raises if any tile is missing or the wrong
    size / dtype.

    FLIP_Y / FLIP_X are applied at the end if set.
    """
    out = np.zeros((SRC_SIZE, SRC_SIZE), dtype=np.uint16)
    t_start = time.time()

    for y in range(GRID):
        for x in range(GRID):
            p = tile_dir / f"{name_prefix}_y{y}_x{x}.tif"
            if not p.exists():
                raise FileNotFoundError(f"Missing Gaea tile: {p}")
            with rasterio.open(str(p)) as r:
                arr = r.read(1)
            if arr.shape != (TILE, TILE):
                raise ValueError(f"{p}: expected ({TILE},{TILE}), got {arr.shape}")
            if arr.dtype != np.uint16:
                # Gaea sometimes exports uint32 — coerce to uint16 via clipping.
                arr = np.clip(arr, 0, 65535).astype(np.uint16)

            out[y * TILE : (y + 1) * TILE, x * TILE : (x + 1) * TILE] = arr

    if FLIP_Y:
        out = np.flipud(out)
    if FLIP_X:
        out = np.fliplr(out)

    elapsed = time.time() - t_start
    print(
        f"  stitched {GRID*GRID} tiles from {tile_dir.name}: "
        f"shape={out.shape} dtype={out.dtype} min={out.min()} max={out.max()} "
        f"mean={out.mean():.1f} p95={np.percentile(out, 95):.0f} "
        f"[{elapsed:.1f}s]"
    )
    return out


def _write_uint16_tif(data: np.ndarray, path: Path) -> None:
    """Write a 2D uint16 array to an LZW-tiled GeoTIFF for inspection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "width": data.shape[1],
        "height": data.shape[0],
        "count": 1,
        "dtype": "uint16",
        "compress": "lzw",
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
    }
    with rasterio.open(str(path), "w", **profile) as dst:
        dst.write(data, 1)


# ─── Main ────────────────────────────────────────────────────────────────────

def run(
    do_slope: bool = True,
    do_dusting: bool = True,
    skip_8k_write: bool = False,
    skip_50k_write: bool = False,
) -> None:
    MASKS_DIR.mkdir(parents=True, exist_ok=True)
    t_top = time.time()

    if do_slope:
        print("[1/2] Slope")
        slope_8k = _stitch_tiles(SLOPE_DIR, "Slope_Out")
        if not skip_8k_write:
            print(f"  writing {SLOPE_8K_PATH}")
            _write_uint16_tif(slope_8k, SLOPE_8K_PATH)
        if not skip_50k_write:
            print(
                f"  upscaling -> {ROCK_GAP_50K_PATH} "
                f"(T={SLOPE_THRESHOLD:g}, width={SLOPE_DITHER_WIDTH:g})"
            )
            t0 = time.time()
            upscale_continuous_then_threshold_dither(
                slope_8k,
                ROCK_GAP_50K_PATH,
                threshold=SLOPE_THRESHOLD,
                dither_width=SLOPE_DITHER_WIDTH,
                target_size=TARGET_SIZE,
                interpolation="cubic_spline",
            )
            print(f"  rock_gap.tif written [{time.time() - t0:.1f}s]")

    if do_dusting:
        print("[2/2] Dusting")
        dusting_8k = _stitch_tiles(DUSTING_DIR, "Dusting_Out")
        if not skip_8k_write:
            print(f"  writing {DUSTING_8K_PATH}")
            _write_uint16_tif(dusting_8k, DUSTING_8K_PATH)
        if not skip_50k_write:
            print(
                f"  upscaling -> {SNOW_GAP_50K_PATH} "
                f"(T={DUSTING_THRESHOLD:g}, width={DUSTING_DITHER_WIDTH:g})"
            )
            t0 = time.time()
            upscale_continuous_then_threshold_dither(
                dusting_8k,
                SNOW_GAP_50K_PATH,
                threshold=DUSTING_THRESHOLD,
                dither_width=DUSTING_DITHER_WIDTH,
                target_size=TARGET_SIZE,
                interpolation="cubic_spline",
            )
            print(f"  snow_gap.tif written [{time.time() - t0:.1f}s]")

    print(f"\n[rebuild_gaea_gaps] done in {time.time() - t_top:.1f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--slope-only", action="store_true", help="Skip dusting")
    ap.add_argument("--dusting-only", action="store_true", help="Skip slope")
    ap.add_argument(
        "--skip-8k",
        action="store_true",
        help="Skip writing the 8k intermediate TIFs (saves ~10s)",
    )
    ap.add_argument(
        "--skip-50k",
        action="store_true",
        help="Skip the 50k upscale (stitch + 8k only; useful for orientation checks)",
    )
    args = ap.parse_args()

    run(
        do_slope=not args.dusting_only,
        do_dusting=not args.slope_only,
        skip_8k_write=args.skip_8k,
        skip_50k_write=args.skip_50k,
    )
