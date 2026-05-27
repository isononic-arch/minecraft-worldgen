"""
build_vein_and_cap_masks.py — S88 walk #10/11: build vein_field.tif + rebuild
cliff_cap.tif with convex peaks + fade band.

Outputs (50k uint8):
  masks/vein_field.tif
  masks/cliff_cap.tif

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
VEIN_FAULT_SCALE_BLOCKS = 240   # simplex period in world blocks
VEIN_FAULT_WIDTH = 0.12         # |noise| < width = fault trace zone
VEIN_SLOPE_MIN_DEG = 32.0       # gate: only on >= 32° slopes (matches strata)

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


def compute_fault_zero_crossings(H: int, W: int, scale_blocks: int) -> np.ndarray:
    """Simplex noise at scale, return |noise| < width as fault trace zones."""
    # Sample period in 1:4 pixels
    period_px = scale_blocks // SCALE
    osn = opensimplex.OpenSimplex(seed=0xFA017)
    x_idx = np.arange(W, dtype=np.float64) / period_px
    y_idx = np.arange(H, dtype=np.float64) / period_px
    fault = osn.noise2array(x_idx, y_idx).astype(np.float32)
    return (np.abs(fault) < VEIN_FAULT_WIDTH).astype(np.float32)


def write_upscaled_uint8(arr_1x4: np.ndarray, output_path: str) -> None:
    """Upscale arr from H_BUILD to H_50K via NEAREST + chunked write."""
    profile = {
        "driver": "GTiff",
        "height": H_50K, "width": H_50K, "count": 1,
        "dtype": "uint8",
        "compress": "lzw", "tiled": True, "blockxsize": 512, "blockysize": 512,
    }
    print(f"  writing {output_path} as {H_50K}x{H_50K}...")
    with rasterio.open(output_path, "w", **profile) as dst:
        # Write in 1024-row chunks
        for y_start in range(0, H_50K, 1024):
            y_end = min(y_start + 1024, H_50K)
            chunk_h = y_end - y_start
            # Find corresponding source rows
            src_y_start = y_start // SCALE
            src_y_end = (y_end + SCALE - 1) // SCALE
            src_chunk = arr_1x4[src_y_start:src_y_end, :]
            # Upscale via repeat
            upscaled = np.repeat(np.repeat(src_chunk, SCALE, axis=0), SCALE, axis=1)
            # Crop to exact target rows + width
            upscaled = upscaled[: chunk_h, : H_50K]
            dst.write(upscaled.astype(np.uint8), 1, window=Window(0, y_start, H_50K, chunk_h))


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
    fault_traces = compute_fault_zero_crossings(H_BUILD, H_BUILD, VEIN_FAULT_SCALE_BLOCKS)
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

    print()
    print(f"DONE in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
