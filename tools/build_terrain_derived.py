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
    binary_dilation, distance_transform_edt, gaussian_filter, sobel,
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


def _wind_factor(surface_y: np.ndarray, wind_source_deg: float) -> np.ndarray:
    """Windward exposure in [0,1]: 1 where the slope's downhill FACING points
    straight into the wind SOURCE bearing, 0 leeward/across. Direct gradient .
    wind-vector dot product in an explicit (east, north) frame (north = -row,
    east = +col) — no aspect-angle convention to invert. Verified: source WNW
    (292 deg) => west/WNW faces ~1.0, ESE faces 0. SHARED by rock_layers +
    cliff_cap so the world wind is consistent and the sign lives in one place."""
    sy = gaussian_filter(surface_y.astype(np.float32), sigma=1.5)
    dy = sobel(sy, axis=0)   # d/d row (row+ = south)
    dx = sobel(sy, axis=1)   # d/d col (col+ = east)
    face_e = -dx
    face_n = dy
    mag = np.hypot(face_e, face_n) + 1e-6
    s = np.radians(wind_source_deg)
    return np.clip(
        (face_e * float(np.sin(s)) + face_n * float(np.cos(s))) / mag, 0.0, 1.0
    ).astype(np.float32)


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


def build_cliff_cap(surface_y: np.ndarray, convex_norm: np.ndarray,
                     wind_factor: np.ndarray, cap_cfg: dict) -> np.ndarray:
    """S89 convexity-exposure polish (was cliff-top 8-dir walk).

    intensity = convex_norm * (1 + wind_coeff*wind_factor) * elev_fade, as
    uint8 0-255 (continuous -> bilinear upscale). Lights up convex crests /
    domes / knobs (the gentle convex tops the rock_layers slope ladder misses),
    intensified on windward (WNW) faces, faded in only at alpine elevation
    [elev_lo, elev_hi] MC-Y. Drives the scoured-cap palette + tree/ground-cover
    suppression downstream."""
    convex_coeff = float(cap_cfg.get("convex_coeff", 1.0))
    wind_coeff = float(cap_cfg.get("wind_coeff", 0.5))
    elev_lo = float(cap_cfg.get("elev_lo", 150.0))
    elev_hi = float(cap_cfg.get("elev_hi", 220.0))
    elev_fade = np.clip(
        (surface_y.astype(np.float32) - elev_lo) / max(1.0, elev_hi - elev_lo),
        0.0, 1.0,
    )
    base = np.clip(convex_norm.astype(np.float32) * convex_coeff, 0.0, 1.0)
    wind_boost = 1.0 + wind_coeff * wind_factor.astype(np.float32)
    inten = np.clip(base * wind_boost * elev_fade, 0.0, 1.0) * 255.0
    return inten.astype(np.uint8)


def build_talus_apron(surface_y: np.ndarray, slope_deg: np.ndarray,
                       cliff_min_deg: float, apron_max_deg: float,
                       search_pixels: int,
                       concavity_pos_norm: np.ndarray | None = None,
                       gully_fan_coeff: float = 0.0) -> np.ndarray:
    """S89: debris apron below cliffs. Walk downhill from cliff faces with
    run-out taper, gate to slope < apron_max_deg (angle of repose). Then
    GULLY-FAN: multiply intensity by (1 + gully_fan_coeff * concavity_pos_norm)
    so talus concentrates in concave footslopes (couloir/gully mouths) and
    stays thin below planar faces."""
    cliff_face = slope_deg >= cliff_min_deg
    inten = _8direction_walk(cliff_face, surface_y, slope_deg,
                              search_pixels=search_pixels,
                              slope_match_max_deg=apron_max_deg,
                              uphill=False).astype(np.float32)
    if concavity_pos_norm is not None and gully_fan_coeff > 0.0:
        fan = 1.0 + float(gully_fan_coeff) * concavity_pos_norm
        inten = np.clip(inten * fan, 0.0, 255.0)
    return inten.astype(np.uint8)


