"""
rebuild_beach.py — Precompute beach.tif globally at 1:8

Beaches form where ocean meets land at sea level on gentle slopes.
User directive (S51): beach ONLY at Y=63, no simplex noise, sand-only.

The precompute mask identifies candidate pixels via EDT + elevation + slope
gating.  The per-pixel Y=63 constraint is enforced downstream in
eco_gradients.py (surface_y == SEA_LEVEL).

Output: masks/beach.tif (uint8 0-255 gradient at 50k, bilinear upscale)

Usage:
    python rebuild_beach.py [--max-dist 3] [--elev-band 20]
"""
from __future__ import annotations
import argparse, gc, sys, time
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from scipy.ndimage import distance_transform_edt, gaussian_filter

sys.path.insert(0, str(Path(__file__).resolve().parent))

_SCRIPT_DIR = Path(__file__).resolve().parent
MASKS_DIR  = _SCRIPT_DIR / "masks" if (_SCRIPT_DIR / "masks").is_dir() else Path(r"C:\Users\nicho\minecraft-worldgen\masks")
SCALE      = 8
FULL_SIZE  = 50_000
DS_SIZE    = FULL_SIZE // SCALE  # 6250
SEA_RAW    = 17050


def read_ds(name, resamp=Resampling.nearest):
    path = MASKS_DIR / f"{name}.tif"
    with rasterio.open(str(path)) as src:
        return src.read(1, out_shape=(DS_SIZE, DS_SIZE), resampling=resamp)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-dist", type=float, default=3.0,
                        help="Max EDT distance in 1:8 pixels for beach (3 = 24 blocks)")
    parser.add_argument("--elev-band", type=float, default=20.0,
                        help="Raw height units above SEA_RAW for beach eligibility "
                             "(20 ≈ ~2.5m MC Y — tight gate, real filter is Y=63 in eco_grads)")
    parser.add_argument("--slope-start", type=float, default=8.0,
                        help="Slope degrees where beach starts thinning")
    parser.add_argument("--slope-end", type=float, default=15.0,
                        help="Slope degrees where beach fully excluded")
    args = parser.parse_args()

    t_total = time.perf_counter()

    # ── 1. Read height mask ──────────────────────────────────────────
    print("Reading height at 1:8 ...", flush=True)
    t0 = time.perf_counter()
    height_raw = read_ds("height", Resampling.average).astype(np.float32)
    H, W = height_raw.shape
    print(f"  {H}x{W}, read in {time.perf_counter()-t0:.1f}s", flush=True)

    land_mask = height_raw > SEA_RAW
    ocean_mask = ~land_mask

    # ── 2. EDT from ocean ────────────────────────────────────────────
    print("Computing EDT from ocean ...", flush=True)
    t0 = time.perf_counter()
    dist_to_ocean = distance_transform_edt(land_mask).astype(np.float32)
    print(f"  Done in {time.perf_counter()-t0:.1f}s", flush=True)
    print(f"  dist_to_ocean: max={dist_to_ocean.max():.1f} px, "
          f"land within {args.max_dist}px: "
          f"{int(((dist_to_ocean > 0) & (dist_to_ocean <= args.max_dist)).sum())} px",
          flush=True)

    # ── 3. Distance gate ─────────────────────────────────────────────
    dist_gate = np.clip(1.0 - dist_to_ocean / args.max_dist, 0.0, 1.0).astype(np.float32)
    dist_gate[ocean_mask] = 0.0
    del dist_to_ocean; gc.collect()

    # ── 4. Elevation gate ────────────────────────────────────────────
    # Tight band just above sea level.  Real Y=63 filter is in eco_gradients.
    elev_above_sea = height_raw - SEA_RAW
    elev_gate = np.clip(1.0 - elev_above_sea / args.elev_band, 0.0, 1.0).astype(np.float32)
    elev_gate[ocean_mask] = 0.0
    elev_gate[elev_above_sea < 0] = 0.0
    del elev_above_sea; gc.collect()

    # ── 5. Slope gate ────────────────────────────────────────────────
    print("Computing slope gate ...", flush=True)
    sy = np.float32(-64.0) + (height_raw * np.float32(512.0 / 65535.0))
    del height_raw; gc.collect()
    gy = np.gradient(sy, axis=0).astype(np.float32) / np.float32(SCALE)
    gx = np.gradient(sy, axis=1).astype(np.float32) / np.float32(SCALE)
    slope_deg = np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)
    del gx, gy, sy; gc.collect()
    print(f"  slope_deg: min={slope_deg.min():.1f}° max={slope_deg.max():.1f}° "
          f"mean={slope_deg.mean():.1f}°", flush=True)

    slope_gate = np.clip(
        1.0 - (slope_deg - args.slope_start) / (args.slope_end - args.slope_start),
        0.0, 1.0,
    ).astype(np.float32)
    del slope_deg; gc.collect()

    # ── 6. Combine (no noise — S51 directive) ────────────────────────
    print("Combining factors (no boundary noise) ...", flush=True)
    beach_score = (dist_gate * elev_gate * slope_gate).astype(np.float32)
    del dist_gate, elev_gate, slope_gate; gc.collect()

    # ── 7. Final cleanup ─────────────────────────────────────────────
    beach_score[ocean_mask] = 0.0
    # Light smooth for bilinear-friendly edges (no staircases)
    beach_score = gaussian_filter(beach_score, sigma=0.8).astype(np.float32)
    beach_score[ocean_mask] = 0.0
    beach_score = np.clip(beach_score, 0.0, 1.0)

    # ── 8. Stats ─────────────────────────────────────────────────────
    print("\nCoverage stats:", flush=True)
    n_land = int(land_mask.sum())
    for thr in [0.01, 0.10, 0.30, 0.50]:
        n = int((beach_score >= thr).sum())
        print(f"  beach_score>={thr}: {n} px ({100*n/max(n_land,1):.2f}% of land)")

    result = np.clip(beach_score * 255.0, 0, 255).astype(np.uint8)
    del beach_score, land_mask, ocean_mask; gc.collect()

    # ── 9. Write at 50k ──────────────────────────────────────────────
    out_path = MASKS_DIR / "beach.tif"
    print(f"\nWriting {out_path} at 50k ...", flush=True)
    t0 = time.perf_counter()
    from core.hydrology_precompute import write_upscaled
    write_upscaled(result, out_path, dtype="uint8", scale=SCALE,
                   full_size=FULL_SIZE, chunk_rows=50, interpolation="bilinear")
    del result
    print(f"  Written in {time.perf_counter()-t0:.1f}s", flush=True)

    print(f"\nTotal: {time.perf_counter()-t_total:.1f}s")


if __name__ == "__main__":
    main()
