"""
rebuild_rock_exposure.py — Precompute rock_exposure.tif globally at 1:8

Alpine/mountain rock exposure based on real treeline ecology:

  In temperate latitudes, treeline sits ~3000-3500m (Y≈200-240 in our
  spline).  Above that, trees can't establish — too cold, too windy, soil
  too thin.  The transition isn't a line but a krummholz belt ~100-300m
  wide where stunted trees give way to alpine meadow then bare rock.

  Key real-world drivers:
    1. Elevation — primary.  Treeline altitude varies by latitude/biome.
    2. Slope — steep rock faces shed soil; exposed even below treeline.
    3. Aspect/wind — windward (west in prevailing westerlies) slopes
       have LOWER treeline (20-30% depression in extreme cases).
       Wind-flagged trees, snow-loading, desiccation.
    4. TPI — isolated ridges/peaks: lower treeline (fully exposed).
       Sheltered valleys: treeline rises (thermal inversion, wind shelter).
    5. Continental position — maritime climates push treeline higher;
       continental climates lower it.

  Real-world treeline references (temperate Northern Hemisphere):
    - Alps:          ~2300m (~7500 ft) → scales to ~Y=195
    - Rockies:       ~3200m (~10500 ft) → ~Y=220
    - Cascades:      ~2100m (~6900 ft) → ~Y=185
    - Appalachians:  ~1800m (~5900 ft) → ~Y=170  (lower due to maritime)
    - Boreal/taiga:  ~1200m (~3900 ft) → ~Y=145  (high latitude = low treeline)
    - Arctic:        ~300m  (~1000 ft) → ~Y=100  (treeline near sea level)
    - Tropical:      ~4000m (~13100 ft) → ~Y=250 (high due to equatorial warmth)

Output: masks/rock_exposure.tif (uint8 0/1 at 50k)

Usage:
    python rebuild_rock_exposure.py [--base-treeline 200] [--wind-dir 270]
"""
from __future__ import annotations
import argparse, gc, json, sys, time
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
    parser.add_argument("--base-treeline", type=float, default=275.0,
                        help="Base treeline MC Y for temperate mid-latitude (default 275)")
    parser.add_argument("--wind-dir", type=float, default=270.0,
                        help="Prevailing wind FROM direction in degrees (270=westerly)")
    parser.add_argument("--wind-depression", type=float, default=0.15,
                        help="Fraction treeline depressed on windward slopes (0.15=15%%)")
    parser.add_argument("--tpi-kernel", type=int, default=40,
                        help="TPI kernel at 1:8 (40 = 320 blocks)")
    parser.add_argument("--tpi-treeline-shift", type=float, default=20.0,
                        help="MC Y treeline depression on ridges")
    parser.add_argument("--slope-threshold", type=float, default=58.0,
                        help="Slope degrees for rock exposure below treeline (real cliffs)")
    parser.add_argument("--slope-blend", type=float, default=10.0,
                        help="Degrees of transition for slope-driven exposure")
    parser.add_argument("--noise-scale", type=float, default=80.0,
                        help="Treeline noise scale at 1:8 (80 = 640 blocks)")
    parser.add_argument("--noise-amplitude", type=float, default=25.0,
                        help="MC Y noise amplitude on treeline (±25 blocks)")
    parser.add_argument("--subalpine-start", type=float, default=45.0,
                        help="MC Y below treeline where thinning begins")
    parser.add_argument("--rock-above", type=float, default=25.0,
                        help="MC Y above treeline where full rock begins")
    # Tight rock mask
    parser.add_argument("--rock-tight-offset", type=float, default=35.0,
                        help="MC Y above treeline where tight rock starts (centre)")
    parser.add_argument("--rock-tight-width", type=float, default=70.0,
                        help="MC Y transition width for tight rock (wide for dither)")
    # Snow caps
    parser.add_argument("--snow-offset", type=float, default=80.0,
                        help="MC Y above treeline where snow starts")
    parser.add_argument("--snow-width", type=float, default=25.0,
                        help="MC Y transition width for snow (subtle)")
    parser.add_argument("--snow-slope-shed", type=float, default=50.0,
                        help="Slope degrees where snow starts shedding")
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
    # Approximate MC Y (float32)
    sy = np.float32(-64.0) + (height_raw * np.float32(512.0 / 65535.0))
    del height_raw; gc.collect()

    # ── 2. Terrain derivatives (float32 throughout) ──────────────────
    print("Computing terrain derivatives ...", flush=True)
    t0 = time.perf_counter()
    gy = np.gradient(sy, axis=0).astype(np.float32)
    gx = np.gradient(sy, axis=1).astype(np.float32)

    # Slope in degrees
    slope_deg = np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)

    # Aspect — only need cos(aspect - wind) for wind modulation
    aspect = np.arctan2(-gx, gy).astype(np.float32)

    # TPI — positive = ridge, negative = valley
    mean_elev = uniform_filter(sy, size=args.tpi_kernel).astype(np.float32)
    tpi = sy - mean_elev
    del mean_elev
    gc.collect()
    print(f"  Done in {time.perf_counter()-t0:.1f}s", flush=True)

    # ── 3. Per-biome treeline ────────────────────────────────────────
    # Based on real-world ecology — treeline altitude varies enormously
    # by climate zone.  Our Y=200 base ≈ 3000m at temperate mid-latitude.
    #
    # Biomes sorted by ecological treeline (cold → warm):
    #   Arctic/frozen:   treeline near sea level (Y≈100-120)
    #   Alpine meadow:   just above krummholz (Y≈140-150)
    #   Boreal/taiga:    ~1500m (Y≈155-170)
    #   Temperate:       ~2500-3500m (Y≈190-220) — our base
    #   Mediterranean:   ~2800m (Y≈210)
    #   Tropical:        ~4000m (Y≈250) — warm base pushes treeline up
    #   Desert:          aridity limit, not temperature — slope-driven only

    _BIOME_TREELINE = {
        # code: absolute treeline Y (-25 from prev, tundra +25 instead)
        55:  200,  # FROZEN_FLATS — raised +25 (tundra exception)
        50:  210,  # ARCTIC_TUNDRA — raised +25 (tundra exception)
        40:  215,  # ALPINE_MEADOW — just above krummholz belt
        35:  230,  # SNOWY_BOREAL_TAIGA — subarctic
        30:  245,  # BOREAL_TAIGA — boreal
        10:  250,  # COASTAL_HEATH — cool maritime
        100: 240,  # KARST_BARRENS — thin soil, lower effective treeline
        150: 255,  # SCRUBBY_HEATHLAND
        140: 260,  # DRY_PINE_BARRENS — continental, moderate
        120: 275,  # MIXED_FOREST — temperate reference
        110: 275,  # BIRCH_FOREST
        115: 270,  # EASTERN_TEMPERATE_COAST
        60:  280,  # TEMPERATE_DECIDUOUS — slightly higher (warmer)
        20:  285,  # TEMPERATE_RAINFOREST — maritime warmth
        240: 275,  # FRESHWATER_FEN
        130: 285,  # CONTINENTAL_STEPPE — warm continental
        90:  295,  # DRY_OAK_SAVANNA — warm
        210: 290,  # DRY_WOODLAND_MAQUIS — Mediterranean
        190: 295,  # DESERT_STEPPE_TRANSITION
        200: 300,  # SEMI_ARID_SHRUBLAND — aridity, not cold
        170: 325,  # SAND_DUNE_DESERT — very high; rock only from slope
        70:  315,  # RAINFOREST_COAST — tropical
        160: 315,  # LUSH_RAINFOREST_COAST
        220: 320,  # TIDAL_JUNGLE_FRINGE
        230: 320,  # MANGROVE_COAST
    }

    print("Building treeline map ...", flush=True)
    t0 = time.perf_counter()
    treeline = np.full((H, W), args.base_treeline, dtype=np.float32)
    for code, tl_y in _BIOME_TREELINE.items():
        bm = override == code
        if bm.any():
            treeline[bm] = np.float32(tl_y)
    del override; gc.collect()
    # Smooth treeline at biome boundaries — per-biome treeline values can
    # differ by 100+ Y blocks (e.g. ALPINE_MEADOW 215 vs SAND_DUNE_DESERT 325).
    # Hard biome boundaries create visible seams in the rock mask. Gaussian
    # blur the treeline so boundaries fade over ~30 source pixels (240 blocks).
    treeline = gaussian_filter(treeline, sigma=15.0).astype(np.float32)

    # ── 4. Wind aspect modulation ────────────────────────────────────
    # Real-world: windward slopes see 10-25% treeline depression.
    # Prevailing westerlies → west-facing slopes are windward.
    # Leeward (east-facing) get slight boost from wind shelter.
    wind_rad = np.float32(np.radians(args.wind_dir))
    windward = np.cos(aspect - wind_rad).astype(np.float32)
    # Keep aspect alive for snow caps computation
    gc.collect()

    # In-place modification to save memory
    # windward>0: depress treeline; windward<0: slight boost
    dep = np.float32(args.wind_depression)
    wind_mod = np.where(
        windward > 0,
        np.float32(1.0) - windward * dep,           # windward: 0.85× treeline
        np.float32(1.0) - windward * dep * np.float32(0.3),  # leeward: 1.045× treeline
    ).astype(np.float32)
    del windward
    treeline *= wind_mod
    del wind_mod; gc.collect()

    # ── 5. TPI modulation ────────────────────────────────────────────
    # Ridges: lower treeline (isolated, exposed, wind-blasted)
    # Valleys: higher treeline (sheltered, thermal inversion, moisture)
    tpi_max = np.percentile(np.abs(tpi[land_mask]), 98) if land_mask.any() else np.float32(1.0)
    tpi_norm = np.clip(tpi / max(float(tpi_max), 0.1), -1.0, 1.0).astype(np.float32)
    del tpi
    treeline -= tpi_norm * np.float32(args.tpi_treeline_shift)
    del tpi_norm; gc.collect()

    # ── 6. Noise for organic treeline boundary ───────────────────────
    # Real treelines are jagged — microclimate, soil pockets, shelter
    # from boulders all create local variation.  Two-octave noise
    # simulates this natural undulation.
    print(f"Treeline noise (scale={args.noise_scale}) ...", flush=True)
    t0_n = time.perf_counter()
    try:
        import opensimplex as ox

        NOISE_DS = 4
        nH, nW = H // NOISE_DS, W // NOISE_DS
        xs = np.arange(nW, dtype=np.float64) / (args.noise_scale / NOISE_DS)
        zs = np.arange(nH, dtype=np.float64) / (args.noise_scale / NOISE_DS)
        ox.seed(99001)
        n1 = ox.noise2array(xs, zs).astype(np.float32)
        xs2 = np.arange(nW, dtype=np.float64) / (args.noise_scale / NOISE_DS * 0.45)
        zs2 = np.arange(nH, dtype=np.float64) / (args.noise_scale / NOISE_DS * 0.45)
        ox.seed(99002)
        n2 = ox.noise2array(xs2, zs2).astype(np.float32)
        combined = np.float32(0.65) * n1 + np.float32(0.35) * n2
        del n1, n2, xs, zs, xs2, zs2
        combined = gaussian_filter(combined, sigma=1.5).astype(np.float32)
        from PIL import Image as _pil
        _n_pil = _pil.fromarray(combined)
        treeline_noise = np.array(
            _n_pil.resize((W, H), _pil.BILINEAR), dtype=np.float32
        )
        del combined, _n_pil
    except ImportError:
        rng = np.random.default_rng(99001)
        treeline_noise = (rng.random((H, W)).astype(np.float32) - 0.5) * 2.0

    treeline += treeline_noise * np.float32(args.noise_amplitude)
    del treeline_noise; gc.collect()
    print(f"  Done in {time.perf_counter()-t0_n:.1f}s", flush=True)

    # ── 7. Elevation gradient (alpine meadow mask) ─────────────────
    # Continuous gradient from 0 (full forest) to 1.0 (bare rock).
    # Drives tree thinning (0-0.3), alpine meadow (0.3-0.7), and the
    # base data for the tight rock / snow masks computed below.
    print("Computing alpine gradient ...", flush=True)
    gradient_range = np.float32(args.subalpine_start + args.rock_above)
    elev_grad = np.clip(
        (sy - (treeline - np.float32(args.subalpine_start))) / gradient_range,
        np.float32(0.0), np.float32(1.0),
    ).astype(np.float32)

    # ── 8. Slope-driven exposure (elevation-scaled) ──────────────────
    SLOPE_LOW  = np.float32(72.0)
    SLOPE_HIGH = np.float32(48.0)
    local_slope_thr = SLOPE_LOW + (SLOPE_HIGH - SLOPE_LOW) * elev_grad
    slope_grad = np.clip(
        (slope_deg - (local_slope_thr - np.float32(args.slope_blend))) / np.float32(args.slope_blend),
        np.float32(0.0), np.float32(1.0),
    ).astype(np.float32)
    del local_slope_thr; gc.collect()

    # ── 9. Combined gradient: rock_exposure.tif ─────────────────────
    slope_contribution = slope_grad * (np.float32(0.3) + np.float32(0.7) * elev_grad)
    rock_score = np.clip(elev_grad + slope_contribution, np.float32(0.0), np.float32(1.0))
    del slope_contribution; gc.collect()
    rock_score[~land_mask] = 0.0
    rock_score = gaussian_filter(rock_score, sigma=1.2).astype(np.float32)
    rock_score[~land_mask] = 0.0
    rock_score = np.clip(rock_score, 0.0, 1.0)

    # ── 10. Stats (alpine) ───────────────────────────────────────────
    land_scores = rock_score[land_mask]
    n_land = int(land_mask.sum())
    print("  rock_exposure.tif (alpine gradient):", flush=True)
    for thr, label in [(0.01, "any thinning"), (0.3, "alpine meadow"), (0.7, "bare rock ref")]:
        n = int((land_scores >= thr).sum())
        print(f"    gradient>={thr} ({label}): {n} px ({100*n/max(n_land,1):.1f}%)", flush=True)

    result_alpine = np.clip(rock_score * 255.0, 0, 255).astype(np.uint8)

    # ── 11. Write rock_exposure.tif ──────────────────────────────────
    from core.hydrology_precompute import write_upscaled
    out_path = MASKS_DIR / "rock_exposure.tif"
    print(f"Writing {out_path} ...", flush=True)
    t0 = time.perf_counter()
    write_upscaled(result_alpine, out_path, dtype="uint8", scale=SCALE,
                   full_size=FULL_SIZE, chunk_rows=50, interpolation="bilinear")
    del result_alpine
    print(f"  Written in {time.perf_counter()-t0:.1f}s", flush=True)

    # ══════════════════════════════════════════════════════════════════
    # ── 12. TIGHT ROCK MASK (rock_exposure_tight.tif) ────────────────
    # Shrunken rock zone — only the highest peaks.  Higher offset above
    # treeline than the alpine gradient.  Slope contributes more heavily
    # near the top so cliff faces always show as rock.
    # ══════════════════════════════════════════════════════════════════
    print(f"\nComputing tight rock mask (offset={args.rock_tight_offset}, width={args.rock_tight_width}) ...", flush=True)
    rock_tight_grad = np.clip(
        (sy - (treeline + np.float32(args.rock_tight_offset))) / np.float32(args.rock_tight_width),
        np.float32(0.0), np.float32(1.0),
    ).astype(np.float32)
    # Slope boost — stronger near treeline+offset (cliff faces are rock)
    slope_boost = slope_grad * (np.float32(0.4) + np.float32(0.6) * rock_tight_grad)
    rock_tight = np.clip(rock_tight_grad + slope_boost, np.float32(0.0), np.float32(1.0))
    del rock_tight_grad, slope_boost; gc.collect()
    rock_tight[~land_mask] = 0.0
    # Wider blur for organic boundary
    rock_tight = gaussian_filter(rock_tight, sigma=1.5).astype(np.float32)
    rock_tight[~land_mask] = 0.0
    rock_tight = np.clip(rock_tight, 0.0, 1.0)

    rt_land = rock_tight[land_mask]
    n_rt = int((rt_land >= 0.4).sum())
    print(f"  tight rock (>=0.4): {n_rt} px ({100*n_rt/max(n_land,1):.1f}%)", flush=True)

    result_rock = np.clip(rock_tight * 255.0, 0, 255).astype(np.uint8)
    del rock_tight; gc.collect()

    out_path_rt = MASKS_DIR / "rock_exposure_tight.tif"
    print(f"Writing {out_path_rt} ...", flush=True)
    t0 = time.perf_counter()
    write_upscaled(result_rock, out_path_rt, dtype="uint8", scale=SCALE,
                   full_size=FULL_SIZE, chunk_rows=50, interpolation="bilinear")
    del result_rock
    print(f"  Written in {time.perf_counter()-t0:.1f}s", flush=True)

    # ══════════════════════════════════════════════════════════════════
    # ── 13. SNOW CAPS MASK (snow_caps.tif) ───────────────────────────
    # Snow on the very tips of peaks.  Tightest zone.
    # Snow line = treeline + snow_offset, modulated by:
    #   - North aspect: snow persists on north faces, melts on south
    #   - Wind: leeward accumulation
    #   - Slope exclusion: steep faces shed snow
    # ══════════════════════════════════════════════════════════════════
    print(f"\nComputing snow caps (offset={args.snow_offset}, width={args.snow_width}) ...", flush=True)

    # North factor: cos(aspect) where aspect=0 is north
    # north_factor > 0 = north-facing (snow persists), < 0 = south-facing
    north_factor = np.cos(aspect).astype(np.float32)

    # Wind leeward: cos(aspect - wind_dir + 180°) — leeward side accumulates
    wind_rad = np.float32(np.radians(args.wind_dir))
    leeward = np.cos(aspect - wind_rad + np.float32(np.pi)).astype(np.float32)

    # Snow line per-pixel: base + aspect modulation + wind modulation
    snow_line = treeline + np.float32(args.snow_offset)
    snow_line -= north_factor * np.float32(15.0)  # north faces: -15Y (snow lower)
    snow_line -= leeward * np.float32(10.0)        # leeward: -10Y (snow accumulates)
    del north_factor, leeward; gc.collect()

    snow_grad = np.clip(
        (sy - snow_line) / np.float32(args.snow_width),
        np.float32(0.0), np.float32(1.0),
    ).astype(np.float32)
    del snow_line; gc.collect()

    # Slope exclusion: steep faces shed snow
    shed_start = np.float32(args.snow_slope_shed)
    snow_slope_factor = np.clip(
        np.float32(1.0) - (slope_deg - shed_start) / np.float32(15.0),
        np.float32(0.0), np.float32(1.0),
    ).astype(np.float32)
    snow_grad *= snow_slope_factor
    del snow_slope_factor; gc.collect()

    snow_grad[~land_mask] = 0.0
    snow_grad = gaussian_filter(snow_grad, sigma=0.8).astype(np.float32)
    snow_grad[~land_mask] = 0.0
    snow_grad = np.clip(snow_grad, 0.0, 1.0)

    sc_land = snow_grad[land_mask]
    n_sc = int((sc_land >= 0.5).sum())
    print(f"  snow caps (>=0.5): {n_sc} px ({100*n_sc/max(n_land,1):.1f}%)", flush=True)

    result_snow = np.clip(snow_grad * 255.0, 0, 255).astype(np.uint8)
    del snow_grad; gc.collect()

    out_path_sc = MASKS_DIR / "snow_caps.tif"
    print(f"Writing {out_path_sc} ...", flush=True)
    t0 = time.perf_counter()
    write_upscaled(result_snow, out_path_sc, dtype="uint8", scale=SCALE,
                   full_size=FULL_SIZE, chunk_rows=50, interpolation="bilinear")
    del result_snow
    print(f"  Written in {time.perf_counter()-t0:.1f}s", flush=True)

    # ── Cleanup shared arrays (keep rock_score/sy/land_mask for preview) ─
    del treeline, slope_deg, slope_grad, elev_grad, aspect, gx, gy
    gc.collect()

    # ── 14. Half-res diagnostic preview ──────────────────────────────
    print("\nRendering preview (half-res) ...", flush=True)
    DS = 2
    sy_ds = sy[::DS, ::DS]
    land_ds = land_mask[::DS, ::DS]
    grad_ds = rock_score[::DS, ::DS]
    del rock_score, sy, land_mask; gc.collect()
    hH, hW = sy_ds.shape

    gy_ds = np.gradient(sy_ds, axis=0).astype(np.float32)
    gx_ds = np.gradient(sy_ds, axis=1).astype(np.float32)
    del sy_ds
    mag = np.maximum(np.hypot(gx_ds, gy_ds), np.float32(1e-6))
    hs = np.clip(
        (gx_ds * np.float32(np.sin(np.radians(315)))
         + gy_ds * np.float32(np.cos(np.radians(315)))) / mag * 0.5 + 0.5,
        0, 1,
    ).astype(np.float32)
    del gx_ds, gy_ds, mag
    grey = (hs * 180 + 40).astype(np.uint8)
    del hs
    rgb = np.stack([grey, grey, grey], axis=-1)
    del grey
    rgb[~land_ds] = [30, 50, 80]
    for i in range(3):
        ch = rgb[:, :, i].astype(np.float32)
        meadow_color = np.array([140, 170, 100], dtype=np.float32)
        rock_color = np.array([180, 165, 145], dtype=np.float32)
        meadow_t = np.clip((grad_ds - 0.1) / 0.4, 0, 1) * land_ds
        rock_t = np.clip((grad_ds - 0.5) / 0.5, 0, 1) * land_ds
        ch = ch * (1 - meadow_t * 0.5) + meadow_color[i] * meadow_t * 0.5
        ch = ch * (1 - rock_t * 0.6) + rock_color[i] * rock_t * 0.6
        rgb[:, :, i] = np.clip(ch, 0, 255).astype(np.uint8)
    del grad_ds

    pct_rock = int((land_scores >= 0.7).sum()) * 100 / max(n_land, 1)
    preview_path = Path("output/rock_exposure_world.png")
    preview_path.parent.mkdir(exist_ok=True)
    from PIL import Image, ImageDraw
    img = Image.fromarray(rgb, "RGB")
    del rgb
    draw = ImageDraw.Draw(img)
    draw.text((10, 10),
              f"Rock gradient | treeline={args.base_treeline} "
              f"rock(>0.7)={pct_rock:.1f}% | wind={args.wind_dir}deg",
              fill=(255, 255, 255))
    img.save(str(preview_path))
    print(f"  Preview: {preview_path} ({hH}x{hW})")

    print(f"\nTotal: {time.perf_counter()-t_total:.1f}s")


if __name__ == "__main__":
    main()
