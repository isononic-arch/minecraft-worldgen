"""build_mainland_clearing_mask.py — terrain-derived forest-clearing mask for
the MAINLAND 50k world.

WHY: mainland forest clearings today are pure simplex-noise blobs
(core/meadow_clearing_field.py) decoupled from terrain — they land on cliffs
and ridges where clearings never form. The consumption plumbing already exists
and is mask-presence-gated (mainland ships no clearing_mask.tif -> inert):
  - core/tile_streamer.py:MASK_NAMES lists "clearing_mask" (streamer /255 -> [0,1])
  - run_pipeline.py Step-6d: clearing_field = min(clearing_field, 1 - clearing_mask)
  - core/surface_decorator.py: re-asserts grass in the mask interior (>0.5)
Building this ONE mask activates terrain-grounded clearings, zero core changes.

CONTRACT: clearing_mask.tif is uint8 [0,255]; streamer normalizes /255 to [0,1].
HIGH value = clearing (pulls clearing_field LOW -> clearing interior). Feathered
edges land in the run_pipeline dither band (interior 0.38 +/- 0.06).

ALGORITHM (adapted from islands/synth_eco_masks.py CLEARING logic, at mainland
scale, computed at 1:8 = 6250x6250 to stay under the ~7.4GB memory budget):
  - benches: gentle slope AND mid-elevation MCY band AND forest-biome, then
    hash-keep ~45% of connected bench patches (splitmix64 on the patch centroid
    world coords), area-gated 25..4000 px @1:8.
  - wet concave hollows: laplacian>thr AND high-flow-percentile AND forest-biome.
  - gaussian feather sigma~1.5 @1:8, then bilinear-upsample to 50k as uint8.

Mainland forest eligibility zones (core/biome_assignment.py:OVERRIDE_BIOME_MAP):
  20 TEMPERATE_RAINFOREST, 30 BOREAL_TAIGA, 60 TEMPERATE_DECIDUOUS,
  80 RIPARIAN_WOODLAND, 110 BIRCH_FOREST, 120 MIXED_FOREST.

Does NOT touch windthrow/floodplain — the mainland has real wind_windthrow.tif +
hydro_floodplain.tif from rebuild_*.py. This tool only writes clearing_mask.tif.

Run:  py tools/build_mainland_clearing_mask.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MASKS = ROOT / "masks"
VAL = ROOT / "islands" / "_val"

SEA_RAW = 17050
DS = 8                        # mainland precompute scale 1:8 (6250x6250)

FOREST_ZONES = (20, 30, 60, 80, 110, 120)

# ── knobs ────────────────────────────────────────────────────────────────────
# Island values (islands/synth_eco_masks.py) in brackets; retuned for mainland
# where the Gaea slope distribution is far steeper (forest slope p50 ~= 22 deg;
# only ~3.9% of forest land is < 6 deg). CL_SLOPE_MAX 6 -> 10 to build a workable
# bench pool while still excluding the 24-deg-median cliffs; verified below.
CL_SLOPE_MAX        = 15.0    # [6.0] deg — benches (mainland is much steeper;
                              # forest slope p50 ~= 22 deg, so 6 gave only 1.9%
                              # coverage; 15 lands ~3-5% while excluding the
                              # 24-deg-median cliffs)
CL_MIN_MCY          = 67.0    # [67] above the coast band
CL_PEAK_FRAC        = 0.80    # [0.80] below 80% of relief (under treeline-ish)
CL_MIN_AREA_PX      = 16      # [25] rugged mainland benches are smaller/broken;
                              # 16 px @1:8 ~= 16x64 blocks still despeckles noise
CL_MAX_AREA_PX      = 4000    # [4000] megaplateaus stay forested
CL_KEEP_FRAC        = 0.55    # [0.45] fraction of candidate benches that open —
                              # bumped from island 0.45 to lift forest coverage
                              # into the 3-8% target on the sparser mainland pool
CL_HOLLOW_SLOPE_MAX = 14.0    # [8.0] hollows (also retuned for steep mainland)
CL_HOLLOW_LAP_THR   = 0.08    # [0.35] concavity — at 1:8 the laplacian of the
                              # smoothed MCY grid is much flatter than the
                              # island 1:4 grid, so 0.35 caught nothing (0 px)
CL_HOLLOW_FLOW_PCTL = 92.0    # [92] wet
CL_HOLLOW_MIN_AREA  = 8       # despeckle @1:8
CL_FEATHER_SIGMA    = 1.5     # [1.5] 1:8 px


def _splitmix01(x: int, z: int, salt: int) -> float:
    """Deterministic [0,1) hash of world coords. Mainland coords are all >= 0,
    but mask before uint64 anyway (matches the island helper)."""
    M = 0xFFFFFFFFFFFFFFFF
    with np.errstate(over="ignore"):  # uint64 wraparound is the intended mixing
        h = (np.uint64(x & M) * np.uint64(0x9E3779B97F4A7C15)
             + np.uint64(z & M) * np.uint64(0xBF58476D1CE4E5B9)
             + np.uint64(salt * 0x94D049BB133111EB & M))
        h = (h ^ (h >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        h = (h ^ (h >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        h = h ^ (h >> np.uint64(31))
    return float(h) / float(np.iinfo(np.uint64).max)


def _rd8(name: str) -> np.ndarray:
    """Strided 1:8 read. Explicit strided slice (not rasterio out_shape
    decimation, which misreports on tiled TIFFs)."""
    import rasterio
    with rasterio.open(str(MASKS / f"{name}.tif")) as s:
        return s.read(1)[::DS, ::DS]


def _despeckle(mask: np.ndarray, min_area: int, max_area: int | None = None):
    from scipy.ndimage import label
    lab, n = label(mask)
    if n == 0:
        return mask
    counts = np.bincount(lab.ravel())
    keep = counts >= min_area
    if max_area is not None:
        keep &= counts <= max_area
    keep[0] = False
    return keep[lab]


def build(verbose: bool = True) -> dict:
    from scipy.ndimage import gaussian_filter, label, laplace

    # ── 1:8 inputs ───────────────────────────────────────────────────────────
    h4 = _rd8("height").astype(np.float32)
    H8, W8 = h4.shape
    slope4 = _rd8("slope").astype(np.float32) / 65535.0 * 45.0   # approx degrees
    flow4 = _rd8("flow").astype(np.float32) / 65535.0
    ov4 = _rd8("override")

    sp = json.loads((ROOT / "config" / "thresholds.json").read_text())["terrain_spline"]
    mcy4 = np.interp(h4, sp["gaea_in"], sp["mc_y_out"]).astype(np.float32)

    land4 = h4 > SEA_RAW + 40
    forest4 = np.isin(ov4, FOREST_ZONES) & land4
    if not forest4.any():
        raise SystemExit("no forest-biome land found — aborting")
    peak = float(mcy4[land4].max())
    forest_land_px = int(forest4.sum())

    if verbose:
        print(f"[clearing] 1:8 grid {H8}x{W8}  land {100*land4.mean():.1f}%  "
              f"forest {100*forest4.sum()/land4.sum():.1f}% of land "
              f"({forest_land_px} px @1:8)", flush=True)
        print(f"[clearing] forest slope deg: p50 {np.percentile(slope4[forest4],50):.1f}"
              f"  frac<{CL_SLOPE_MAX:.0f}deg {100*(slope4[forest4]<CL_SLOPE_MAX).mean():.1f}%"
              f"  |  peak MCY {peak:.0f}", flush=True)

    # ── benches: gentle slope, mid-elevation, forest, hash-kept per patch ──────
    band_hi = 63.0 + CL_PEAK_FRAC * (peak - 63.0)
    bench = (forest4 & (slope4 < CL_SLOPE_MAX) & (mcy4 > CL_MIN_MCY)
             & (mcy4 < band_hi))
    lab, n = label(bench)
    cl = np.zeros_like(bench)
    kept_patches = 0
    if n:
        counts = np.bincount(lab.ravel())
        # vectorized centroid per label
        idx = np.arange(lab.size)
        ys = idx // W8
        xs = idx % W8
        flat = lab.ravel()
        sum_y = np.bincount(flat, weights=ys, minlength=n + 1)
        sum_x = np.bincount(flat, weights=xs, minlength=n + 1)
        for li in range(1, n + 1):
            c = counts[li]
            if not (CL_MIN_AREA_PX <= c <= CL_MAX_AREA_PX):
                continue
            cy = int(sum_y[li] / c)
            cx = int(sum_x[li] / c)
            # world coords = 1:8 index * DS
            if _splitmix01(cx * DS, cy * DS, 0xC1EA71) < CL_KEEP_FRAC:
                cl[lab == li] = True
                kept_patches += 1

    # ── wet concave hollows ────────────────────────────────────────────────────
    hs = gaussian_filter(mcy4, 1.0)  # island used 2.0 @1:4; lighter @1:8 to keep
                                     # concavity signal (laplacian) from washing out
    fl_forest = flow4[forest4]
    flow_thr = np.percentile(fl_forest, CL_HOLLOW_FLOW_PCTL)
    hollow = (forest4 & (slope4 < CL_HOLLOW_SLOPE_MAX)
              & (laplace(hs) > CL_HOLLOW_LAP_THR)
              & (flow4 >= flow_thr) & (mcy4 > CL_MIN_MCY))
    hollow = _despeckle(hollow, CL_HOLLOW_MIN_AREA)
    cl = cl | hollow

    # island used *1.3; mainland benches are smaller/more broken so a 1.3 boost
    # let a sigma=1.5 feather wash small (16-40 px) patches below 0.5 (interior
    # lost). 1.6 keeps small-patch cores solid while still feathering edges into
    # the run_pipeline 0.38+/-0.06 dither band.
    cl_soft = np.clip(
        gaussian_filter(cl.astype(np.float32), CL_FEATHER_SIGMA) * 1.6, 0.0, 1.0)
    # re-mask to land so the feather never bleeds clearing into ocean; the
    # surface_decorator grass re-assertion is already forest-biome-gated, so a
    # little cross-biome feather on land is harmless, but ocean bleed is not.
    cl_soft[~land4] = 0.0

    # ── validation on the 1:8 grid ─────────────────────────────────────────────
    cl_bin = cl_soft > 0.5
    cov_forest = 100.0 * cl_bin[forest4].sum() / forest_land_px
    cov_all_land = 100.0 * cl_bin[land4].sum() / int(land4.sum())
    clearing_slope = slope4[cl_bin].mean() if cl_bin.any() else 0.0
    forest_slope = slope4[forest4].mean()
    # spot-check: clearing outside forest zones must be ~0
    nonforest_clearing = int((cl_bin & land4 & ~forest4).sum())

    stats = {
        "forest_land_px_1_8": forest_land_px,
        "kept_bench_patches": kept_patches,
        "hollow_px_1_8": int(hollow.sum()),
        "clearing_px_1_8": int(cl_bin.sum()),
        "coverage_pct_of_forest_land": round(cov_forest, 2),
        "coverage_pct_of_all_land": round(cov_all_land, 2),
        "mean_slope_clearing_deg": round(float(clearing_slope), 2),
        "mean_slope_forest_deg": round(float(forest_slope), 2),
        "nonforest_clearing_px": nonforest_clearing,
    }

    # per-forest-biome coverage
    per_biome = {}
    for z in FOREST_ZONES:
        m = (ov4 == z) & land4
        if m.sum():
            per_biome[z] = round(100.0 * (cl_bin & m).sum() / int(m.sum()), 2)
    stats["coverage_pct_by_zone"] = per_biome

    if verbose:
        print(f"[clearing] kept {kept_patches} bench patches, "
              f"{int(hollow.sum())} hollow px @1:8", flush=True)
        print(f"[clearing] COVERAGE  forest-land {cov_forest:.2f}%  "
              f"all-land {cov_all_land:.2f}%", flush=True)
        print(f"[clearing] per-forest-zone %: {per_biome}", flush=True)
        print(f"[clearing] SPOT-CHECK  mean slope: clearing {clearing_slope:.2f}deg"
              f"  vs forest {forest_slope:.2f}deg   "
              f"non-forest clearing px: {nonforest_clearing}", flush=True)

    # ── preview PNG (before the big upsample so it's cheap) ────────────────────
    _write_preview(land4, forest4, cl_bin, ov4)

    # ── memory-safe upsample -> 50k uint8 + windowed write ─────────────────────
    _write_full_res(cl_soft, verbose=verbose)

    return stats


def _write_preview(land4, forest4, cl_bin, ov4):
    """Downsample the 1:8 grid to ~2000px and paint a diagnostic PNG."""
    from PIL import Image
    H8, W8 = land4.shape
    step = max(1, H8 // 2000)
    l = land4[::step, ::step]
    f = forest4[::step, ::step]
    c = cl_bin[::step, ::step]
    img = np.zeros((*l.shape, 3), dtype=np.uint8)
    img[~l] = (40, 70, 140)         # ocean blue
    img[l & ~f] = (35, 35, 35)      # non-forest land dark
    img[f] = (120, 120, 120)        # forest gray
    img[c] = (60, 230, 60)          # clearings bright green
    VAL.mkdir(parents=True, exist_ok=True)
    out = VAL / "mainland_clearing_preview.png"
    Image.fromarray(img).save(str(out))
    print(f"[clearing] preview -> {out}  ({img.shape[1]}x{img.shape[0]})", flush=True)


def _write_full_res(cl_soft8: np.ndarray, verbose: bool = True):
    """Upsample the 1:8 clearing strength to 50000x50000 uint8 and write with
    the height.tif profile. Memory-safe: build the 50k uint8 array (2.5 GB) via
    per-row-block nearest-ish bilinear from the tiny 6250 float source, and write
    it in windowed blocks so we never hold a 50k float32 (10 GB, would OOM)."""
    import rasterio
    from rasterio.windows import Window

    with rasterio.open(str(MASKS / "height.tif")) as ref:
        prof = ref.profile.copy()
    H8, W8 = cl_soft8.shape
    H, W = 50000, 50000
    prof.update(dtype="uint8", count=1, nodata=0, compress="deflate",
                tiled=True, blockxsize=512, blockysize=512)

    # precompute bilinear column mapping once (shared across all rows)
    xs = np.linspace(0, W8 - 1, W, dtype=np.float32)
    x0 = np.floor(xs).astype(np.int32)
    x1 = np.minimum(x0 + 1, W8 - 1)
    wx = (xs - x0).astype(np.float32)

    BLK = 1024   # write 1024 rows at a time -> 1024*50000 uint8 = 50 MB/block
    src = cl_soft8.astype(np.float32)
    with rasterio.open(str(MASKS / "clearing_mask.tif"), "w", **prof) as dst:
        for y0 in range(0, H, BLK):
            y1 = min(y0 + BLK, H)
            ys = np.linspace(0, H8 - 1, H, dtype=np.float32)[y0:y1]
            sy0 = np.floor(ys).astype(np.int32)
            sy1 = np.minimum(sy0 + 1, H8 - 1)
            wy = (ys - sy0).astype(np.float32)[:, None]
            # gather rows: (nrows, W8) via bilinear in x, then blend in y
            top = src[sy0][:, x0] * (1 - wx) + src[sy0][:, x1] * wx
            bot = src[sy1][:, x0] * (1 - wx) + src[sy1][:, x1] * wx
            block = top * (1 - wy) + bot * wy
            block = np.clip(block * 255.0, 0, 255).astype(np.uint8)
            dst.write(block, 1, window=Window(0, y0, W, y1 - y0))
            if verbose and (y0 // BLK) % 8 == 0:
                print(f"[clearing] wrote rows {y0}..{y1}", flush=True)
    if verbose:
        print(f"[clearing] wrote {MASKS / 'clearing_mask.tif'}", flush=True)


def _verify_output():
    import rasterio
    with rasterio.open(str(MASKS / "clearing_mask.tif")) as s:
        assert s.width == 50000 and s.height == 50000, (s.width, s.height)
        assert s.dtypes[0] == "uint8", s.dtypes
        # sample a mid-world land window (the top rows are all ocean edge)
        w = s.read(1, window=rasterio.windows.Window(20000, 20000, 10000, 10000))
        nz = int((w > 0).sum())
    assert nz > 0, "clearing_mask read back all-zero over mid-world land!"
    print(f"[clearing] VERIFY: 50000x50000 uint8, mid-world 10k window nonzero "
          f"px={nz}, max={int(w.max())}", flush=True)


def main():
    stats = build(verbose=True)
    _verify_output()
    print("\n=== SUMMARY ===")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