def build_rock_layers_u(surface_y: np.ndarray, slope_deg: np.ndarray,
                         lithology: np.ndarray, name_to_id: dict[str, int],
                         rl_cfg: dict) -> np.ndarray:
    """S89 rock_layers: build the CONTINUOUS per-group 'tier coordinate' field.

    Returns float32 `u` (same shape as slope_deg) where, per lithology group,
    slope_eff is piecewise-mapped through that group's percentile thresholds:
        0 -> 0 (not rock) ... t1 -> 1 (floor) ... t2 -> 2 (dark/mid) ...
        t3 -> 3 (mid/light) ... smax -> 4
    t2/t3 are the slope values that split the group's in-rock pixels by its
    `split` [dark%,mid%,light%] target (computed as percentiles HERE so the px
    dominance is hit exactly, regardless of the distorted degree scale).

    Wind: a uniform shift is folded into slope_eff (slope + wind_factor*delta),
    windward faces (toward wind_source compass bearing) get MORE stone and
    lighter tiers, with the dark/mid/light SPACING preserved (uniform shift).

    NOTE: do NOT threshold or dither here. This continuous u is upscaled to 50k
    and tier-thresholded + blue-noise-dithered at target res by
    core.upscale.upscale_continuous_then_multitier_dither — keeps tier edges
    organic instead of NEAREST-staircased.
    """
    H, W = slope_deg.shape
    # FIXED degree thresholds (user S89-v2): dark exposes at t1, mid at t2,
    # light at t3 -- same for every lithology group (no percentile split).
    t1 = float(rl_cfg.get("t1_deg", 45.0))
    t2 = float(rl_cfg.get("t2_deg", 50.0))
    t3 = float(rl_cfg.get("t3_deg", 55.0))
    smax = float(rl_cfg.get("smax_deg", 90.0))
    wind_source = float(rl_cfg.get("wind_source_deg", 292.0))
    wind_delta = float(rl_cfg.get("wind_delta_deg", 6.0))
    groups_cfg = rl_cfg.get("groups", {})

    # Wind: uniform shift folded into slope_eff (windward faces => more stone,
    # gradient spacing preserved). World wind TRAVELS ESE (~112); SOURCE WNW
    # (wind_source ~292). See _wind_factor for the verified frame.
    wind_factor = _wind_factor(surface_y, wind_source)
    slope_eff = (slope_deg.astype(np.float32) + wind_factor * wind_delta)

    # Piecewise map slope_eff -> u: <t1 -> not rock (<1); t1..t2 -> dark [1,2);
    # t2..t3 -> mid [2,3); >=t3 -> light [3,4]. Tier boundaries dithered at 50k.
    xp = np.array([0.0, t1, t2, t3, smax], dtype=np.float32)
    fp = np.array([0.0, 1.0, 2.0, 3.0, 4.0], dtype=np.float32)
    u = np.zeros((H, W), dtype=np.float32)
    for gname, gdata in groups_cfg.items():
        gid = name_to_id.get(gname)
        if gid is None:
            continue
        gmask = (lithology == gid)
        if not gmask.any():
            continue
        u[gmask] = np.interp(slope_eff[gmask], xp, fp).astype(np.float32)
        print(f"    {gname}(id{gid}): pixels={int(gmask.sum()):>9d}  "
              f"fixed t1/t2/t3={t1:.0f}/{t2:.0f}/{t3:.0f}  "
              f">=t1 frac={float((u[gmask] >= 1).mean()):.3f}")
    return u


# ─── Upscale + write ─────────────────────────────────────────────────────

def chunked_upscale_write(arr_ds: np.ndarray, path: Path, scale: int,
                            method: str = "bilinear", full_size: int = WORLD_50K) -> None:
    """Chunked upscale 1:scale -> full_size + write to disk in row stripes.
    Peak memory = chunk_rows * full_size * 1 byte ~ 1 MB per stripe.

    Uses core.hydrology_precompute.write_upscaled (the existing chunked
    upscaler used by rebuild_floodplain etc.) so the I/O profile matches
    every other mask write. full_size defaults to 50k (mainland); islands
    pass their footprint."""
    from core.hydrology_precompute import write_upscaled
    path.parent.mkdir(parents=True, exist_ok=True)
    write_upscaled(
        data=arr_ds, path=path, dtype="uint8", scale=scale,
        full_size=full_size, chunk_rows=100,
        interpolation=("bilinear" if method == "bilinear" else "nearest"),
    )


