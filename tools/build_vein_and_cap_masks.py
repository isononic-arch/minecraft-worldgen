"""
build_vein_and_cap_masks.py — S88 walk #10/11: build vein_field.tif + rebuild
cliff_cap.tif + bake varnish_field.tif.

Outputs (50k uint8):
  masks/vein_field.tif
  masks/cliff_cap.tif
  masks/varnish_field.tif

Mask design:

VEIN_FIELD = (laplacian_magnitude * w_lap) + (fault_zero_crossings * w_fault),
             slope-gated (>= slope_min_deg), uint8 0-255

CLIFF_CAP  = max(edge_cap, peak_cap) with gaussian fade band
  edge_cap: flat shoulders adjacent to uphill cliff_face (slope >= cliff_min)
            with search radius search_blocks
  peak_cap: convex peaks (negative laplacian) AT high elevation
  fade_band: gaussian falloff at the outer boundary of (edge_cap | peak_cap)

Compute strategy: read height.tif at 1:4 (12500x12500), compute everything at
1:4 with float32, then upsample to 50k via NEAREST chunked write.

Args:  none (reads masks/height.tif, masks/slope.tif if available)
"""
from __future__ import annotations
import sys, time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.ndimage import (
    gaussian_filter, sobel, binary_dilation, minimum_filter,
    maximum_filter, distance_transform_edt,
)
import opensimplex

H_50K = 50000
SCALE = 4  # build at 1:4 = 12500x12500
H_BUILD = H_50K // SCALE  # 12500

# ─── VEIN_FIELD params ──────────────────────────────────────────────────
VEIN_LAP_SIGMA = 1.5            # gaussian pre-smooth before laplacian
VEIN_LAP_WEIGHT = 0.5           # contribution of |laplacian| to final intensity
VEIN_FAULT_WEIGHT = 0.5         # contribution of fault zero-crossings
# Walk #10.2: TWO-scale fault network for branching (main + cross-faults)
VEIN_FAULT_MAIN_SCALE = 240     # main fault period in world blocks
VEIN_FAULT_MAIN_WIDTH = 0.10    # |noise| < width for mainline traces
VEIN_FAULT_BRANCH_SCALE = 80    # cross-fault/branch period (3x finer)
VEIN_FAULT_BRANCH_WIDTH = 0.06  # narrower → thinner branches
VEIN_FAULT_BRANCH_PROXIMITY = 8 # branches only spawn within N pixels of mainlines
VEIN_SLOPE_MIN_DEG = 32.0       # gate: only on >= 32° slopes (matches strata)

# ─── VARNISH_FIELD params ───────────────────────────────────────────────
# Slope-fade range (intensity ramps from 0 at slope_min to 255 at slope_max).
VARN_SLOPE_MIN_DEG = 32.0
VARN_SLOPE_MAX_DEG = 60.0
# Drip flow band — between dry (no water) and wash (active channel).
VARN_FLOW_DRIP_MIN = 0.0001
VARN_FLOW_DRIP_MAX = 0.001
# Read flow.tif at full 50k then downsample to 1:4 by max-pool (preserves
# drip-path signal; min-pool would over-erase narrow streaks).

# ─── CLIFF_CAP params ───────────────────────────────────────────────────
CAP_FLAT_MAX_DEG = 28.0         # pixel must be flatter than this to qualify
CAP_CLIFF_MIN_DEG = 35.0        # uphill neighbor must be steeper than this
CAP_SEARCH_BLOCKS = 24          # search radius for adjacent cliff face (was 8)
CAP_PEAK_LAP_THR = 1.0          # negative laplacian > this = convex peak
CAP_PEAK_MIN_Y = 200            # only peaks above this Y count
CAP_FADE_BLOCKS = 6             # gaussian fade ring at cap boundary
CAP_BASE_INTENSITY = 200        # solid cap pixels get this value
CAP_PEAK_INTENSITY = 220        # peak pixels get this (stronger)


def read_at_scale(path: str, scale: int) -> np.ndarray:
    """Read full TIF at decimated 1:scale resolution."""
    with rasterio.open(path) as src:
        out_h = src.shape[0] // scale
        out_w = src.shape[1] // scale
        arr = src.read(1, out_shape=(out_h, out_w))
    return arr


