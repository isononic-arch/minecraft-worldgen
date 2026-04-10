"""
rebuild_sand_dunes.py — Precompute sand_dunes.tif globally at 1:8

Sand dunes form by wind transport. Sand particles get blown until they
encounter a low-energy zone — a topographic basin, leeward slope, or
gentle valley wall. Steep terrain (>20°) sheds sand by gravity. Rocky
outcrops poke through where bedrock is too steep.

The reference image "Dune flows on rocky red desert" shows sand flowing
through a corridor between rocky highlands — NOT spread evenly. We
emulate this with multiplicative gating:

  sand_score = slope_gate × basin_factor × wind_lee × concavity × biome_weight

Where:
  - slope_gate    : sand only sits on slopes < 18°, soft falloff to 25°
  - basin_factor  : INVERSE TPI (sy below local mean = basin = sand trap)
  - wind_lee      : leeward side of obstacles accumulates drifted sand
  - concavity     : laplacian — drainage paths funnel wind/sand
  - biome_weight  : 1.0 SAND_DUNE_DESERT, 0.4 DESERT_STEPPE_TRANSITION,
                    0.2 SEMI_ARID_SHRUBLAND, 0 elsewhere

Output: masks/sand_dunes.tif (uint8 0-255 gradient at 50k)

Usage:
    python rebuild_sand_dunes.py [--wind-dir 270] [--basin-kernel 30]
"""
from __future__ import annotations
import argparse, gc, sys, time
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from scipy.ndimage import uniform_filter, gaussian_filter, gaussian_laplace

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR  = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
SCALE      = 8
FULL_SIZE  = 50_000
DS_SIZE    = FULL_SIZE // SCALE  # 6250
SEA_RAW    = 17050

# Biome weights for sand bleed-through
SAND_BIOME_WEIGHTS = {
    170: 1.00,  # SAND_DUNE_DESERT — primary
    190: 0.40,  # DESERT_STEPPE_TRANSITION — sand bleeds in
    200: 0.20,  # SEMI_ARID_SHRUBLAND — light sand fingers
}


