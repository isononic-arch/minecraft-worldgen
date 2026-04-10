"""
rebuild_floodplain.py — Precompute hydro_floodplain.tif globally at 1:8

Reads existing masks at 1:8 scale (6250×6250), computes the floodplain
binary mask using Strahler flood-fill, max-filter propagation, slope gate,
elevation factor, coast decay, and longitudinal noise.  Writes result at
50k×50k via chunked NEAREST upscale.

The per-tile eco_gradients.py then just reads this mask — zero seams.

Output values:  0 = not floodplain,  1 = floodplain

Usage:
    python rebuild_floodplain.py [--preset B_wide]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from scipy.ndimage import (
    distance_transform_edt,
    label as ndlabel,
    maximum_filter,
    gaussian_filter,
    binary_closing,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR   = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG_PATH = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
SCALE       = 8
FULL_SIZE   = 50_000
DS_SIZE     = FULL_SIZE // SCALE  # 6250

PRESETS = {
    "A_moderate": {1: 25, 2: 50, 3: 80, 4: 120, 5: 160},
    "B_wide":     {1: 35, 2: 70, 3: 110, 4: 170, 5: 230},
    "C_dramatic": {1: 45, 2: 90, 3: 150, 4: 220, 5: 300},
}

# Sea level in raw uint16 height space
SEA_RAW = 17050


def read_1_8(name: str, resampling=Resampling.nearest) -> np.ndarray:
    path = MASKS_DIR / f"{name}.tif"
    with rasterio.open(str(path)) as src:
        return src.read(1, out_shape=(DS_SIZE, DS_SIZE), resampling=resampling)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="B_wide", choices=list(PRESETS))
    args = parser.parse_args()

    flood_base = PRESETS[args.preset]
    print(f"Preset: {args.preset} -> {flood_base}")

    t_total = time.perf_counter()
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    # ── 1. Read masks at 1:8 ─────────────────────────────────────────
    print("Reading masks at 1:8 ...", flush=True)
    t0 = time.perf_counter()

    height_raw = read_1_8("height", Resampling.average).astype(np.float32)
    hydro_order = read_1_8("hydro_order", Resampling.nearest).astype(np.uint8)
    hydro_centerline = read_1_8("hydro_centerline", Resampling.nearest).astype(np.uint8)
    hydro_width = read_1_8("hydro_width", Resampling.nearest).astype(np.float32)
    hydro_lake = read_1_8("hydro_lake", Resampling.nearest)

    H, W = height_raw.shape
    print(f"  Shape: {H}×{W}, read in {time.perf_counter()-t0:.1f}s", flush=True)

    # ── 2. Build masks ───────────────────────────────────────────────
    print("Building masks ...", flush=True)
    # Height to MC Y (approximate spline — just need relative elevation)
    # At 1:8, height_raw is uint16 values
    land_mask = height_raw > SEA_RAW
    # Approximate surface Y for slope and elevation factor
    # Simple linear: Y = -64 + (height_raw / 65535) * 512
    sy = -64.0 + (height_raw / 65535.0) * 512.0

    # Channel mask from hydro_order
    channel_mask = hydro_order > 0

    # River water: centerline > 0 gives us channels + braid fill at 1:8
    # Also include hydro_order channels (some thin tributaries)
    river_water = (hydro_centerline > 0) | channel_mask
    # Exclude lake pixels
    lake_mask = hydro_lake > 0
    river_water = river_water & ~lake_mask

    n_river = river_water.sum()
    n_land = land_mask.sum()
    print(f"  River water: {n_river} px, Land: {n_land} px", flush=True)

    if not river_water.any():
        print("No river water found — writing empty mask.")
        result = np.zeros((H, W), dtype=np.uint8)
    else:
        # ── 3. Distance from river water edge ────────────────────────
        print("Computing distance from water edge ...", flush=True)
        t0 = time.perf_counter()
        dist_from_water = distance_transform_edt(~river_water).astype(np.float32)
        print(f"  Done in {time.perf_counter()-t0:.1f}s", flush=True)

        # ── 4. Strahler flood-fill through connected water ───────────
        print("Strahler flood-fill ...", flush=True)
        t0 = time.perf_counter()
        water_order = np.zeros((H, W), dtype=np.uint8)
        water_order[channel_mask] = hydro_order[channel_mask]

        rw_labeled, n_rw = ndlabel(river_water)
        print(f"  {n_rw} connected water components", flush=True)
        if n_rw > 0:
            comp_max_order = np.zeros(n_rw + 1, dtype=np.uint8)
            for s in range(5, 0, -1):
                s_mask = water_order == s
                if s_mask.any():
                    labels_at_s = rw_labeled[s_mask]
                    comp_max_order[labels_at_s] = np.maximum(
                        comp_max_order[labels_at_s], s)
            water_order = comp_max_order[rw_labeled].astype(np.uint8)
        print(f"  Done in {time.perf_counter()-t0:.1f}s", flush=True)

        # ── 5. Max-filter propagation (kernel=201) ───────────────────
        print("Max-filter propagation (size=201) ...", flush=True)
        t0 = time.perf_counter()
        # At 1:8 scale, 201 real-px = 201/8 ≈ 25 ds-px.
        # But we want 201 BLOCK equivalent, so 201/8 ≈ 25 at this scale.
        mf_size = max(201 // SCALE, 3)  # 25 at 1:8
        order_field = maximum_filter(water_order, size=mf_size)
        print(f"  kernel={mf_size}, done in {time.perf_counter()-t0:.1f}s", flush=True)

        # ── 6. Base radius from Strahler ─────────────────────────────
        # Radii are in BLOCKS — divide by SCALE for 1:8 pixel space
        base_radius = np.zeros((H, W), dtype=np.float32)
        for strahler, base_r in flood_base.items():
            base_radius[order_field >= strahler] = base_r / SCALE

        # ── 7. Width multiplier ──────────────────────────────────────
        width_field = maximum_filter(hydro_width, size=mf_size)
        width_mult = np.clip(
            0.8 + 0.6 * (width_field / max(width_field.max(), 1.0)),
            0.8, 1.4)

        # ── 8. Slope gate ────────────────────────────────────────────
        gy, gx = np.gradient(sy)
        cliff_deg = np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)
        slope_norm = np.clip(cliff_deg / 45.0, 0.0, 1.0)
        slope_gate = np.clip(1.0 - slope_norm * 1.5, 0.0, 1.0)

        # ── 9. Elevation factor ──────────────────────────────────────
        elev_factor = np.clip(2.0 - (sy - 63.0) / 150.0, 0.8, 2.0)
        elev_factor[~land_mask] = 0.0

        # ── 10. Distance-to-coast decay ──────────────────────────────
        print("Distance-to-coast ...", flush=True)
        t0 = time.perf_counter()
        dist_to_ocean = distance_transform_edt(land_mask).astype(np.float32)
        # Convert to block-space for the decay (1 ds-px = 8 blocks)
        dist_to_ocean_blocks = dist_to_ocean * SCALE
        coast_factor = np.clip(2.0 - dist_to_ocean_blocks / 300.0, 1.0, 2.0)
        print(f"  Done in {time.perf_counter()-t0:.1f}s", flush=True)

        # ── 11. Concavity boost ──────────────────────────────────────
        from scipy.ndimage import laplace
        conc_raw = laplace(sy)
        p2, p98 = np.percentile(conc_raw[land_mask], [2, 98]) if land_mask.any() else (0, 1)
        conc_clipped = np.clip(conc_raw, p2, p98)
        conc_norm = (conc_clipped - p2) / max(p98 - p2, 1e-6)
        conc_boost = np.clip(0.85 + 0.3 * conc_norm, 0.85, 1.15)

        # ── 12. Longitudinal noise ───────────────────────────────────
        print("Longitudinal noise ...", flush=True)
        try:
            import opensimplex as ox
            xs = np.arange(W, dtype=np.float64) / (160.0 / SCALE)
            zs = np.arange(H, dtype=np.float64) / (160.0 / SCALE)
            ox.seed(77004)
            n_fp = ox.noise2array(xs, zs).astype(np.float32)
            n_fp = gaussian_filter(n_fp, sigma=10.0 / SCALE)
            n_fp = (n_fp - n_fp.min()) / max(n_fp.max() - n_fp.min(), 1e-6)
            flood_noise = 0.45 + 0.55 * n_fp
        except ImportError:
            flood_noise = np.full((H, W), 0.75, dtype=np.float32)

        # ── 13. Final radius and threshold ───────────────────────────
        print("Computing floodplain mask ...", flush=True)
        t0 = time.perf_counter()
        flood_radius = (base_radius * width_mult * slope_gate
                        * conc_boost * elev_factor * coast_factor
                        * flood_noise)

        floodplain = (dist_from_water < flood_radius) & land_mask
        # Exclude water pixels
        floodplain = floodplain & ~river_water & ~lake_mask

        # Morphological closing (iterations=5 at 1:8 ≈ iterations=1 at 1:8)
        # Each iteration at 1:8 = 8 blocks, so 1 iteration bridges 8-block gaps
        if floodplain.any():
            floodplain = binary_closing(floodplain, iterations=2)
            floodplain = floodplain & land_mask & ~river_water & ~lake_mask

        n_flood = floodplain.sum()
        pct = n_flood * 100 / max(n_land, 1)
        print(f"  Floodplain: {n_flood} px ({pct:.1f}% of land), "
              f"done in {time.perf_counter()-t0:.1f}s", flush=True)

        result = floodplain.astype(np.uint8)

    # ── 14. Write at 50k ─────────────────────────────────────────────
    out_path = MASKS_DIR / "hydro_floodplain.tif"
    print(f"Writing {out_path} at 50k ...", flush=True)
    t0 = time.perf_counter()

    from core.hydrology_precompute import write_upscaled
    write_upscaled(result, out_path, dtype="uint8", scale=SCALE,
                   full_size=FULL_SIZE, chunk_rows=50, interpolation="bilinear")

    print(f"  Written in {time.perf_counter()-t0:.1f}s", flush=True)
    print(f"\nTotal: {time.perf_counter()-t_total:.1f}s")
    print(f"Output: {out_path}")


if __name__ == "__main__":
    main()
