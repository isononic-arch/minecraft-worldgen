"""synth_eco_masks.py — S101: DEM-DERIVED windthrow / floodplain / clearing
masks for ISLAND bakes (mainland untouched — these files simply don't exist in
masks/, and the tile streamer returns None for missing masks).

WHY: islands had NO `wind_windthrow.tif` and NO `hydro_floodplain.tif`, so
gap==2 and gap==4 never fired there (trees saw neither), and the only clearing
signal was the runtime noise field. This module derives all three from the
island DEM at bake time and writes them with the CANONICAL mainland mask names
so the existing eco_gradients / surface_decorator / schematic_placement
machinery consumes them with zero core changes (plus one new mask,
`clearing_mask.tif`, blended into the Step-6d clearing field by run_pipeline —
mask-presence-gated, mainland unaffected).

CONSUMPTION CONTRACTS (verified in code):
- eco_gradients:843  wind_windthrow  — uint8, streamer /255, `> 0.001` = binary
  ELIGIBILITY; per-biome probability tuples roll actual coverage inside it.
- eco_gradients:546  hydro_floodplain — same binary-corridor contract.
- run_pipeline Step 6d clearing_field — float [0,1], LOW = clearing
  (interior < 0.38, dither band ±0.06). clearing_mask strength s pulls the
  field to (1 - s): s=1 → definite clearing, feathered edges land in the
  dither band. Tree suppression is biome-gated to _CLEARING_BIOMES_TREE
  (temperate/boreal six) — tropical islands keep full canopy in v1.

DESIGN (all computed at 1:4 of the island bbox, bilinear-upsampled — these are
gradient-ish masks, 1:4 matches the mainland's coarser precompute honestly):
- WINDTHROW: windward exposure vs the tradewind (270°, wind FROM the west →
  west-FACING slopes take it) × relative-elevation ramp × a moderate-slope band
  (6°–34°: cliffs belong to rock gap 5, flats don't windthrow) × ridge-convexity
  bonus. Threshold → despeckle.
- FLOODPLAIN: stream cells = top (100−q)% of D8 flow accumulation on land →
  EDT distance + nearest-stream elevation → corridor = gentle slope (<5.5°)
  ∧ within a flow-widened reach ∧ ≤4.5 blocks above the stream ∧ above the
  beach band. Valley pasture, no carving (island rivers are not carved).
- CLEARINGS: mid-elevation flat benches (patch-sized, hash-selected so only
  ~45% of benches open up — deterministic per island via splitmix64 on the
  patch centroid world coords) + wet concave hollows with flow. Feathered
  σ=1.5 px so edges cross the 0.38±0.06 dither band.

Standalone CLI (re-synth without a full re-bake — masks read from the island
dir): py islands/synth_eco_masks.py <island-substring> [--all]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ISL = Path(__file__).resolve().parent
ROOT = ISL.parent
sys.path.insert(0, str(ROOT))

SEA_RAW = 17050
DS = 4                       # synth scale 1:4

# ── knobs (walk-tunable; conservative v1) ───────────────────────────────────
WT_SCORE_THR      = 0.32     # windthrow score threshold
WT_SLOPE_LO, WT_SLOPE_HI = 6.0, 34.0
WT_MIN_AREA_PX    = 8        # despeckle at 1:4 (≈ 8*16 blocks²)
FP_FLOW_PCTL      = 99.0     # stream seed = top 1% land flow
FP_SLOPE_MAX      = 5.5      # deg
FP_HAND_MAX       = 4.5      # blocks above nearest stream
FP_REACH_BASE_PX  = 3        # 1:4 px (= 12 blocks)
FP_REACH_FLOW_PX  = 9        # + up to 36 blocks on big streams
FP_MIN_AREA_PX    = 10
CL_SLOPE_MAX      = 6.0      # deg — benches
CL_MIN_MCY        = 67.0     # above the coast band
CL_PEAK_FRAC      = 0.80     # below 80% of island relief (under treeline-ish)
CL_MIN_AREA_PX    = 25       # ≥ ~20x20 blocks
CL_MAX_AREA_PX    = 4000     # megaplateaus stay forested
CL_KEEP_FRAC      = 0.45     # fraction of candidate benches that open up
CL_HOLLOW_FLOW_PCTL = 92.0
CL_FEATHER_SIGMA  = 1.5      # 1:4 px


def _splitmix01(x: int, z: int, salt: int) -> float:
    # two's-complement mask BEFORE uint64 — island world coords go negative
    M = 0xFFFFFFFFFFFFFFFF
    h = (np.uint64(x & M) * np.uint64(0x9E3779B97F4A7C15)
         + np.uint64(z & M) * np.uint64(0xBF58476D1CE4E5B9)
         + np.uint64(salt * 0x94D049BB133111EB & M))
    h = (h ^ (h >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    h = (h ^ (h >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    h = h ^ (h >> np.uint64(31))
    return float(h) / float(np.iinfo(np.uint64).max)


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


def _upsample(a4: np.ndarray, H: int, W: int) -> np.ndarray:
    from scipy.ndimage import zoom
    out = zoom(a4.astype(np.float32), DS, order=1)
    return out[:H, :W] if out.shape[0] >= H and out.shape[1] >= W else \
        np.pad(out, ((0, H - out.shape[0]), (0, W - out.shape[1])))[:H, :W]


def synth_eco_masks(mdir: Path, world_offset_px=(0, 0), verbose=True) -> dict:
    """Read height/slope/flow from the island mask dir, write
    wind_windthrow.tif + hydro_floodplain.tif + clearing_mask.tif (uint8 0-255).
    Returns coverage stats."""
    import rasterio
    from scipy.ndimage import (distance_transform_edt, gaussian_filter, label,
                               laplace)

    def rd(name):
        with rasterio.open(str(mdir / f"{name}.tif")) as s:
            return s.read(1)

    height = rd("height")
    H, W = height.shape
    h4 = height[::DS, ::DS].astype(np.float32)
    slope4 = rd("slope")[::DS, ::DS].astype(np.float32) / 65535.0 * 45.0
    flow4 = rd("flow")[::DS, ::DS].astype(np.float32) / 65535.0

    # raw -> mcy via the island spline: read from thresholds_island.json
    cfg = json.loads((mdir / "thresholds_island.json").read_text())
    sp = cfg["terrain_spline"]
    mcy4 = np.interp(h4, sp["gaea_in"], sp["mc_y_out"]).astype(np.float32)
    land4 = h4 > SEA_RAW + 40
    if not land4.any():
        return {"windthrow": 0, "floodplain": 0, "clearings": 0}
    peak = float(mcy4[land4].max())

    # ── WINDTHROW (gap 2 eligibility) ────────────────────────────────────
    hs = gaussian_filter(mcy4, 2.0)
    gz, gx = np.gradient(hs)                      # per 1:4 px
    mag = np.hypot(gx, gz) + 1e-6
    windward = np.clip(gx / mag, 0.0, 1.0)        # west-facing: rises eastward
    h_rel = np.clip((mcy4 - 75.0) / max(peak * 0.75 - 75.0, 30.0), 0.0, 1.0)
    s_band = (np.clip((slope4 - WT_SLOPE_LO) / 6.0, 0, 1)
              * np.clip((WT_SLOPE_HI - slope4) / 8.0, 0, 1))
    ridge = np.clip(-laplace(hs) / 1.5, 0.0, 1.2)
    score = (0.25 + 0.75 * windward) * (h_rel ** 0.8) * s_band * (0.6 + 0.6 * ridge)
    wt = _despeckle(land4 & (score > WT_SCORE_THR), WT_MIN_AREA_PX)

    # ── FLOODPLAIN (gap 4 corridor) ──────────────────────────────────────
    # RELIEF GATE: flat coral platforms/atolls (relief < 25 blocks) have no
    # real drainage — percentile seeding there picks flow NOISE and blankets
    # a third of the island in gap 4 (measured: La Tortuga 36%). No relief,
    # no floodplain. Absolute flow floor kills residual noise seeds elsewhere.
    fl_land = flow4[land4]
    fp = np.zeros_like(land4)
    if fl_land.size and (peak - 63.0) >= 25.0:
        thr = max(np.percentile(fl_land, FP_FLOW_PCTL), 0.015)
        streams = land4 & (flow4 >= thr) & (mcy4 > 63.5)
        if streams.any():
            dist, (iy, ix) = distance_transform_edt(~streams, return_indices=True)
            stream_mcy = mcy4[iy, ix]
            stream_flow = flow4[iy, ix]
            fmax = float(stream_flow.max()) or 1.0
            reach = FP_REACH_BASE_PX + FP_REACH_FLOW_PX * np.clip(
                stream_flow / fmax * 1.5, 0.0, 1.0)
            fp = (land4 & (slope4 < FP_SLOPE_MAX) & (dist <= reach)
                  & (mcy4 - stream_mcy <= FP_HAND_MAX) & (mcy4 > 64.2))
            fp = _despeckle(fp, FP_MIN_AREA_PX)

    # ── CLEARINGS (field-pull mask) ──────────────────────────────────────
    ox, oz = int(world_offset_px[0]), int(world_offset_px[1])
    bench = (land4 & (slope4 < CL_SLOPE_MAX) & (mcy4 > CL_MIN_MCY)
             & (mcy4 < 63.0 + CL_PEAK_FRAC * (peak - 63.0)) & ~fp & ~wt)
    lab, n = label(bench)
    cl = np.zeros_like(bench)
    if n:
        counts = np.bincount(lab.ravel())
        for li in range(1, n + 1):
            if not (CL_MIN_AREA_PX <= counts[li] <= CL_MAX_AREA_PX):
                continue
            ys, xs = np.where(lab == li)
            cy, cx = int(ys.mean()), int(xs.mean())
            if _splitmix01(ox + cx * DS, oz + cy * DS, 0xC1EA71) < CL_KEEP_FRAC:
                cl[lab == li] = True
    hollow = (land4 & (slope4 < 8.0) & (laplace(hs) > 0.35)
              & (flow4 >= np.percentile(fl_land, CL_HOLLOW_FLOW_PCTL))
              & (mcy4 > CL_MIN_MCY) & ~fp)
    cl = cl | _despeckle(hollow, WT_MIN_AREA_PX)
    cl_soft = np.clip(gaussian_filter(cl.astype(np.float32), CL_FEATHER_SIGMA) * 1.3, 0, 1)

    # ── write at full res ────────────────────────────────────────────────
    from islands.render_islands import _wtif
    _wtif(mdir / "wind_windthrow.tif",
          (np.clip(_upsample(wt.astype(np.float32), H, W), 0, 1) > 0.5).astype(np.uint8) * 255)
    _wtif(mdir / "hydro_floodplain.tif",
          (np.clip(_upsample(fp.astype(np.float32), H, W), 0, 1) > 0.5).astype(np.uint8) * 255)
    _wtif(mdir / "clearing_mask.tif",
          (np.clip(_upsample(cl_soft, H, W), 0, 1) * 255).astype(np.uint8))

    lp = int(land4.sum())
    stats = {"windthrow": int(wt.sum()), "floodplain": int(fp.sum()),
             "clearings": int(cl.sum()), "land_px_1_4": lp,
             "wt_pct": 100.0 * wt.sum() / lp, "fp_pct": 100.0 * fp.sum() / lp,
             "cl_pct": 100.0 * cl.sum() / lp}
    if verbose:
        print(f"[eco-synth] {mdir.name}: windthrow {stats['wt_pct']:.1f}% "
              f"floodplain {stats['fp_pct']:.1f}% clearings {stats['cl_pct']:.1f}% of land",
              flush=True)
    return stats


def main():
    import re
    sel = sys.argv[1] if len(sys.argv) > 1 else "--all"
    layout = json.loads((ISL / "layout.json").read_text())["islands"]
    def safe(n): return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")
    for e in layout:
        name = safe(e["name"])
        if sel != "--all" and sel not in name:
            continue
        mdir = ISL / "masks_islands" / name
        if (mdir / "height.tif").exists():
            synth_eco_masks(mdir, e["world_offset_px"])


if __name__ == "__main__":
    main()
