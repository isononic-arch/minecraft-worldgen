"""Query-time Gaea gap sampler (S60).

Replaces the S56 pre-rendered 50k rock_gap/snow_gap pipeline with per-tile
sampling of the 8k Gaea source via Catmull-Rom at world-space coordinates.
Output is a (H, W) float32 mask in [0, 1] for the requested tile window
(same shape + semantics as the TIF-based reads in `tile_streamer.read_tile`).

Why: the baked 50k mask commits early to one interpolation kernel + one
dither decision, and costs ~2.3 GB on disk per mask. Query-time keeps the
small 8k source in memory and computes the mask live at tile time so
threshold + kernel + dither become runtime knobs, re-render without rebuild.

Process-local cache: the 8k source is loaded once per process (lazy) and
reused across every tile read. Cost ~67 MB RAM per mask = ~128 MB total for
rock+snow, well under the 256 MB/tile budget.

See core/upscale.py:_catmull_rom_at_1d for the underlying interpolator.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import rasterio

from core.upscale import _catmull_rom_at_1d, make_blue_noise_tile

SRC_SIZE = 8192
TARGET_SIZE = 50000
SCALE = SRC_SIZE / TARGET_SIZE   # ≈ 0.1638


@lru_cache(maxsize=4)
def _load_source(path_str: str) -> np.ndarray:
    """Read and cache an 8k Gaea TIF per process. Returns float32."""
    with rasterio.open(path_str) as src:
        raw = src.read(1)
    return raw.astype(np.float32, copy=False)


def sample_gap_at_tile(
    source_8k_path: Path | str,
    col_off: int,
    row_off: int,
    width:  int = 512,
    height: int = 512,
    *,
    pad_px: int = 0,
    threshold: float,
    dither: str = "none",          # "none" | "white" | "blue_noise"
    dither_width: float = 0.0,
    seed: int = 42,
    blue_noise_size: int = 512,
    threshold_noise_scale: float = 0.0,    # S60: simplex wavelength (source-px) for per-pixel threshold modulation
    threshold_noise_amp:   float = 0.0,    # amplitude — threshold shifts DOWN by up to this much at noise=1
    threshold_noise_seed:  int = 43,
) -> np.ndarray:
    """Return an `(eff_h, eff_w)` float32 mask in {0.0, 1.0} for the requested
    tile window by sampling the 8k Gaea source via Catmull-Rom at world-scaled
    positions, then thresholding with optional dither.

    Shape matches `tile_streamer.read_tile`: when `pad_px=0`, output is
    `(height, width)`; when `pad_px>0`, output is `(height+2*pad_px, width+2*pad_px)`.

    Parameters mirror the S56 `upscale_continuous_then_threshold_dither`
    config but evaluated per-tile instead of pre-materialized to disk.
    """
    src = _load_source(str(source_8k_path))

    eff_col = col_off - pad_px
    eff_row = row_off - pad_px
    eff_w = width  + 2 * pad_px
    eff_h = height + 2 * pad_px

    sx = (eff_col + np.arange(eff_w, dtype=np.float32)) * SCALE
    sy = (eff_row + np.arange(eff_h, dtype=np.float32)) * SCALE

    # Separable 1D Catmull-Rom: first X pass on only the source rows we need,
    # then Y pass at the requested target rows.
    # Pick a source-row slab that covers all sy with 2-pixel cubic padding.
    sy_lo = max(0, int(np.floor(sy.min())) - 2)
    sy_hi = min(src.shape[0], int(np.ceil(sy.max())) + 3)
    slab = src[sy_lo:sy_hi]  # keeps arr small

    # After slicing slab, sy coordinates are offset by -sy_lo in slab space.
    sy_slab = sy - sy_lo

    stage_x = _catmull_rom_at_1d(slab, sx, axis=1)        # (slab_h, eff_w)
    vals    = _catmull_rom_at_1d(stage_x, sy_slab, axis=0)  # (eff_h, eff_w)

    # S60: warpy per-pixel threshold modulation. When amp > 0 and scale > 0,
    # generate a simplex noise field in source-px space and bias threshold
    # DOWN by up to `amp` (never up), so the cutoff wobbles spatially and
    # more snow/rock bleeds through in noise-high regions. Keeps the baseline
    # threshold as the UPPER bound so it doesn't explode to more coverage.
    if threshold_noise_amp > 0.0 and threshold_noise_scale > 0.0:
        try:
            import opensimplex as ox
            ox.seed(int(threshold_noise_seed))
            # sx/sy already in source-px coords; downscale further by
            # threshold_noise_scale to get wavelength-sized waves.
            _xs = sx / float(threshold_noise_scale)
            _ys = sy / float(threshold_noise_scale)
            _n = ox.noise2array(_xs.astype(np.float64), _ys.astype(np.float64))
            _n = (_n + 1.0) * 0.5  # [-1,1] -> [0,1]
            T_field = float(threshold) - float(threshold_noise_amp) * _n.astype(np.float32)
        except ImportError:
            T_field = np.full(vals.shape, float(threshold), dtype=np.float32)
    else:
        T_field = float(threshold)

    # Threshold + optional dither
    if dither == "none" or dither_width <= 0.0:
        mask = (vals > T_field).astype(np.float32)
    else:
        lo = T_field - 0.5 * float(dither_width)
        hi = T_field + 0.5 * float(dither_width)
        # When T_field is an array, lo/hi are arrays too — elementwise ramp.
        _denom = (hi - lo) if np.ndim(hi) > 0 else max(hi - lo, 1e-9)
        prob = np.clip((vals - lo) / _denom, 0.0, 1.0)
        if dither == "white":
            seed_hash = (int(seed) * 2654435761
                         ^ int(eff_col) * 73856093
                         ^ int(eff_row) * 19349663) & 0xFFFFFFFF
            rng = np.random.default_rng(seed_hash)
            coin = rng.random((eff_h, eff_w)).astype(np.float32)
        elif dither == "blue_noise":
            bn = make_blue_noise_tile(blue_noise_size, seed=int(seed))
            ys = (eff_row + np.arange(eff_h, dtype=np.int64))[:, None] % blue_noise_size
            xs = (eff_col + np.arange(eff_w, dtype=np.int64))[None, :] % blue_noise_size
            coin = bn[ys, xs]
        else:
            raise ValueError(f"unknown dither mode: {dither!r}")
        mask = (prob > coin).astype(np.float32)

    return mask


# Config helper: normalizes a thresholds.json `gaea_gaps` block to a dict of
# per-mask kwargs. Orchestrators call this once and pass the results to
# `tile_streamer.read_tile`.
def build_gap_config(cfg_block: dict, masks_dir: Path | str) -> dict:
    """Convert a thresholds.json `gaea_gaps` config block into a dict of
    per-mask-name kwargs usable by `tile_streamer.read_tile`. Returns {} if
    query-time is disabled or the required 8k sources are missing."""
    if not cfg_block or not cfg_block.get("use_query_time", False):
        return {}
    masks_dir = Path(masks_dir)
    result: dict[str, dict] = {}
    slope_src = masks_dir / cfg_block.get("slope_source_8k", "gaea_slope_8k.tif")
    dust_src  = masks_dir / cfg_block.get("dusting_source_8k", "gaea_dusting_8k.tif")

    # Per-mask dither override falls back to the global `dither` if unset.
    _global_dither = cfg_block.get("dither", "none")
    common = dict(
        seed=cfg_block.get("seed", 42),
        blue_noise_size=cfg_block.get("blue_noise_size", 512),
    )
    if slope_src.exists():
        result["rock_gap"] = dict(
            source_8k_path=slope_src,
            threshold=float(cfg_block.get("slope_threshold", 52000.0)),
            dither_width=float(cfg_block.get("slope_dither_width", 18000.0)),
            dither=str(cfg_block.get("slope_dither", _global_dither)),
            threshold_noise_scale=float(cfg_block.get("slope_threshold_noise_scale", 0.0)),
            threshold_noise_amp=float(cfg_block.get("slope_threshold_noise_amp", 0.0)),
            threshold_noise_seed=int(cfg_block.get("slope_threshold_noise_seed", 43)),
            **common,
        )
    if dust_src.exists():
        result["snow_gap"] = dict(
            source_8k_path=dust_src,
            threshold=float(cfg_block.get("dusting_threshold", 1500.0)),
            dither_width=float(cfg_block.get("dusting_dither_width", 800.0)),
            dither=str(cfg_block.get("dusting_dither", _global_dither)),
            threshold_noise_scale=float(cfg_block.get("dusting_threshold_noise_scale", 0.0)),
            threshold_noise_amp=float(cfg_block.get("dusting_threshold_noise_amp", 0.0)),
            threshold_noise_seed=int(cfg_block.get("dusting_threshold_noise_seed", 44)),
            **common,
        )
    return result
