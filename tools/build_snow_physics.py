"""
build_snow_physics.py — S89 physics-based snow mask (Winstral-style wind redistribution).

Replaces the altitude/dusting snow line with a terrain-physics potential that
sheds snow off steep rock, fills concave bowls/gullies, strips wind-scoured
ridge crests, and drifts onto lee slopes:

    base       = altitude fade over [snow_lo_y, snow_hi_y] MC-Y (the snow line)
    slope_gate = 1 - smoothstep(shed_lo, shed_hi, slope_deg)   (steep faces shed)
    curv       = 1 + a * concavity_signed                      (fill bowls / strip ridges)
    lee        = 1 + b * faces_downwind                        (SLIGHT ESE lee deposit)
    shelter    = c * max(Sx, 0)                                (drift tails behind ridges)
    snow = clamp(base * slope_gate * curv * lee + shelter, 0, 1)

Then the continuous potential is upscaled to 50k and thresholded with blue-noise
dither (core.upscale) -> masks/snow_gap_physics.tif, written A/B ALONGSIDE the
existing Gaea snow_gap.tif (the default until validated).

Biome exemptions (SNOWY_BOREAL_TAIGA / FROZEN_FLATS keep snow_carpet) stay in
surface_decorator's gap==7 consumer — the mask is just the alpine snow potential.

Shares slope / wind conventions + the verified _wind_factor with
build_terrain_derived (single world wind: TRAVELS ESE ~112, SOURCE WNW ~292).
Run at --scale 8 for Sx affordability.

Usage:
  py tools/build_snow_physics.py --scale 8
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from rasterio.enums import Resampling
from scipy.ndimage import gaussian_filter, uniform_filter

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.build_terrain_derived import (  # noqa: E402
    WORLD_50K, read_at_scale, height_norm_to_mc_y,
    slope_deg_from_surface_y, _wind_factor,
)


def _smoothstep(lo: float, hi: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - lo) / max(1e-6, hi - lo), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def compute_sx(surface_y: np.ndarray, scale: int, source_deg: float,
               radius_blocks: float, fan_rays: int, fan_half_deg: float) -> np.ndarray:
    """Winstral Sx upwind-shelter parameter (radians, positive = sheltered).

    For each pixel, the MAX upwind slope angle within `radius_blocks`, averaged
    over a fan of `fan_rays` bearings spanning +/-`fan_half_deg` around the wind
    SOURCE bearing. Vectorized as directional shifted-max accumulation (np.roll
    along each ray) — NOT a per-pixel Python march. Edge wrap (<1% area) accepted,
    same as build_terrain_derived's _8direction_walk."""
    H, W = surface_y.shape
    sy = surface_y.astype(np.float32)
    radius_px = max(1, int(round(radius_blocks / scale)))
    bearings = np.linspace(source_deg - fan_half_deg,
                           source_deg + fan_half_deg, max(1, fan_rays))
    sx = np.zeros((H, W), dtype=np.float32)
    for b in bearings:
        rb = np.radians(b)
        # upwind unit (east, north) = (sin b, cos b) -> image (drow=-north, dcol=east)
        d_e = float(np.sin(rb))
        d_n = float(np.cos(rb))
        ray_max = np.zeros((H, W), dtype=np.float32)
        for k in range(1, radius_px + 1):
            # Read the UPWIND cell at (r - d_n*k, c + d_e*k) [north=-row,east=+col].
            # np.roll(a, s)[i] = a[i-s], so the roll shift is the NEGATIVE of the
            # upwind step: shift_row = d_n*k, shift_col = -d_e*k.
            dr = int(round(d_n * k))
            dc = int(round(-d_e * k))
            if dr == 0 and dc == 0:
                continue
            shifted = np.roll(np.roll(sy, dr, axis=0), dc, axis=1)
            dist_blocks = max(1e-3, k * scale)
            ang = np.arctan2(shifted - sy, dist_blocks)  # +ve if upwind higher
            np.maximum(ray_max, ang, out=ray_max)
        sx += ray_max
    sx /= float(len(bearings))
    return sx


