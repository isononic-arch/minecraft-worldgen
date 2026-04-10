"""
rebuild_windthrow.py — Precompute wind_windthrow.tif globally at 1:8

Directional windthrow mapping based on:
  1. Topographic Position Index (ridge detection)
  2. Aspect alignment with prevailing wind (default: west 270deg)
  3. Elevation exposure
  4. Slope sweet-spot (moderate slopes most vulnerable)
  5. Anisotropic noise (elongated along wind direction)

Output: masks/wind_windthrow.tif (uint8 0/1 at 50k)

Usage:
    python rebuild_windthrow.py [--wind-dir 270] [--aniso 3.5] [--tpi-kernel 30]
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from scipy.ndimage import uniform_filter, gaussian_filter, binary_closing, label as ndlabel

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR   = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG_PATH = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
SCALE       = 8
FULL_SIZE   = 50_000
DS_SIZE     = FULL_SIZE // SCALE  # 6250
SEA_RAW     = 17050


def read_ds(name, resamp=Resampling.nearest):
    path = MASKS_DIR / f"{name}.tif"
    with rasterio.open(str(path)) as src:
        return src.read(1, out_shape=(DS_SIZE, DS_SIZE), resampling=resamp)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wind-dir", type=float, default=270.0,
                        help="Prevailing wind FROM direction in degrees (270=westerly)")
    parser.add_argument("--aniso", type=float, default=3.5,
                        help="Anisotropy ratio: how elongated swaths are along wind")
    parser.add_argument("--tpi-kernel", type=int, default=30,
                        help="TPI kernel size at 1:8 (30 = 240 blocks)")
    parser.add_argument("--noise-scale", type=float, default=60.0,
                        help="Noise base scale at 1:8")
    parser.add_argument("--elev-min", type=float, default=90.0,
                        help="Min MC Y for windthrow exposure")
    parser.add_argument("--target-pct", type=float, default=3.0,
                        help="Target percentage of forested land")
    args = parser.parse_args()

    t_total = time.perf_counter()
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    # ── 1. Read masks ────────────────────────────────────────────────
    print("Reading masks at 1:8 ...", flush=True)
    t0 = time.perf_counter()
    height_raw = read_ds("height", Resampling.average).astype(np.float32)
    override = read_ds("override", Resampling.nearest).astype(np.uint8)
    H, W = height_raw.shape
    print(f"  {H}x{W}, read in {time.perf_counter()-t0:.1f}s", flush=True)

    land_mask = height_raw > SEA_RAW
    # Approximate MC Y
    sy = -64.0 + (height_raw / 65535.0) * 512.0

    # ── 2. Topographic Position Index ────────────────────────────────
    print(f"TPI (kernel={args.tpi_kernel}) ...", flush=True)
    t0 = time.perf_counter()
    mean_elev = uniform_filter(sy, size=args.tpi_kernel)
    tpi = sy - mean_elev
    # Normalize positive values (ridges) to [0, 1]
    tpi_pos = np.clip(tpi, 0, None)
    tpi_max = np.percentile(tpi_pos[land_mask], 98) if land_mask.any() else 1.0
    tpi_norm = np.clip(tpi_pos / max(tpi_max, 0.1), 0.0, 1.0).astype(np.float32)
    print(f"  TPI range: [{tpi[land_mask].min():.1f}, {tpi[land_mask].max():.1f}], "
          f"done in {time.perf_counter()-t0:.1f}s", flush=True)

    # ── 3. Aspect alignment with wind direction ──────────────────────
    print(f"Aspect alignment (wind from {args.wind_dir} deg) ...", flush=True)
    gy, gx = np.gradient(sy)
    aspect = np.arctan2(-gx, gy)  # radians, compass convention
    wind_rad = np.radians(args.wind_dir)
    # Windward: facing INTO the wind scores highest
    windward = np.clip(np.cos(aspect - wind_rad), 0.0, 1.0)
    # Ridgetops get base exposure regardless of aspect
    wind_aspect = (0.35 + 0.65 * windward).astype(np.float32)
    wind_aspect[~land_mask] = 0.0

    # ── 4. Elevation factor ──────────────────────────────────────────
    # Windthrow at any forested elevation — TPI handles the exposure.
    # Slight boost at higher elevation where wind is stronger.
    elev_factor = np.clip((sy - 68.0) / 200.0, 0.0, 1.0).astype(np.float32)
    elev_factor[~land_mask] = 0.0

    # ── 5. Slope factor ──────────────────────────────────────────────
    slope_deg = np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)
    # Moderate slopes most vulnerable; flat and very steep less so
    slope_factor = np.exp(-0.5 * ((slope_deg - 25.0) / 18.0) ** 2).astype(np.float32)
    slope_factor = np.clip(slope_factor, 0.25, 1.0)

    # ── 6. Combined terrain score (weighted SUM, not product) ────────
    # Any single strong signal can drive windthrow — a high ridge at
    # moderate elevation still scores, unlike multiplicative which
    # crushes everything to near-zero.
    terrain_score = (
        0.45 * tpi_norm +        # ridge position is dominant driver
        0.30 * wind_aspect +     # windward aspect
        0.15 * elev_factor +     # elevation boost
        0.10 * slope_factor      # slope sweet-spot
    ).astype(np.float32)
    terrain_score[~land_mask] = 0.0

    # ── 7. Anisotropic noise (elongated along wind direction) ────────
    print(f"Anisotropic noise (ratio={args.aniso}, scale={args.noise_scale}) ...", flush=True)
    t0 = time.perf_counter()

    # Anisotropic noise: generate at 1/4 resolution (1562×1562) and upscale.
    # noise2array on 6250×6250 takes 7+ min — at 1/4 it's ~30s.
    # The blur + upscale produces smooth, coherent patches with no visible
    # grid artifacts since the patches are large-scale anyway.
    NOISE_DS = 4  # downsample factor for noise generation
    nH, nW = H // NOISE_DS, W // NOISE_DS
    try:
        import opensimplex as ox
        from scipy.ndimage import zoom as _zoom_noise

        xs = np.arange(nW, dtype=np.float64) / (args.noise_scale / NOISE_DS * args.aniso)
        zs = np.arange(nH, dtype=np.float64) / (args.noise_scale / NOISE_DS)

        ox.seed(88001)
        n_large = ox.noise2array(xs, zs).astype(np.float32)
        xs2 = np.arange(nW, dtype=np.float64) / (args.noise_scale / NOISE_DS * args.aniso * 0.5)
        zs2 = np.arange(nH, dtype=np.float64) / (args.noise_scale / NOISE_DS * 0.5)
        ox.seed(88002)
        n_small = ox.noise2array(xs2, zs2).astype(np.float32)

        combined = 0.7 * n_large + 0.3 * n_small
        combined = gaussian_filter(combined, sigma=2.0)
        combined = (combined - combined.min()) / max(combined.max() - combined.min(), 1e-6)
        # Upscale to full 1:8 resolution with bilinear (no NEAREST — avoids pixelation)
        from PIL import Image as _pil_img
        _noise_pil = _pil_img.fromarray(combined)
        _noise_up = _noise_pil.resize((W, H), _pil_img.BILINEAR)
        aniso_noise = np.array(_noise_up, dtype=np.float32)
    except ImportError:
        rng = np.random.default_rng(88001)
        aniso_noise = rng.random((H, W)).astype(np.float32)

    print(f"  Done in {time.perf_counter()-t0:.1f}s", flush=True)

    # ── 8. Final score and threshold ─────────────────────────────────
    print("Thresholding ...", flush=True)
    final_score = terrain_score * aniso_noise

    # Exclude non-forested biomes (desert, tundra, ocean, etc.)
    # Override codes for non-forested biomes:
    _NO_WINDTHROW = {0, 50, 55, 100, 130, 170, 190, 200}
    forested = land_mask.copy()
    for code in _NO_WINDTHROW:
        forested[override == code] = False

    final_score[~forested] = 0.0

    # Percentile threshold to achieve target coverage
    valid_scores = final_score[forested & (final_score > 0)]
    if valid_scores.size > 100:
        target_frac = args.target_pct / 100.0
        threshold = np.percentile(valid_scores, 100 * (1.0 - target_frac))
        windthrow = (final_score >= threshold) & forested
    else:
        windthrow = np.zeros((H, W), dtype=bool)

    # Morphological closing to merge nearby patches
    if windthrow.any():
        windthrow = binary_closing(windthrow, iterations=1)
        windthrow = windthrow & forested

    # Remove tiny fragments (min 5 px at 1:8 = 40 blocks = ~320 block area)
    if windthrow.any():
        labeled, n_comp = ndlabel(windthrow)
        sizes = np.bincount(labeled.ravel())
        too_small = np.where(sizes < 5)[0]
        if len(too_small) > 0:
            windthrow[np.isin(labeled, too_small)] = False

    n_wt = windthrow.sum()
    n_forest = forested.sum()
    pct = n_wt * 100 / max(n_forest, 1)
    print(f"  Windthrow: {n_wt} px ({pct:.1f}% of forested land)", flush=True)

    result = windthrow.astype(np.uint8)

    # ── 9. Write at 50k ──────────────────────────────────────────────
    out_path = MASKS_DIR / "wind_windthrow.tif"
    print(f"Writing {out_path} at 50k ...", flush=True)
    t0 = time.perf_counter()
    from core.hydrology_precompute import write_upscaled
    write_upscaled(result, out_path, dtype="uint8", scale=SCALE,
                   full_size=FULL_SIZE, chunk_rows=50, interpolation="bilinear")
    print(f"  Written in {time.perf_counter()-t0:.1f}s", flush=True)

    # ── 10. Quick diagnostic preview ─────────────────────────────────
    from PIL import Image, ImageDraw
    # Hillshade
    shade = np.gradient(sy)
    hs = np.clip((np.cos(np.radians(45)) * np.cos(np.arctan(np.hypot(*shade))) +
                   np.sin(np.radians(45)) * (shade[1] * np.sin(np.radians(315)) +
                   shade[0] * np.cos(np.radians(315))) /
                   np.maximum(np.hypot(*shade), 1e-6) + 1) / 2, 0, 1)
    grey = (hs * 180 + 40).astype(np.uint8)
    rgb = np.stack([grey, grey, grey], axis=-1)
    rgb[~land_mask] = [30, 50, 80]
    # Windthrow overlay
    wt_px = windthrow & land_mask
    rgb[wt_px] = (rgb[wt_px] * 0.4 + np.array([220, 160, 50]) * 0.6).astype(np.uint8)

    preview_path = Path("output/windthrow_world.png")
    preview_path.parent.mkdir(exist_ok=True)
    img = Image.fromarray(rgb, "RGB")
    draw = ImageDraw.Draw(img)
    draw.text((10, 10), f"Windthrow {pct:.1f}% of forest | wind={args.wind_dir}deg "
              f"aniso={args.aniso}x tpi_k={args.tpi_kernel}", fill=(255,255,255))
    img.save(str(preview_path))
    print(f"  Preview: {preview_path}")

    print(f"\nTotal: {time.perf_counter()-t_total:.1f}s")


if __name__ == "__main__":
    main()
