"""
build_vein_and_cap_masks.py — S88 walk #10/11: full terrain-derived mask set.

Outputs (50k uint8):
  masks/vein_field.tif       — laplacian + branching fault network, slope-gated
  masks/cliff_cap.tif        — edge caps + convex peaks + gaussian fade band
  masks/varnish_field.tif    — slope-faded drip-flow zone, NORTH-MODULATED (walk #11)
  masks/joint_pattern.tif    — columnar joint planes (walk #11, for basaltics)
  masks/insolation_index.tif — sun exposure index (walk #11, aspect+slope)
  masks/concavity_field.tif  — local depression curvature (walk #11)

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

# ─── CLIFF_CAP params (walk #11 bumped per user) ────────────────────────
CAP_FLAT_MAX_DEG = 28.0         # pixel must be flatter than this to qualify
CAP_CLIFF_MIN_DEG = 35.0        # uphill neighbor must be steeper than this
CAP_SEARCH_BLOCKS = 40          # walk #11: 24 -> 40 (2x bigger cap coverage)
CAP_PEAK_LAP_THR = 1.0          # negative laplacian > this = convex peak
CAP_PEAK_MIN_Y = 200            # only peaks above this Y count
CAP_FADE_BLOCKS = 10            # walk #11: 6 -> 10 (smoother peak halos)
CAP_BASE_INTENSITY = 200        # solid cap pixels get this value
CAP_PEAK_INTENSITY = 220        # peak pixels get this (stronger)

# ─── JOINT_PATTERN params (walk #11 — basaltic columnar joints) ─────────
# Walk #11 fix: bumped col_size_blocks 3 -> 12 so that at 1:4 build res
# (col_size_blocks // SCALE = 3 build-pixels per column) we have actual
# column granularity.  At 50k = 12 block columns = ~12m diameter (real
# basalt is 0.5-2m, but we're at MC block scale; 3-block columns would
# require building at full 50k which is too memory-heavy).
JOINT_COL_SIZE_BLOCKS = 12      # column width in 50k blocks (3 at 1:4)
JOINT_JITTER_SCALE = 48         # simplex period for column-shape jitter
JOINT_JITTER_AMP_BLOCKS = 4.0   # max perturbation (1 build pixel ≈ 4 blocks)
JOINT_DILATE_PX = 0             # 0 = 1-block-wide joints; 1 = 2-block visible

# ─── INSOLATION params ──────────────────────────────────────────────────
# south_factor = -cos(aspect_rad).  insolation = (south_factor+1)/2 * slope_norm.
INSOL_SOUTH_BIAS = True         # True = south faces get high values
INSOL_FLAT_VALUE = 128          # neutral for flat (aspect sentinel = 255)

# ─── CONCAVITY params (matches runtime defaults walk #9.2) ──────────────
CONCAV_LAP_SIGMA = 1.5
CONCAV_LAP_THR = 3.0            # slightly looser than runtime 5.0 so mask
                                # captures candidate zones; runtime can re-gate
CONCAV_SLOPE_MIN_DEG = 32.0
CONCAV_DILATE_PX = 2


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


def compute_joint_pattern(H: int, W: int) -> np.ndarray:
    """Walk #11: simplex-perturbed grid of columnar joint planes.

    Bakes the (col_x, col_z) hash boundaries with a slight per-pixel jitter
    (simplex offset) so columns are quasi-hexagonal, not a perfect grid.
    Output: uint8 where joint boundaries = 255, column interiors = 0.

    Consumed by basaltic groups in surface_decorator — joint pixels get
    a darker basalt variant, producing visible vertical fracture lines on
    cliff faces.
    """
    osn_x = opensimplex.OpenSimplex(seed=0xC0107)
    osn_z = opensimplex.OpenSimplex(seed=0x707A4)
    jit_scale_px = max(1, JOINT_JITTER_SCALE // SCALE)
    jx = osn_x.noise2array(
        np.arange(W, dtype=np.float64) / jit_scale_px,
        np.arange(H, dtype=np.float64) / jit_scale_px,
    ) * JOINT_JITTER_AMP_BLOCKS
    jz = osn_z.noise2array(
        np.arange(W, dtype=np.float64) / jit_scale_px,
        np.arange(H, dtype=np.float64) / jit_scale_px,
    ) * JOINT_JITTER_AMP_BLOCKS

    col_size_px = max(1, JOINT_COL_SIZE_BLOCKS // SCALE)
    x_w = (np.arange(W, dtype=np.float32)[None, :] + jx.astype(np.float32))
    z_w = (np.arange(H, dtype=np.float32)[:, None] + jz.astype(np.float32))
    col_x = (x_w / float(col_size_px)).astype(np.int32)
    col_z = (z_w / float(col_size_px)).astype(np.int32)

    # Boundary: neighbor differs in either axis
    boundary = np.zeros((H, W), dtype=bool)
    diff_zr = np.abs(col_z[:-1, :] - col_z[1:, :]) > 0
    diff_xr = np.abs(col_x[:, :-1] - col_x[:, 1:]) > 0
    boundary[:-1, :] |= diff_zr
    boundary[1:, :] |= diff_zr
    boundary[:, :-1] |= diff_xr
    boundary[:, 1:] |= diff_xr

    if JOINT_DILATE_PX > 0:
        boundary = binary_dilation(boundary, iterations=JOINT_DILATE_PX)

    out = np.where(boundary, 255, 0).astype(np.uint8)
    return out


def compute_insolation_index(slope_deg: np.ndarray, aspect_byte: np.ndarray | None) -> np.ndarray:
    """Walk #11: aspect+slope sun-exposure proxy.  south=high, north=low.

    Used by varnish modulation (north faces wetter = more varnish), and
    available for future vegetation/snow passes.
    """
    H, W = slope_deg.shape
    if aspect_byte is None:
        return np.full((H, W), INSOL_FLAT_VALUE, dtype=np.uint8)
    # aspect_byte 0..254 maps to 0..360°; 255 = flat sentinel
    valid = aspect_byte < 255
    aspect_rad = (aspect_byte.astype(np.float32) * (2 * np.pi / 256.0) - np.pi)
    # south_factor = -cos(aspect) in [-1, 1]; +1 = south
    south_factor = (-np.cos(aspect_rad) + 1.0) * 0.5  # remap to [0, 1]
    # slope contribution: steeper = more direct sun penetration
    slope_norm = np.clip(slope_deg / 60.0, 0.0, 1.0).astype(np.float32)
    insolation = south_factor * slope_norm
    out = np.clip(insolation * 255, 0, 255).astype(np.uint8)
    out[~valid] = INSOL_FLAT_VALUE
    return out


def compute_concavity_field(height_f: np.ndarray, slope_deg: np.ndarray) -> np.ndarray:
    """Walk #11: bake positive laplacian (depressions) + slope gate.

    Same compute as runtime _apply_concavity_drainage but at build time so
    we save per-tile compute + can visualize.
    """
    smooth = gaussian_filter(height_f.astype(np.float32), sigma=CONCAV_LAP_SIGMA)
    lap = np.zeros_like(smooth)
    lap[1:-1, 1:-1] = (
        smooth[:-2, 1:-1] + smooth[2:, 1:-1] +
        smooth[1:-1, :-2] + smooth[1:-1, 2:] -
        4.0 * smooth[1:-1, 1:-1]
    )
    paint_mask = (lap >= CONCAV_LAP_THR) & (slope_deg >= CONCAV_SLOPE_MIN_DEG)
    if CONCAV_DILATE_PX > 0 and paint_mask.any():
        paint_mask = binary_dilation(paint_mask, iterations=CONCAV_DILATE_PX)
        paint_mask &= (slope_deg >= CONCAV_SLOPE_MIN_DEG)
    out = np.where(paint_mask, 255, 0).astype(np.uint8)
    return out


def write_upscaled_uint8(arr_1x4: np.ndarray, output_path: str) -> None:
    """Walk #11 v2: scipy.ndimage.zoom (BLAS-backed) bilinear upscale.

    Previous attempts:
      - np.repeat NEAREST: fast but produced 4-block staircase stamps
      - PIL.Image.resize BILINEAR: single-threaded Python, ~5 min/mask
      - hand-rolled vectorized bilinear: huge temp allocs (~50MB×4 per
        chunk), still slow due to np.ix_ overhead

    scipy.ndimage.zoom does proper spline interpolation in a single C
    call.  order=1 = bilinear.  Allocates the full output in float32
    (~10GB peak for 50k) which fits in 16GB+ cloud boxes.
    """
    from scipy.ndimage import zoom as _zoom
    profile = {
        "driver": "GTiff",
        "height": H_50K, "width": H_50K, "count": 1,
        "dtype": "uint8",
        "compress": "lzw", "tiled": True, "blockxsize": 512, "blockysize": 512,
    }
    print(f"  scipy.zoom (order=1) to {H_50K}x{H_50K}...")
    zoomed = _zoom(arr_1x4.astype(np.float32), zoom=SCALE, order=1, prefilter=False)
    # Trim/pad to exact target size
    zoomed = zoomed[:H_50K, :H_50K]
    if zoomed.shape != (H_50K, H_50K):
        # Pad with edge values if zoom output is short
        full = np.zeros((H_50K, H_50K), dtype=np.float32)
        h, w = zoomed.shape
        full[:h, :w] = zoomed
        zoomed = full
    print(f"  writing {output_path}...")
    out_u8 = np.clip(zoomed, 0, 255).astype(np.uint8)
    with rasterio.open(output_path, "w", **profile) as dst:
        for y_start in range(0, H_50K, 2048):
            y_end = min(y_start + 2048, H_50K)
            dst.write(out_u8[y_start:y_end], 1, window=Window(0, y_start, H_50K, y_end - y_start))


def main() -> int:
    import sys as _sys
    # Force unbuffered stdout so we see progress in real time when redirected
    _sys.stdout.reconfigure(line_buffering=True)
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

    # Read aspect at 1:4 for north-modulation (walk #11)
    print("  reading aspect.tif for north-modulation...")
    aspect_byte = None
    try:
        aspect_full = read_at_scale("masks/aspect.tif", SCALE)
        # aspect uint8: 0..254 = compass 0..360°, 255 = flat sentinel
        if aspect_full.dtype == np.uint8:
            aspect_byte = aspect_full
        else:
            aspect_byte = np.clip(aspect_full, 0, 255).astype(np.uint8)
    except Exception as e:
        print(f"  WARN: no aspect.tif ({e}); skipping north-modulation")

    # Walk #11: NORTH-MODULATION for varnish
    # north_factor: cos(aspect_rad) remapped [-1,1] -> [0,1] where 1=north
    # Multiply varnish intensity by (0.5 + north_factor) so north faces
    # get up to 1.5x stain, south faces ~0.5x (real water lingers on shaded
    # cliffs vs evaporating on sunny ones).
    if aspect_byte is not None:
        _valid = aspect_byte < 255
        _arad = aspect_byte.astype(np.float32) * (2 * np.pi / 256.0) - np.pi
        north_factor = ((np.cos(_arad) + 1.0) * 0.5).astype(np.float32)  # [0,1]
        north_factor[~_valid] = 0.5
        # Aspect modulation: scale = 0.5 + north_factor (range [0.5, 1.5])
        varn_intensity = (
            varn_slope_factor * drip_band.astype(np.float32) *
            (0.5 + north_factor)
        )
    else:
        varn_intensity = varn_slope_factor * drip_band.astype(np.float32)

    varn_u8 = np.clip(varn_intensity * 255, 0, 255).astype(np.uint8)
    pct_varn = (varn_u8 >= 32).sum() / varn_u8.size * 100
    print(f"  varnish_field >=32: {pct_varn:.2f}%  max={varn_u8.max()}")
    write_upscaled_uint8(varn_u8, "masks/varnish_field.tif")
    print(f"  varnish_field.tif written ({time.time() - t0:.1f}s total)")

    # ── 4. JOINT_PATTERN (walk #11 — columnar joints for basaltics) ──────
    print()
    print("=== JOINT_PATTERN ===")
    joint_u8 = compute_joint_pattern(H_BUILD, H_BUILD)
    pct_joint = (joint_u8 >= 128).sum() / joint_u8.size * 100
    print(f"  joint_pattern boundary pixels: {pct_joint:.2f}%  max={joint_u8.max()}")
    write_upscaled_uint8(joint_u8, "masks/joint_pattern.tif")
    print(f"  joint_pattern.tif written ({time.time() - t0:.1f}s total)")

    # ── 5. INSOLATION_INDEX (walk #11 — sun exposure proxy) ──────────────
    print()
    print("=== INSOLATION_INDEX ===")
    insol_u8 = compute_insolation_index(slope_deg, aspect_byte)
    print(f"  insolation min/mean/max: {insol_u8.min()}/{insol_u8.mean():.1f}/{insol_u8.max()}")
    write_upscaled_uint8(insol_u8, "masks/insolation_index.tif")
    print(f"  insolation_index.tif written ({time.time() - t0:.1f}s total)")

    # ── 6. CONCAVITY_FIELD (walk #11 — bake runtime laplacian) ───────────
    print()
    print("=== CONCAVITY_FIELD ===")
    concav_u8 = compute_concavity_field(height.astype(np.float32), slope_deg)
    pct_concav = (concav_u8 >= 128).sum() / concav_u8.size * 100
    print(f"  concavity_field >=128: {pct_concav:.2f}%")
    write_upscaled_uint8(concav_u8, "masks/concavity_field.tif")
    print(f"  concavity_field.tif written ({time.time() - t0:.1f}s total)")

    print()
    print(f"DONE in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