def build_snow_potential(surface_y: np.ndarray, slope_deg: np.ndarray,
                         scale: int, cfg_sp: dict) -> np.ndarray:
    """Return the continuous snow potential in [0,1] at working scale."""
    snow_lo = float(cfg_sp.get("snow_lo_y", 430.0))
    snow_hi = float(cfg_sp.get("snow_hi_y", 475.0))
    shed_lo = float(cfg_sp.get("shed_lo_deg", 33.0))
    shed_hi = float(cfg_sp.get("shed_hi_deg", 42.0))
    a = float(cfg_sp.get("curvature_coeff", 0.35))
    b = float(cfg_sp.get("leeward_coeff", 0.12))
    c = float(cfg_sp.get("shelter_coeff", 0.30))
    wind_travel = float(cfg_sp.get("wind_travel_deg", 112.0))
    wind_source = float(cfg_sp.get("wind_source_deg", 292.0))

    syf = surface_y.astype(np.float32)

    # 1. altitude base (the snow line, feathered)
    base = _smoothstep(snow_lo, snow_hi, syf)

    # 2. slope gate — steep faces shed
    slope_gate = (1.0 - _smoothstep(shed_lo, shed_hi, slope_deg)).astype(np.float32)

    # 3. curvature — fill bowls (+), strip ridges (-). Signed, normalized.
    nbr_mean = (uniform_filter(syf, size=3) * 9.0 - syf) / 8.0
    conc = nbr_mean - syf
    cmax = max(float(np.abs(conc).max()), 1e-6)
    conc_n = np.clip(conc / cmax, -1.0, 1.0).astype(np.float32)
    curv = (1.0 + a * conc_n).astype(np.float32)

    # 4. leeward — SLIGHT. faces_downwind = faces toward the travel bearing.
    lee = _wind_factor(surface_y, wind_travel)  # 1 where the slope faces ESE (lee)
    lee_factor = (1.0 + b * lee).astype(np.float32)

    # 5. Sx upwind shelter -> additive drift tails on the lee of ridges
    if c > 0.0:
        sx = compute_sx(
            surface_y, scale, wind_source,
            radius_blocks=float(cfg_sp.get("sx_search_blocks", 700.0)),
            fan_rays=int(cfg_sp.get("sx_fan_rays", 5)),
            fan_half_deg=float(cfg_sp.get("sx_fan_half_deg", 12.0)),
        )
        shelter = c * np.clip(sx, 0.0, None)
    else:
        shelter = np.zeros_like(syf)

    snow = np.clip(base * slope_gate * curv * lee_factor + shelter, 0.0, 1.0)
    # light feather so the threshold band isn't a hard contour
    snow = gaussian_filter(snow.astype(np.float32), sigma=1.0)
    return np.clip(snow, 0.0, 1.0).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--masks", default="masks/")
    ap.add_argument("--config", default="config/thresholds.json")
    ap.add_argument("--scale", type=int, default=8,
                    help="Working scale denominator (8 = 6250x6250; Sx affordable).")
    ap.add_argument("--out", default="snow_gap_physics",
                    help="Output mask name (A/B; default snow_gap_physics).")
    args = ap.parse_args()

    masks_dir = Path(args.masks)
    cfg_path = Path(args.config)
    if not masks_dir.is_dir():
        print(f"ERROR: masks dir not found: {masks_dir}", file=sys.stderr)
        return 2
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg_sp = cfg.get("snow_physics", {})
    if not cfg_sp:
        print("ERROR: config has no snow_physics block", file=sys.stderr)
        return 2

    scale = int(args.scale)
    ds_size = WORLD_50K // scale
    print(f"snow_physics at 1:{scale} ({ds_size}x{ds_size})")

    t0 = time.perf_counter()
    h_raw = read_at_scale(masks_dir, "height", ds_size, Resampling.average)
    h_norm = h_raw.astype(np.float32) / (
        65535.0 if h_raw.dtype == np.uint16 else 1.0)
    surface_y = height_norm_to_mc_y(h_norm)
    slope_deg = slope_deg_from_surface_y(surface_y, scale=scale)
    print(f"  surface_y {int(surface_y.min())}..{int(surface_y.max())}, "
          f"slope {float(slope_deg.min()):.1f}..{float(slope_deg.max()):.1f}deg "
          f"[{time.perf_counter()-t0:.1f}s]")

    t = time.perf_counter()
    snow = build_snow_potential(surface_y, slope_deg, scale, cfg_sp)
    print(f"  snow potential: >0 frac {float((snow>0).mean()):.3f}, "
          f">0.5 frac {float((snow>0.5).mean()):.3f} [{time.perf_counter()-t:.1f}s]")

    from core.upscale import upscale_continuous_then_threshold_dither
    out_path = masks_dir / f"{args.out}.tif"
    t = time.perf_counter()
    upscale_continuous_then_threshold_dither(
        snow, out_path,
        threshold=float(cfg_sp.get("threshold", 0.5)),
        dither_width=float(cfg_sp.get("dither_width", 0.15)),
        target_size=WORLD_50K,
        interpolation="catmull_rom",
    )
    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"  -> {out_path.name}  file={size_mb:.1f} MB "
          f"[{time.perf_counter()-t:.1f}s]")
    print(f"Total {time.perf_counter()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
