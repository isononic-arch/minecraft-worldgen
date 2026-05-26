"""
build_terrain_derived.py — Produce the four S88 precompute masks:

  masks/cliff_cap.tif         uint8 50k  (cap-rock intensity at cliff tops)
  masks/talus_apron.tif       uint8 50k  (debris-fan intensity below cliffs)
  masks/bedrock_drainage.tif  uint8 50k  (water-cut polished rock channels)
  masks/aspect.tif            uint8 50k  (compass facing; 255 = flat sentinel)

Working scale: 1:4 (12500x12500).  Each working pixel = 4 world blocks.
At 1:4 the slope gradient + 8-direction walk are accurate enough for
cliff-cap precision (4-block shelves) while keeping memory <6 GB peak.

Reads:
  masks/height.tif   (uint16 50k)   — Gaea raw heightmap
  masks/flow.tif     (uint16/float 50k) — Gaea flow accumulation

Per-mask parameters come from config/thresholds.json:
  lithology.cliff_cap          (search_blocks, cliff_min_deg, flat_max_deg)
  lithology.talus              (search_blocks, cliff_min_deg, apron_max_deg)
  lithology.bedrock_drainage   (flow_threshold, slope_min_deg, dilation_blocks, fade_blocks)
  eco_gradients.aspect         (slope_min_deg)

Total runtime: ~5-8 min on a single thread.  Output ~50 MB each compressed.

Usage:
  py tools/build_terrain_derived.py
  py tools/build_terrain_derived.py --only aspect,bedrock
  py tools/build_terrain_derived.py --scale 8        # downscale to 1:8 for speed
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
    binary_dilation, distance_transform_edt, gaussian_filter, sobel, zoom,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ─── Constants ───────────────────────────────────────────────────────────

WORLD_50K = 50_000
DEFAULT_SCALE = 4  # 1:4 working scale; override via --scale


# ─── Helpers ─────────────────────────────────────────────────────────────

def read_at_scale(masks_dir: Path, name: str, ds_size: int,
                   resampling: Resampling = Resampling.average) -> np.ndarray:
    """Read a 50k TIF downsampled to ds_size × ds_size via rasterio out_shape."""
    path = masks_dir / f"{name}.tif"
    with rasterio.open(str(path)) as src:
        return src.read(1, out_shape=(ds_size, ds_size), resampling=resampling)


def height_norm_to_mc_y(h_norm: np.ndarray) -> np.ndarray:
    """Map normalised height [0,1] through terrain_spline LUT to MC Y (int16)."""
    from core import column_generator as col_gen
    h_int = np.clip((h_norm * 65535.0).astype(np.int32), 0, 65535)
    return col_gen._LUT[h_int].astype(np.int16)


def slope_deg_from_surface_y(surface_y: np.ndarray, scale: int,
                              sigma: float = 1.5) -> np.ndarray:
    """Slope in degrees.  At 1:scale, 1 grid step = `scale` world blocks horizontal.
    np.gradient gives Δheight per pixel → divide by scale to recover true rise/run."""
    sy_smooth = gaussian_filter(surface_y.astype(np.float32), sigma=sigma)
    gy, gx = np.gradient(sy_smooth)
    grad_per_world_block = np.hypot(gx, gy) / float(scale)
    return np.degrees(np.arctan(grad_per_world_block)).astype(np.float32)


# ─── Mask builders (all at working scale) ────────────────────────────────

def build_aspect(surface_y: np.ndarray, slope_deg: np.ndarray,
                  slope_min_deg: float) -> np.ndarray:
    """Compass facing as uint8 0..255, with 255 = flat sentinel."""
    sy_smooth = gaussian_filter(surface_y.astype(np.float32), sigma=1.5)
    dy = sobel(sy_smooth, axis=0)
    dx = sobel(sy_smooth, axis=1)
    # Convention: atan2(-dy, dx) → 0=East, π/2=North, π=West, -π/2=South.
    aspect_rad = np.arctan2(-dy, dx)
    aspect_byte = (
        (aspect_rad + np.pi) * (256.0 / (2.0 * np.pi))
    ).astype(np.int32) % 256
    # Reserve 255 as the flat sentinel; clip 254 to 253 to avoid collision.
    aspect_byte[aspect_byte == 255] = 254
    aspect_byte[slope_deg < slope_min_deg] = 255
    return aspect_byte.astype(np.uint8)


def build_bedrock_drainage(flow: np.ndarray, slope_deg: np.ndarray,
                            flow_threshold: float, slope_min_deg: float,
                            dilation_pixels: int, fade_pixels: int) -> np.ndarray:
    """Steep + high-flow rock channels.  dilation_pixels / fade_pixels are at
    working scale (caller converts world-blocks → pixels)."""
    raw = (flow > flow_threshold) & (slope_deg >= slope_min_deg)
    if not raw.any():
        return np.zeros(raw.shape, dtype=np.uint8)
    if dilation_pixels > 0:
        core = binary_dilation(raw, iterations=dilation_pixels)
    else:
        core = raw
    dist = distance_transform_edt(~core).astype(np.float32)
    intensity = np.clip(
        255.0 * (1.0 - dist / max(0.5, float(fade_pixels))),
        0.0, 255.0,
    ).astype(np.uint8)
    return intensity


def _8direction_walk(seed: np.ndarray, surface_y: np.ndarray,
                      slope_deg: np.ndarray, search_pixels: int,
                      slope_match_max_deg: float, uphill: bool) -> np.ndarray:
    """Walk `seed` cells outward `search_pixels` steps along the per-pixel
    gradient direction (uphill or downhill).  Each step decays intensity by
    1/(search_pixels+1).  Final intensity is gated by slope < slope_match_max_deg
    so only cells of the target type (cap shelf / talus apron) get values."""
    if not seed.any() or search_pixels < 1:
        return np.zeros(seed.shape, dtype=np.uint8)
    sy_smooth = gaussian_filter(surface_y.astype(np.float32), sigma=1.5)
    dy = sobel(sy_smooth, axis=0)
    dx = sobel(sy_smooth, axis=1)
    grad_mag = np.hypot(dy, dx) + 1e-6
    intensity = np.zeros(seed.shape, dtype=np.float32)
    sign = 1.0 if uphill else -1.0
    # 8 compass directions (dr, dc)
    DIRS = [(0, 1), (1, 1), (1, 0), (1, -1),
            (0, -1), (-1, -1), (-1, 0), (-1, 1)]
    for dr, dc in DIRS:
        # cells whose gradient direction is closest to (sign*dr, sign*dc).
        # Tolerance: dot/|grad| > cos(45°) = 0.707 means a ±45° band per dir.
        dot_norm = (sign * dy * dr + sign * dx * dc) / grad_mag
        match = dot_norm > 0.707
        if not match.any():
            continue
        for step in range(1, search_pixels + 1):
            # Walk seed cells `step` units in (dr,dc).  np.roll wraps but
            # the edges are <1% of total area; acceptable for a precompute.
            rolled = np.roll(seed, shift=(step * dr, step * dc), axis=(0, 1))
            here = rolled & match
            if not here.any():
                continue
            step_intensity = 255.0 * (1.0 - step / float(search_pixels + 1))
            np.maximum(intensity, here.astype(np.float32) * step_intensity,
                       out=intensity)
    # Cap must itself be a flat shelf / apron must itself be flat ground.
    intensity[slope_deg >= slope_match_max_deg] = 0.0
    return intensity.astype(np.uint8)


def build_cliff_cap(surface_y: np.ndarray, slope_deg: np.ndarray,
                     cliff_min_deg: float, flat_max_deg: float,
                     search_pixels: int) -> np.ndarray:
    cliff_face = slope_deg >= cliff_min_deg
    return _8direction_walk(cliff_face, surface_y, slope_deg,
                              search_pixels=search_pixels,
                              slope_match_max_deg=flat_max_deg,
                              uphill=True)


def build_talus_apron(surface_y: np.ndarray, slope_deg: np.ndarray,
                       cliff_min_deg: float, apron_max_deg: float,
                       search_pixels: int) -> np.ndarray:
    cliff_face = slope_deg >= cliff_min_deg
    return _8direction_walk(cliff_face, surface_y, slope_deg,
                              search_pixels=search_pixels,
                              slope_match_max_deg=apron_max_deg,
                              uphill=False)


# ─── Upscale + write ─────────────────────────────────────────────────────

def upscale_50k(arr_ds: np.ndarray, scale: int,
                 method: str = "bilinear") -> np.ndarray:
    """Upscale 1:scale → 50k.  method='bilinear' for fade-like masks,
    'nearest' for categorical (aspect)."""
    factor = WORLD_50K / arr_ds.shape[0]
    order = 1 if method == "bilinear" else 0
    out = zoom(arr_ds, factor, order=order, prefilter=False)
    return np.clip(out, 0, 255).astype(np.uint8)


def write_mask(path: Path, arr_50k: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff",
        height=WORLD_50K, width=WORLD_50K,
        count=1, dtype="uint8",
        compress="lzw", tiled=True, blockxsize=512, blockysize=512,
    ) as dst:
        dst.write(arr_50k, 1)


# ─── Main ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--masks", default="masks/")
    ap.add_argument("--config", default="config/thresholds.json")
    ap.add_argument("--only", default=None,
                     help='Comma-separated subset: aspect,bedrock,talus,cap')
    ap.add_argument("--scale", type=int, default=DEFAULT_SCALE,
                     help='Working scale denominator (4 = 12500x12500; 8 = 6250x6250)')
    args = ap.parse_args()

    masks_dir = Path(args.masks)
    cfg_path = Path(args.config)
    if not masks_dir.is_dir():
        print(f"ERROR: masks dir not found: {masks_dir}", file=sys.stderr)
        return 2
    if not cfg_path.is_file():
        print(f"ERROR: config not found: {cfg_path}", file=sys.stderr)
        return 2

    scale = int(args.scale)
    ds_size = WORLD_50K // scale
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    only = set(args.only.split(",")) if args.only else None

    litho = cfg.get("lithology", {})
    cap_cfg = litho.get("cliff_cap", {})
    talus_cfg = litho.get("talus", {})
    bedrock_cfg = litho.get("bedrock_drainage", {})
    aspect_cfg = cfg.get("eco_gradients", {}).get("aspect", {})

    print(f"Working at 1:{scale} ({ds_size}x{ds_size}).  "
          f"World scale {WORLD_50K}.")

    t_total = time.perf_counter()
    t = time.perf_counter()
    print("Reading height + flow at working scale...")
    h_raw = read_at_scale(masks_dir, "height", ds_size, Resampling.average)
    h_norm = h_raw.astype(np.float32) / (
        65535.0 if h_raw.dtype == np.uint16 else 1.0
    )
    f_raw = read_at_scale(masks_dir, "flow", ds_size, Resampling.average)
    flow = f_raw.astype(np.float32) / (
        65535.0 if f_raw.dtype == np.uint16 else 1.0
    )
    print(f"  height range: {h_norm.min():.4f}..{h_norm.max():.4f}")
    print(f"  flow   range: {flow.min():.4f}..{flow.max():.4f}")
    print(f"  done in {time.perf_counter()-t:.1f}s")

    print("Computing surface_y + slope_deg...")
    t = time.perf_counter()
    surface_y = height_norm_to_mc_y(h_norm)
    slope_deg = slope_deg_from_surface_y(surface_y, scale=scale)
    print(f"  surface_y: {int(surface_y.min())}..{int(surface_y.max())} "
          f"({surface_y.dtype})")
    print(f"  slope_deg: {float(slope_deg.min()):.2f}..{float(slope_deg.max()):.2f} "
          f"({slope_deg.dtype}); "
          f">=25° pixels: {int((slope_deg >= 25).sum())}; "
          f">=35° pixels: {int((slope_deg >= 35).sum())}")
    print(f"  done in {time.perf_counter()-t:.1f}s")

    def world_to_pixels(world_blocks: float) -> int:
        return max(1, int(round(world_blocks / scale)))

    def emit(name: str, arr_ds: np.ndarray, method: str = "bilinear") -> None:
        t_ = time.perf_counter()
        arr_50k = upscale_50k(arr_ds, scale=scale, method=method)
        path = masks_dir / f"{name}.tif"
        write_mask(path, arr_50k)
        nz = int((arr_50k > 0).sum())
        mx = int(arr_50k.max())
        print(f"  -> {name}.tif  nonzero={nz:>10d}  max={mx:>3d}  "
              f"upscale+write {time.perf_counter()-t_:.1f}s")

    if not only or "aspect" in only:
        print("Building aspect...")
        t = time.perf_counter()
        aspect_ds = build_aspect(
            surface_y, slope_deg,
            slope_min_deg=float(aspect_cfg.get("slope_min_deg", 5.0)),
        )
        print(f"  computed in {time.perf_counter()-t:.1f}s; "
              f"flat sentinel pixels: {int((aspect_ds == 255).sum())}")
        emit("aspect", aspect_ds, method="nearest")

    if not only or "bedrock" in only:
        print("Building bedrock_drainage...")
        t = time.perf_counter()
        bdr_ds = build_bedrock_drainage(
            flow, slope_deg,
            flow_threshold=float(bedrock_cfg.get("flow_threshold", 0.02)),
            slope_min_deg=float(bedrock_cfg.get("slope_min_deg", 25.0)),
            dilation_pixels=world_to_pixels(
                bedrock_cfg.get("dilation_blocks", 1)),
            fade_pixels=world_to_pixels(
                bedrock_cfg.get("fade_blocks", 3)),
        )
        print(f"  computed in {time.perf_counter()-t:.1f}s")
        emit("bedrock_drainage", bdr_ds, method="bilinear")

    if not only or "talus" in only:
        print("Building talus_apron...")
        t = time.perf_counter()
        talus_ds = build_talus_apron(
            surface_y, slope_deg,
            cliff_min_deg=float(talus_cfg.get("cliff_min_deg", 35.0)),
            apron_max_deg=float(talus_cfg.get("apron_max_deg", 25.0)),
            search_pixels=world_to_pixels(
                talus_cfg.get("search_blocks", 8)),
        )
        print(f"  computed in {time.perf_counter()-t:.1f}s")
        emit("talus_apron", talus_ds, method="bilinear")

    if not only or "cap" in only:
        print("Building cliff_cap...")
        t = time.perf_counter()
        cap_ds = build_cliff_cap(
            surface_y, slope_deg,
            cliff_min_deg=float(cap_cfg.get("cliff_min_deg", 35.0)),
            flat_max_deg=float(cap_cfg.get("flat_max_deg", 20.0)),
            search_pixels=world_to_pixels(
                cap_cfg.get("search_blocks", 4)),
        )
        print(f"  computed in {time.perf_counter()-t:.1f}s")
        emit("cliff_cap", cap_ds, method="bilinear")

    print(f"\nTotal time: {time.perf_counter() - t_total:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