def read_ds(name, resamp=Resampling.nearest):
    path = MASKS_DIR / f"{name}.tif"
    with rasterio.open(str(path)) as src:
        return src.read(1, out_shape=(DS_SIZE, DS_SIZE), resampling=resamp)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wind-dir", type=float, default=270.0,
                        help="Prevailing wind FROM direction (270=westerly)")
    parser.add_argument("--basin-kernel", type=int, default=30,
                        help="TPI kernel at 1:8 (30 = 240 blocks)")
    parser.add_argument("--slope-shed", type=float, default=18.0,
                        help="Slope degrees where sand starts shedding")
    parser.add_argument("--slope-shed-end", type=float, default=25.0,
                        help="Slope degrees where sand fully sheds")
    parser.add_argument("--basin-strength", type=float, default=1.5,
                        help="Multiplier on basin factor")
    parser.add_argument("--noise-scale", type=float, default=80.0,
                        help="Boundary noise scale at 1:8 (80 = 640 blocks)")
    parser.add_argument("--noise-amplitude", type=float, default=0.20,
                        help="Boundary noise amplitude (±0.20)")
    parser.add_argument("--biome-dilate", type=int, default=12,
                        help="Pixels at 1:8 to dilate biome weight (~96 blocks)")
    args = parser.parse_args()

    t_total = time.perf_counter()

    # ── 1. Read masks ────────────────────────────────────────────────
    print("Reading masks at 1:8 ...", flush=True)
    t0 = time.perf_counter()
    height_raw = read_ds("height", Resampling.average).astype(np.float32)
    override = read_ds("override", Resampling.nearest).astype(np.uint8)
    H, W = height_raw.shape
    print(f"  {H}x{W}, read in {time.perf_counter()-t0:.1f}s", flush=True)

    land_mask = height_raw > SEA_RAW
    sy = np.float32(-64.0) + (height_raw * np.float32(512.0 / 65535.0))
    del height_raw; gc.collect()

    # ── 2. Terrain derivatives ───────────────────────────────────────
    # CORRECTED slope math: np.gradient(sy) returns dY per source pixel,
    # but each source pixel = SCALE blocks horizontally. So actual block-
    # space slope = gradient / SCALE. The old rebuild_*.py scripts skip
    # this division → inflate slopes ~8x. rock_exposure tuned around the
    # inflated values (thresholds 48-72° instead of 6-9° actual) so it
    # works in practice but the math is wrong.
    # slope.tif is also NOT linear in degrees (Gaea normalization unknown).
    # We use the corrected np.gradient approach instead.
    print("Computing terrain derivatives ...", flush=True)
    t0 = time.perf_counter()
    gy = np.gradient(sy, axis=0).astype(np.float32) / np.float32(SCALE)
    gx = np.gradient(sy, axis=1).astype(np.float32) / np.float32(SCALE)
    slope_deg = np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)
    # Aspect from un-divided gradients (direction only, magnitude irrelevant)
    aspect = np.arctan2(-gx, gy).astype(np.float32)
    del gx, gy; gc.collect()
    print(f"  Done in {time.perf_counter()-t0:.1f}s", flush=True)
    # Quick sanity check
    print(f"  slope_deg: min={slope_deg.min():.1f}° max={slope_deg.max():.1f}° mean={slope_deg.mean():.1f}°", flush=True)

    # ── 3. Slope gate (sand can only sit on gentle slopes) ──────────
    # 100% at slope=0, 100% at slope=shed (18), linear falloff to 0 at shed_end (25)
    print("Computing slope gate ...", flush=True)
    slope_gate = np.clip(
        1.0 - (slope_deg - args.slope_shed) / (args.slope_shed_end - args.slope_shed),
        0.0, 1.0,
    ).astype(np.float32)

    # ── 4. Basin factor (INVERSE TPI — sand collects in valleys) ────
    print(f"Computing basin factor (TPI kernel={args.basin_kernel}) ...", flush=True)
    t0 = time.perf_counter()
    mean_elev = uniform_filter(sy, size=args.basin_kernel).astype(np.float32)
    tpi = sy - mean_elev  # negative = basin
    del mean_elev
    # Normalize: most-negative TPI gets highest score, neutral TPI gets 0.5
    tpi_p95 = float(np.percentile(np.abs(tpi[land_mask]), 95)) if land_mask.any() else 1.0
    basin = np.clip(0.5 - tpi / max(2.0 * tpi_p95, 1.0), 0.0, 1.0).astype(np.float32)
    del tpi; gc.collect()
    print(f"  Done in {time.perf_counter()-t0:.1f}s", flush=True)

    # ── 5. Wind leeward factor ───────────────────────────────────────
    # Leeward side of obstacles accumulates sand. cos(aspect - wind + 180°)
    # gives high values on the side facing AWAY from wind.
    print("Computing wind leeward factor ...", flush=True)
    wind_rad = np.float32(np.radians(args.wind_dir))
    leeward = np.cos(aspect - wind_rad + np.float32(np.pi)).astype(np.float32)
    # Normalize: -1 (windward) → 0.5, +1 (leeward) → 1.0
    wind_lee = np.clip(0.75 + 0.25 * leeward, 0.5, 1.0).astype(np.float32)
    del leeward, aspect; gc.collect()

    # ── 6. Concavity (drainage path proxy) ───────────────────────────
    # Laplacian: positive = concave (basin/valley), negative = convex (ridge)
    print("Computing concavity ...", flush=True)
    laplace = -gaussian_laplace(sy, sigma=4.0).astype(np.float32)
    lap_p95 = float(np.percentile(np.abs(laplace[land_mask]), 95)) if land_mask.any() else 1.0
    concavity = np.clip(0.5 + 0.5 * laplace / max(lap_p95, 0.01), 0.3, 1.0).astype(np.float32)
    del laplace; gc.collect()

    # ── 7. Biome weight ──────────────────────────────────────────────
    # Multi-biome weighted gate. Dilated to allow sand fingers to extend
    # slightly past biome boundaries (matches reference image where sand
    # crosses into the rocky steppe).
    print(f"Building biome weight (dilate={args.biome_dilate}) ...", flush=True)
    biome_weight = np.zeros((H, W), dtype=np.float32)
    for code, weight in SAND_BIOME_WEIGHTS.items():
        bm = override == code
        if bm.any():
            biome_weight[bm] = weight
    del override; gc.collect()

    # Dilate via gaussian smoothing for soft boundary
    biome_weight_smooth = gaussian_filter(biome_weight, sigma=float(args.biome_dilate)).astype(np.float32)
    # Take max(original, smoothed*0.7) so interior stays full strength
    biome_weight = np.maximum(biome_weight, biome_weight_smooth * 0.7)
    del biome_weight_smooth; gc.collect()

    # ── 8. Combine all factors ───────────────────────────────────────
    # WEIGHTED SUM (Session 39 lesson) — multiplicative composition
    # crushes scores when any factor is moderate. Sum allows any strong
    # signal to drive sand accumulation, with slope/biome as hard gates.
    print("Combining factors ...", flush=True)
    accumulation = (
        np.float32(0.40) * basin +
        np.float32(0.20) * wind_lee +
        np.float32(0.20) * concavity +
        np.float32(0.20)  # baseline — flat desert defaults to sand
    ).astype(np.float32)
    sand_score = (slope_gate * biome_weight * accumulation).astype(np.float32)
    del slope_gate, basin, wind_lee, concavity, biome_weight, accumulation
    gc.collect()

    # ── 9. Boundary noise (organic edges) ────────────────────────────
    print(f"Adding boundary noise (scale={args.noise_scale}) ...", flush=True)
    t0_n = time.perf_counter()
    try:
        import opensimplex as ox
        NOISE_DS = 4
        nH, nW = H // NOISE_DS, W // NOISE_DS
        xs = np.arange(nW, dtype=np.float64) / (args.noise_scale / NOISE_DS)
        zs = np.arange(nH, dtype=np.float64) / (args.noise_scale / NOISE_DS)
        ox.seed(77001)
        n1 = ox.noise2array(xs, zs).astype(np.float32)
        xs2 = np.arange(nW, dtype=np.float64) / (args.noise_scale / NOISE_DS * 0.45)
        zs2 = np.arange(nH, dtype=np.float64) / (args.noise_scale / NOISE_DS * 0.45)
        ox.seed(77002)
        n2 = ox.noise2array(xs2, zs2).astype(np.float32)
        combined = np.float32(0.65) * n1 + np.float32(0.35) * n2
        del n1, n2, xs, zs, xs2, zs2
        from PIL import Image as _pil
        _np_pil = _pil.fromarray(combined)
        sand_noise = np.array(
            _np_pil.resize((W, H), _pil.BILINEAR), dtype=np.float32
        )
        del combined, _np_pil
    except ImportError:
        rng = np.random.default_rng(77001)
        sand_noise = (rng.random((H, W)).astype(np.float32) - 0.5) * 2.0

    sand_score = sand_score * (1.0 + sand_noise * args.noise_amplitude)
    del sand_noise; gc.collect()
    print(f"  Done in {time.perf_counter()-t0_n:.1f}s", flush=True)

    # ── 10. Final cleanup ────────────────────────────────────────────
    sand_score[~land_mask] = 0.0
    sand_score = gaussian_filter(sand_score, sigma=1.5).astype(np.float32)
    sand_score[~land_mask] = 0.0
    sand_score = np.clip(sand_score, 0.0, 1.0)

    # ── 11. Stats ────────────────────────────────────────────────────
    print("\nCoverage stats:", flush=True)
    n_land = int(land_mask.sum())
    for thr in [0.05, 0.15, 0.30, 0.50]:
        n = int((sand_score >= thr).sum())
        print(f"  sand_score>={thr}: {n} px ({100*n/max(n_land,1):.1f}% of land)")

    result = np.clip(sand_score * 255.0, 0, 255).astype(np.uint8)
    del sand_score, sy, slope_deg, land_mask; gc.collect()

    # ── 12. Write at 50k ─────────────────────────────────────────────
    out_path = MASKS_DIR / "sand_dunes.tif"
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