# ─── Main ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--masks", default="masks/")
    ap.add_argument("--config", default="config/thresholds.json")
    ap.add_argument("--only", default=None,
                     help='Comma-separated subset: aspect,bedrock,talus,cap')
    ap.add_argument("--scale", type=int, default=DEFAULT_SCALE,
                     help='Working scale denominator (4 = 12500x12500; 8 = 6250x6250)')
    ap.add_argument("--world-size", type=int, default=WORLD_50K,
                     help='Full output size (default 50000 mainland; islands pass their square footprint)')
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
    ds_size = args.world_size // scale
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

    # S89: shared concavity field (neighbour-mean - center; positive in
    # bowls/valleys/gully mouths). Drives the talus gully-fan here; reused by
    # the snow physics builder later.
    from scipy.ndimage import uniform_filter as _uf
    _sy_f = surface_y.astype(np.float32)
    _nbr_mean = (_uf(_sy_f, size=3) * 9.0 - _sy_f) / 8.0
    _concavity = _nbr_mean - _sy_f
    _cmax = max(float(np.abs(_concavity).max()), 1e-6)
    concavity_pos_norm = np.clip(_concavity / _cmax, 0.0, 1.0).astype(np.float32)
    # Convex side (ridge crests / domes / knobs) for the cliff_cap polish pass.
    # Computed at a COARSE (dome) scale, not the fine 3-block laplacian, so
    # broad convex tops register instead of being dwarfed by sharp cliff-edge
    # curvature; robustly normalized by the 95th percentile (extremes clip).
    from scipy.ndimage import laplace as _lap
    _cap_sigma_px = max(1.0, float(cap_cfg.get("curv_smooth_blocks", 48.0)) / scale)
    _sy_sm = gaussian_filter(_sy_f, sigma=_cap_sigma_px)
    _conv = np.clip(-_lap(_sy_sm), 0.0, None)
    _cp = float(np.percentile(_conv[_conv > 0], 95)) if (_conv > 0).any() else 1.0
    convex_norm = np.clip(_conv / max(_cp, 1e-6), 0.0, 1.0).astype(np.float32)

    def world_to_pixels(world_blocks: float) -> int:
        return max(1, int(round(world_blocks / scale)))

    def emit(name: str, arr_ds: np.ndarray, method: str = "bilinear") -> None:
        t_ = time.perf_counter()
        path = masks_dir / f"{name}.tif"
        chunked_upscale_write(arr_ds, path, scale=scale, method=method, full_size=args.world_size)
        # Report stats from working-scale array (50k version is on disk only,
        # not in memory).  Working-scale nonzero is a representative ratio.
        nz_ds = int((arr_ds > 0).sum())
        mx_ds = int(arr_ds.max())
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"  -> {name}.tif  ds_nonzero={nz_ds:>9d}  ds_max={mx_ds:>3d}  "
              f"file={size_mb:>5.1f} MB  in {time.perf_counter()-t_:.1f}s")

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
            apron_max_deg=float(talus_cfg.get("apron_max_deg", 35.0)),
            search_pixels=world_to_pixels(
                talus_cfg.get("search_blocks", 80)),
            concavity_pos_norm=concavity_pos_norm,
            gully_fan_coeff=float(talus_cfg.get("gully_fan_coeff", 0.0)),
        )
        print(f"  computed in {time.perf_counter()-t:.1f}s")
        emit("talus_apron", talus_ds, method="bilinear")

    if not only or "cap" in only:
        print("Building cliff_cap...")
        t = time.perf_counter()
        _cap_wf = _wind_factor(
            surface_y, float(cap_cfg.get("wind_source_deg", 292.0)))
        cap_ds = build_cliff_cap(surface_y, convex_norm, _cap_wf, cap_cfg)
        print(f"  computed in {time.perf_counter()-t:.1f}s")
        emit("cliff_cap", cap_ds, method="bilinear")

    if not only or "rock_layers" in only:
        print("Building rock_layers...")
        t = time.perf_counter()
        rl_cfg = litho.get("rock_layers", {})
        litho_path = masks_dir / "lithology.tif"
        if not rl_cfg.get("groups"):
            print("  SKIP rock_layers: no lithology.rock_layers.groups in config")
        elif not litho_path.exists():
            print(f"  SKIP rock_layers: {litho_path} missing")
        else:
            name_to_id = {gn: int(gd.get("id", 0))
                          for gn, gd in litho.get("groups", {}).items()}
            litho_ds = read_at_scale(masks_dir, "lithology", ds_size,
                                     Resampling.nearest)
            u = build_rock_layers_u(surface_y, slope_deg, litho_ds,
                                    name_to_id, rl_cfg)
            from core.upscale import upscale_continuous_then_multitier_dither
            rl_path = masks_dir / "rock_layers.tif"
            upscale_continuous_then_multitier_dither(
                u, rl_path,
                levels=(1.0, 2.0, 3.0),
                dither_u=float(rl_cfg.get("dither_u", 0.18)),
                target_size=args.world_size,
                interpolation="catmull_rom",
            )
            size_mb = rl_path.stat().st_size / (1024 * 1024)
            print(f"  -> rock_layers.tif  ds_in_rock={int((u >= 1.0).sum())}  "
                  f"file={size_mb:.1f} MB  in {time.perf_counter()-t:.1f}s")

    print(f"\nTotal time: {time.perf_counter() - t_total:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