def compute_slope_deg(height_f: np.ndarray) -> np.ndarray:
    """Approximate slope in degrees from height gradient (at 1:4 scale)."""
    gy, gx = np.gradient(height_f.astype(np.float32))
    # gradient is per-pixel; multiply by scale to get per-block
    slope_per_block = np.sqrt(gx**2 + gy**2) * SCALE
    # Convert to degrees: arctan(rise/run) where run=1 block
    return np.degrees(np.arctan(slope_per_block / 1.0)).astype(np.float32)


def compute_laplacian_magnitude(height_f: np.ndarray) -> np.ndarray:
    """Smooth then discrete laplacian magnitude — sharp curvature features."""
    smooth = gaussian_filter(height_f.astype(np.float32), sigma=VEIN_LAP_SIGMA)
    # Discrete Laplacian via sobel-of-sobel
    lap = np.zeros_like(smooth)
    lap[1:-1, 1:-1] = (
        smooth[:-2, 1:-1] + smooth[2:, 1:-1] +
        smooth[1:-1, :-2] + smooth[1:-1, 2:] -
        4.0 * smooth[1:-1, 1:-1]
    )
    return lap  # signed: negative=convex/peak, positive=concave/bowl


def compute_fault_zero_crossings(H: int, W: int) -> np.ndarray:
    """Walk #10.2: TWO-scale branching fault network.

    Mainlines (period 240) = long linear traces — the dominant fault set.
    Branches (period 80) = shorter cross-faults — only spawn within
    BRANCH_PROXIMITY pixels of a mainline (so they appear as feeders/
    splays off the major faults, not random everywhere).
    """
    # Mainlines — long-period simplex zero-crossings
    osn_main = opensimplex.OpenSimplex(seed=0xFA017)
    main_px = VEIN_FAULT_MAIN_SCALE // SCALE
    fault_main = osn_main.noise2array(
        np.arange(W, dtype=np.float64) / main_px,
        np.arange(H, dtype=np.float64) / main_px,
    ).astype(np.float32)
    mainlines = (np.abs(fault_main) < VEIN_FAULT_MAIN_WIDTH).astype(np.float32)
    print(f"    mainline pixels: {mainlines.mean()*100:.2f}%")

    # Branches — short-period simplex zero-crossings, gated by proximity to mainlines
    osn_br = opensimplex.OpenSimplex(seed=0xBEACE)
    br_px = max(1, VEIN_FAULT_BRANCH_SCALE // SCALE)
    fault_br = osn_br.noise2array(
        np.arange(W, dtype=np.float64) / br_px,
        np.arange(H, dtype=np.float64) / br_px,
    ).astype(np.float32)
    branches_raw = (np.abs(fault_br) < VEIN_FAULT_BRANCH_WIDTH).astype(np.float32)
    # Gate to within BRANCH_PROXIMITY of mainlines
    mainlines_bool = mainlines.astype(bool)
    proximity_iters = max(1, VEIN_FAULT_BRANCH_PROXIMITY // SCALE)
    main_zone = binary_dilation(mainlines_bool, iterations=proximity_iters)
    branches = branches_raw * main_zone.astype(np.float32)
    print(f"    branch pixels (near mainline): {branches.mean()*100:.2f}%")

    # Combine — mainlines at full strength, branches at 70% strength
    return np.maximum(mainlines, branches * 0.7)


def write_upscaled_uint8(arr_1x4: np.ndarray, output_path: str) -> None:
    """Walk #10.1: BILINEAR upscale from H_BUILD to H_50K via PIL.Image.resize.

    The previous NEAREST 1:4 -> 1:1 upscale (np.repeat) produced 4-block
    'staircase' stamps at every signal boundary because each source pixel
    became a perfect 4x4 patch of identical values.  Bilinear smooths the
    transitions: each source pixel now contributes proportionally to a
    region of the output, eliminating the right-angle stairsteps.

    PIL handles the 2.5GB intermediate efficiently (single-image L-mode
    resize).  Then chunked write to GeoTIFF.
    """
    from PIL import Image
    profile = {
        "driver": "GTiff",
        "height": H_50K, "width": H_50K, "count": 1,
        "dtype": "uint8",
        "compress": "lzw", "tiled": True, "blockxsize": 512, "blockysize": 512,
    }
    print(f"  bilinear upscale to {H_50K}x{H_50K}...")
    src_img = Image.fromarray(arr_1x4, mode="L")
    big_img = src_img.resize((H_50K, H_50K), Image.BILINEAR)
    print(f"  writing {output_path}...")
    with rasterio.open(output_path, "w", **profile) as dst:
        # Crop in 1024-row chunks and write
        for y_start in range(0, H_50K, 1024):
            y_end = min(y_start + 1024, H_50K)
            chunk_h = y_end - y_start
            chunk = np.asarray(
                big_img.crop((0, y_start, H_50K, y_end)),
                dtype=np.uint8,
            )
            dst.write(chunk, 1, window=Window(0, y_start, H_50K, chunk_h))


def main() -> int:
    t0 = time.time()
    print("=== build_vein_and_cap_masks ===")
    print(f"reading masks/height.tif at 1:{SCALE}...")
    height = read_at_scale("masks/height.tif", SCALE)
    print(f"  shape={height.shape} dtype={height.dtype}")

    # ── SLOPE in degrees (used by both vein + cap) ──────────────────────
    print("computing slope (deg)...")
    slope_deg = compute_slope_deg(height.astype(np.float32))

    # ── LAPLACIAN (used by both vein + cap-peak) ────────────────────────
    print("computing laplacian (signed)...")
    lap = compute_laplacian_magnitude(height.astype(np.float32))

    # ── 1. VEIN_FIELD ───────────────────────────────────────────────────
    print("=== VEIN_FIELD ===")
    print("  computing simplex fault zero-crossings...")
    fault_traces = compute_fault_zero_crossings(H_BUILD, H_BUILD)
    print(f"  fault_traces coverage: {fault_traces.mean()*100:.1f}%")

    # Normalize |laplacian| to [0,1]
    abs_lap = np.abs(lap)
    lap_p95 = np.percentile(abs_lap, 95)
    lap_norm = np.clip(abs_lap / max(lap_p95, 1e-6), 0.0, 1.0).astype(np.float32)
    print(f"  lap_norm 95th percentile: {lap_p95:.2f}")

    # Combine, gate by slope
    vein_combined = VEIN_LAP_WEIGHT * lap_norm + VEIN_FAULT_WEIGHT * fault_traces
    slope_ok = (slope_deg >= VEIN_SLOPE_MIN_DEG).astype(np.float32)
    vein_field_f = vein_combined * slope_ok
    # Quantize to uint8
    vein_field_u8 = np.clip(vein_field_f * 255, 0, 255).astype(np.uint8)
    pct = (vein_field_u8 >= 32).sum() / vein_field_u8.size * 100
    print(f"  vein_field nonzero: {(vein_field_u8 > 0).sum():,}  >= 32: {pct:.2f}% of build res")
    write_upscaled_uint8(vein_field_u8, "masks/vein_field.tif")
    print(f"  vein_field.tif written ({time.time() - t0:.1f}s total)")

    # ── 2. CLIFF_CAP — edge + peak + fade ────────────────────────────────
    print()
    print("=== CLIFF_CAP (rebuild with peaks + fade) ===")
    # Edge cap: flat pixel adjacent to uphill cliff_face
    flat_px = slope_deg < CAP_FLAT_MAX_DEG
    cliff_face = slope_deg >= CAP_CLIFF_MIN_DEG
    # "Adjacent within search radius" = morphological dilation of cliff_face
    search_radius_1x4 = max(1, CAP_SEARCH_BLOCKS // SCALE)
    cliff_dilated = binary_dilation(cliff_face, iterations=search_radius_1x4)
    edge_cap_mask = flat_px & cliff_dilated
    print(f"  edge_cap pixels: {edge_cap_mask.sum():,} ({edge_cap_mask.mean()*100:.2f}%)")

    # Peak cap: convex peaks (lap < -CAP_PEAK_LAP_THR) at elevation
    # Need to convert height (raw) to MC Y — but for now use a simple
    # "high elevation" check via the raw value range.
    raw_min = height.min()
    raw_max = height.max()
    # In Gaea polarity: LOW raw = HIGH terrain.  Use percentile of raw.
    # Peaks = lowest 20% of raw values = highest 20% of terrain.
    raw_peak_thr = np.percentile(height, 20)
    high_elev = height <= raw_peak_thr
    convex_peak = (lap < -CAP_PEAK_LAP_THR) & high_elev
    print(f"  peak_cap pixels: {convex_peak.sum():,} ({convex_peak.mean()*100:.2f}%)")

    # Combine: edge gets BASE_INTENSITY, peak gets PEAK_INTENSITY
    cap_intensity = np.zeros((H_BUILD, H_BUILD), dtype=np.float32)
    cap_intensity[edge_cap_mask] = CAP_BASE_INTENSITY
    cap_intensity[convex_peak] = np.maximum(cap_intensity[convex_peak], CAP_PEAK_INTENSITY)

    # Add fade band outside the combined zone via distance transform
    combined_zone = edge_cap_mask | convex_peak
    if combined_zone.any():
        fade_radius_1x4 = max(1, CAP_FADE_BLOCKS // SCALE)
        # Distance from cap zone (outside the zone)
        dist_outside = distance_transform_edt(~combined_zone).astype(np.float32)
        # Fade intensity falls off linearly over fade_radius
        fade_intensity = np.clip(
            (1.0 - dist_outside / max(1.0, fade_radius_1x4)), 0.0, 1.0
        ) * (CAP_BASE_INTENSITY * 0.5)  # half-intensity in fade band
        # Only apply fade WHERE not already in core cap
        cap_intensity = np.maximum(cap_intensity, fade_intensity * (~combined_zone))

    cap_u8 = np.clip(cap_intensity, 0, 255).astype(np.uint8)
    pct_strong = (cap_u8 >= 32).sum() / cap_u8.size * 100
    print(f"  cliff_cap >=32 (paintable): {pct_strong:.2f}% of build res")
    write_upscaled_uint8(cap_u8, "masks/cliff_cap.tif")
    print(f"  cliff_cap.tif written ({time.time() - t0:.1f}s total)")

    # ── 3. VARNISH_FIELD — slope-faded drip-flow zone ────────────────────
    print()
    print("=== VARNISH_FIELD ===")
    print("  reading masks/flow.tif at 1:4...")
    flow_full = read_at_scale("masks/flow.tif", SCALE).astype(np.float32)
    # flow.tif is stored uint16 0..65535 mapping to [0,1].  Normalize:
    if flow_full.max() > 1.5:
        flow_full = flow_full / 65535.0
    print(f"  flow range: {flow_full.min():.6f}..{flow_full.max():.6f}")

    # Slope fade-in factor (0 at slope_min, 1 at slope_max)
    slope_denom = max(0.1, VARN_SLOPE_MAX_DEG - VARN_SLOPE_MIN_DEG)
    varn_slope_factor = np.clip(
        (slope_deg - VARN_SLOPE_MIN_DEG) / slope_denom, 0.0, 1.0,
    ).astype(np.float32)

    # Drip flow band
    drip_band = (
        (flow_full > VARN_FLOW_DRIP_MIN) & (flow_full < VARN_FLOW_DRIP_MAX)
    )
    print(f"  drip-band pixels: {drip_band.sum():,} ({drip_band.mean()*100:.2f}%)")

    # Combine: intensity = slope_factor * drip * 255
    varn_intensity = varn_slope_factor * drip_band.astype(np.float32)
    varn_u8 = np.clip(varn_intensity * 255, 0, 255).astype(np.uint8)
    pct_varn = (varn_u8 >= 32).sum() / varn_u8.size * 100
    print(f"  varnish_field >=32: {pct_varn:.2f}%  max={varn_u8.max()}")
    write_upscaled_uint8(varn_u8, "masks/varnish_field.tif")
    print(f"  varnish_field.tif written ({time.time() - t0:.1f}s total)")

    print()
    print(f"DONE in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
