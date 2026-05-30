"""
core/upscale.py — Gaea-mask upscale helpers.

`upscale_continuous_then_threshold_dither()` upscales a low-res grayscale
source (e.g. 8192x8192 uint16 from Gaea) to the 50k target grid using a
continuous cubic spline, then thresholds at target resolution against a
tiled blue-noise texture so that the transition band is salt-and-pepper
with no visible low-frequency clumping.

Why this exists: thresholding at source resolution then upscaling the
binary mask produces staircasing (NEAREST) or phantom intermediates
(BILINEAR). Keeping the source continuous through the upscale, and
dithering only at the target resolution, gives hard rock/not-rock edges
with a natural sub-block roughness.

The blue-noise tile is a void-and-cluster-lite approximation: white noise
passed through a high-pass filter and rank-transformed to uniform [0,1].
Good enough for threshold dithering, and deterministic via `seed`.

Written for the S56 Gaea slope+dusting swap. See plan at
`.claude/plans/partitioned-napping-stonebraker.md`.
"""

from __future__ import annotations

from pathlib import Path
from functools import lru_cache

import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.ndimage import zoom, gaussian_filter


FULL_SIZE = 50000

_INTERP_TO_ORDER = {
    "nearest": 0,
    "bilinear": 1,
    "cubic": 3,
    "cubic_spline": 3,
    "quintic": 5,
    # No true lanczos in scipy.ndimage; quintic (order=5) is the closest
    # well-behaved substitute. Callers that specifically need lanczos can
    # switch to rasterio.warp.Resampling.lanczos in a future iteration.
    "lanczos": 5,
}

# Interpolation names that bypass scipy.ndimage.zoom and use custom kernels.
# Sentinel value (not a scipy order). See `_catmull_rom_zoom_2d` below.
_CUSTOM_INTERP = {"catmull_rom"}


def _catmull_rom_at_1d(src: np.ndarray, coords: np.ndarray, axis: int) -> np.ndarray:
    """Sample `src` along `axis` at explicit float positions `coords` (source-
    index space, can be fractional) via Catmull-Rom. Edge-replicate boundary.
    Returns float32. Used by the query-time gap sampler to sample the 8k Gaea
    source at non-integer positions derived from a tile's world coords."""
    N = src.shape[axis]
    coords = np.asarray(coords, dtype=np.float32)
    i = np.floor(coords).astype(np.int64)
    f = (coords - i).astype(np.float32)
    i_m1 = np.clip(i - 1, 0, N - 1)
    i_0  = np.clip(i,     0, N - 1)
    i_1  = np.clip(i + 1, 0, N - 1)
    i_2  = np.clip(i + 2, 0, N - 1)
    f2 = f * f
    f3 = f2 * f
    w_m1 = 0.5 * (-f + 2.0 * f2 - f3)
    w_0  = 0.5 * (2.0 - 5.0 * f2 + 3.0 * f3)
    w_1  = 0.5 * (f + 4.0 * f2 - 3.0 * f3)
    w_2  = 0.5 * (-f2 + f3)
    v_m1 = np.take(src, i_m1, axis=axis)
    v_0  = np.take(src, i_0,  axis=axis)
    v_1  = np.take(src, i_1,  axis=axis)
    v_2  = np.take(src, i_2,  axis=axis)
    shape = [1] * src.ndim
    shape[axis] = len(coords)
    w_m1 = w_m1.reshape(shape); w_0 = w_0.reshape(shape)
    w_1  = w_1.reshape(shape);  w_2 = w_2.reshape(shape)
    return (w_m1 * v_m1 + w_0 * v_0 + w_1 * v_1 + w_2 * v_2).astype(np.float32, copy=False)


def _catmull_rom_1d(src: np.ndarray, out_len: int, axis: int) -> np.ndarray:
    """Catmull-Rom (Keys a=-0.5) upsample of `src` along `axis` to `out_len`
    samples. Interpolating cubic — passes through source values at grid nodes,
    preserves sharp features better than B-spline but can slightly overshoot
    near binary edges. Edge-replicate boundary (clamp). Returns float32."""
    in_len = src.shape[axis]
    if out_len == in_len:
        return src.astype(np.float32, copy=False)
    # Target sample positions in source-index space, aligned so the two
    # grids share the same span endpoints.
    t = np.arange(out_len, dtype=np.float32) * (in_len - 1) / max(out_len - 1, 1)
    i = np.floor(t).astype(np.int64)
    f = (t - i).astype(np.float32)
    # 4-neighbor indices, clamped to [0, in_len-1]
    i_m1 = np.clip(i - 1, 0, in_len - 1)
    i_0  = np.clip(i,     0, in_len - 1)
    i_1  = np.clip(i + 1, 0, in_len - 1)
    i_2  = np.clip(i + 2, 0, in_len - 1)
    # Catmull-Rom weights: P(f) = 0.5 * sum(w_k * P_k)
    f2 = f * f
    f3 = f2 * f
    w_m1 = 0.5 * (-f + 2.0 * f2 - f3)
    w_0  = 0.5 * (2.0 - 5.0 * f2 + 3.0 * f3)
    w_1  = 0.5 * (f + 4.0 * f2 - 3.0 * f3)
    w_2  = 0.5 * (-f2 + f3)
    # Gather sampled rows/cols
    v_m1 = np.take(src, i_m1, axis=axis)
    v_0  = np.take(src, i_0,  axis=axis)
    v_1  = np.take(src, i_1,  axis=axis)
    v_2  = np.take(src, i_2,  axis=axis)
    # Broadcast weights along the sampled axis
    shape = [1] * src.ndim
    shape[axis] = out_len
    w_m1 = w_m1.reshape(shape); w_0 = w_0.reshape(shape)
    w_1  = w_1.reshape(shape);  w_2 = w_2.reshape(shape)
    return (w_m1 * v_m1 + w_0 * v_0 + w_1 * v_1 + w_2 * v_2).astype(np.float32, copy=False)


def _catmull_rom_zoom_2d(src: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    """2D Catmull-Rom upsample via separable 1D passes (X then Y). Returns
    float32. Memory footprint: `(in_h, out_w)` intermediate + `(out_h, out_w)` output."""
    stage_x = _catmull_rom_1d(src, out_w, axis=1)
    stage_y = _catmull_rom_1d(stage_x, out_h, axis=0)
    return stage_y


def _zoom_or_custom(chunk: np.ndarray, scale: float, interpolation: str,
                    out_h: int, out_w: int) -> np.ndarray:
    """Dispatcher: Catmull-Rom goes through the custom kernel; everything else
    uses scipy.ndimage.zoom with the B-spline order from _INTERP_TO_ORDER."""
    if interpolation == "catmull_rom":
        return _catmull_rom_zoom_2d(chunk, out_h, out_w)
    order = _INTERP_TO_ORDER[interpolation]
    return zoom(chunk, (scale, scale), order=order, mode="reflect",
                prefilter=(order > 1))


@lru_cache(maxsize=4)
def make_blue_noise_tile(size: int = 512, seed: int = 42, iterations: int = 6) -> np.ndarray:
    """Build a uniform [0,1] blue-noise-spectrum tile for dither thresholding.

    Technique: start with white noise, high-pass filter (subtract a blurred
    copy) a few times, then rank-transform to restore uniform distribution.
    The rank step preserves blue-noise spectrum while making values
    behave like uniform samples — suitable for probability thresholding.

    Returns a `(size, size)` float32 array in [0, 1], tile-able by modulo
    indexing. Cached across calls with the same parameters.
    """
    rng = np.random.default_rng(seed)
    n = rng.random((size, size)).astype(np.float32)
    for _ in range(iterations):
        low = gaussian_filter(n, sigma=1.5, mode="wrap")
        n = n - low
    # Rank transform → uniform [0,1]
    flat = n.ravel()
    ranks = flat.argsort().argsort().astype(np.float32)
    uniform = ranks / float(ranks.size - 1)
    return uniform.reshape(size, size)


def upscale_continuous_then_threshold_dither(
    src: np.ndarray,
    out_path: Path | str,
    *,
    threshold: float,
    dither_width: float,
    target_size: int = FULL_SIZE,
    interpolation: str = "cubic_spline",
    chunk_rows: int = 512,
    seed: int = 42,
    blue_noise_size: int = 512,
) -> None:
    """Upscale `src` to `target_size x target_size`, threshold with blue-noise
    dither in the band `[threshold - dither_width/2, threshold + dither_width/2]`,
    and stream the uint8 {0, 1} result to `out_path` as an LZW-compressed GeoTIFF.

    Parameters
    ----------
    src : np.ndarray
        Square 2D array (e.g. 8192x8192 uint16) — the continuous source.
    out_path : Path or str
        Destination TIF (uint8, 0 or 1).
    threshold : float
        Hard cutoff in source dtype units. Values above => 1.
    dither_width : float
        Width of the transition band in source dtype units. The band is
        centered on `threshold`; inside it, probability ramps 0→1 and is
        thresholded against blue-noise. Outside the band, result is
        fully 0 or 1 (no randomness).
    target_size : int
        Output side length. Default 50000 (FULL_SIZE).
    interpolation : str
        "cubic_spline" (default, order=3), "quintic"/"lanczos" (order=5),
        "bilinear" (order=1), or "nearest" (order=0).
    chunk_rows : int
        Target-space rows processed per streamed write. Default 512.
    seed, blue_noise_size : blue-noise tile parameters.

    Memory footprint: ~chunk_rows * target_size * 4 bytes (float32 during
    zoom) + a 50k-row column strip is avoided. For chunk_rows=512,
    target_size=50000: ~100MB per chunk, well under 8GB ceiling.
    """
    if src.ndim != 2 or src.shape[0] != src.shape[1]:
        raise ValueError(f"src must be square 2D; got shape {src.shape}")
    valid = set(_INTERP_TO_ORDER) | _CUSTOM_INTERP
    if interpolation not in valid:
        raise ValueError(
            f"interpolation={interpolation!r} not in {sorted(valid)}"
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    src_size = src.shape[0]
    scale = target_size / src_size
    order = _INTERP_TO_ORDER.get(interpolation, 3)  # padding uses order=3 ≥ Catmull-Rom's 4-tap radius
    pad_src = max(order + 1, 4)  # source-row padding to avoid edge artifacts per chunk

    bn = make_blue_noise_tile(blue_noise_size, seed=seed)

    t_lo = float(threshold) - 0.5 * float(dither_width)
    t_hi = float(threshold) + 0.5 * float(dither_width)
    if t_hi <= t_lo:
        raise ValueError("dither_width must be > 0")

    profile = {
        "driver": "GTiff",
        "width": target_size,
        "height": target_size,
        "count": 1,
        "dtype": "uint8",
        "compress": "lzw",
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
    }

    with rasterio.open(str(out_path), "w", **profile) as dst:
        y_dst = 0
        while y_dst < target_size:
            y_dst_end = min(y_dst + chunk_rows, target_size)
            rows = y_dst_end - y_dst

            # Source-row span covering this target chunk, padded for cubic context.
            y_src_start_f = y_dst / scale
            y_src_end_f = y_dst_end / scale
            y_src_start_i = max(0, int(np.floor(y_src_start_f)) - pad_src)
            y_src_end_i = min(src_size, int(np.ceil(y_src_end_f)) + pad_src)

            chunk_src = src[y_src_start_i:y_src_end_i].astype(np.float32, copy=False)

            # Zoom the padded chunk. Dispatch on interpolation: scipy zoom for
            # B-spline orders, custom Catmull-Rom kernel otherwise.
            chunk_h = chunk_src.shape[0]
            out_chunk_h = max(1, int(round(chunk_h * scale)))
            zoomed = _zoom_or_custom(chunk_src, scale, interpolation,
                                     out_h=out_chunk_h, out_w=target_size)

            # Row offset inside the zoomed chunk that corresponds to global y_dst.
            local_y_off = int(round(y_dst - y_src_start_i * scale))
            # Protect against off-by-one from the rounding in scipy's zoom
            local_y_off = max(0, min(local_y_off, zoomed.shape[0] - rows))
            out_chunk = zoomed[local_y_off : local_y_off + rows, :target_size]

            # Shape guard: if zoom produced a column short by 1-2 pixels from
            # floating rounding, pad via edge-replicate so we always emit the full width.
            if out_chunk.shape[1] < target_size:
                short = target_size - out_chunk.shape[1]
                pad_col = np.repeat(out_chunk[:, -1:], short, axis=1)
                out_chunk = np.concatenate([out_chunk, pad_col], axis=1)

            # Threshold + dither:
            # - below t_lo  → probability 0 → always 0
            # - above t_hi  → probability 1 → always 1
            # - in band     → probability ramps; threshold against blue-noise
            prob = np.clip((out_chunk - t_lo) / (t_hi - t_lo), 0.0, 1.0)

            # Tiled blue-noise sampling at global coords. No full tile expansion;
            # index by modulo.
            ys = (y_dst + np.arange(rows, dtype=np.int64))[:, None] % blue_noise_size
            xs = np.arange(target_size, dtype=np.int64)[None, :] % blue_noise_size
            bn_chunk = bn[ys, xs]

            mask = (prob > bn_chunk).astype(np.uint8)

            dst.write(mask, 1, window=Window(0, y_dst, target_size, rows))
            y_dst = y_dst_end


def upscale_continuous_then_multitier_dither(
    src_u: np.ndarray,
    out_path: Path | str,
    *,
    levels: tuple[float, ...] = (1.0, 2.0, 3.0),
    dither_u: float = 0.18,
    target_size: int = FULL_SIZE,
    interpolation: str = "catmull_rom",
    chunk_rows: int = 512,
    seed: int = 42,
    blue_noise_size: int = 512,
) -> None:
    """Multi-tier generalization of `upscale_continuous_then_threshold_dither`.

    `src_u` is a CONTINUOUS "tier coordinate" field (e.g. rock_layers: 0 = not
    rock, 1 = floor/dark start, 2 = dark/mid cut, 3 = mid/light cut). Upscale it
    continuously to `target_size`, then assign discrete tier indices at TARGET
    resolution by counting how many ordered `levels` each pixel's jittered value
    exceeds. The jitter is blue-noise of half-width `dither_u` (in u-units),
    applied only at target res — so every tier boundary is salt-and-pepper
    ragged with no staircasing or low-frequency clumping.

    Output uint8: 0 below levels[0]; k where levels[k-1] <= u < levels[k];
    len(levels) where u >= levels[-1].

    Same continuous-through-upscale principle as the binary case (see module
    docstring); this just thresholds against multiple ordered cuts instead of
    one. Use for rock_layers (dark/mid/light tiers) so the tier edges read as
    organic transitions, not 4-block NEAREST stamps.
    """
    if src_u.ndim != 2 or src_u.shape[0] != src_u.shape[1]:
        raise ValueError(f"src_u must be square 2D; got shape {src_u.shape}")
    valid = set(_INTERP_TO_ORDER) | _CUSTOM_INTERP
    if interpolation not in valid:
        raise ValueError(f"interpolation={interpolation!r} not in {sorted(valid)}")
    levels = tuple(float(l) for l in levels)
    if any(b <= a for a, b in zip(levels, levels[1:])):
        raise ValueError(f"levels must be strictly ascending; got {levels}")
    if len(levels) > 255:
        raise ValueError("too many tier levels for uint8 output")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    src_size = src_u.shape[0]
    scale = target_size / src_size
    order = _INTERP_TO_ORDER.get(interpolation, 3)
    pad_src = max(order + 1, 4)

    bn = make_blue_noise_tile(blue_noise_size, seed=seed)
    levels_arr = np.asarray(levels, dtype=np.float32)

    profile = {
        "driver": "GTiff",
        "width": target_size,
        "height": target_size,
        "count": 1,
        "dtype": "uint8",
        "compress": "lzw",
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
    }

    with rasterio.open(str(out_path), "w", **profile) as dst:
        y_dst = 0
        while y_dst < target_size:
            y_dst_end = min(y_dst + chunk_rows, target_size)
            rows = y_dst_end - y_dst

            y_src_start_f = y_dst / scale
            y_src_end_f = y_dst_end / scale
            y_src_start_i = max(0, int(np.floor(y_src_start_f)) - pad_src)
            y_src_end_i = min(src_size, int(np.ceil(y_src_end_f)) + pad_src)

            chunk_src = src_u[y_src_start_i:y_src_end_i].astype(np.float32, copy=False)
            chunk_h = chunk_src.shape[0]
            out_chunk_h = max(1, int(round(chunk_h * scale)))
            zoomed = _zoom_or_custom(chunk_src, scale, interpolation,
                                     out_h=out_chunk_h, out_w=target_size)

            local_y_off = int(round(y_dst - y_src_start_i * scale))
            local_y_off = max(0, min(local_y_off, zoomed.shape[0] - rows))
            out_chunk = zoomed[local_y_off : local_y_off + rows, :target_size]

            if out_chunk.shape[1] < target_size:
                short = target_size - out_chunk.shape[1]
                pad_col = np.repeat(out_chunk[:, -1:], short, axis=1)
                out_chunk = np.concatenate([out_chunk, pad_col], axis=1)

            # Blue-noise jitter at TARGET res: shift u by ±dither_u so tier
            # boundaries become ragged instead of clean iso-u contours.
            ys = (y_dst + np.arange(rows, dtype=np.int64))[:, None] % blue_noise_size
            xs = np.arange(target_size, dtype=np.int64)[None, :] % blue_noise_size
            bn_chunk = bn[ys, xs]
            u_jit = out_chunk + (bn_chunk - 0.5) * (2.0 * float(dither_u))

            # tier = count of levels exceeded (0..len(levels))
            tier = (u_jit[..., None] >= levels_arr).sum(axis=-1).astype(np.uint8)

            dst.write(tier, 1, window=Window(0, y_dst, target_size, rows))
            y_dst = y_dst_end


def upscale_continuous(
    src: np.ndarray,
    out_path: Path | str,
    *,
    target_size: int = FULL_SIZE,
    interpolation: str = "cubic_spline",
    chunk_rows: int = 512,
    dtype: str = "uint16",
) -> None:
    """Upscale a continuous grayscale source to `target_size` without thresholding.

    Companion to `upscale_continuous_then_threshold_dither` for cases where
    we want the continuous upscaled field itself (e.g. Phase B height regen).
    Streams to disk in chunks; preserves source dtype range by casting at
    write time with rounding.
    """
    if src.ndim != 2 or src.shape[0] != src.shape[1]:
        raise ValueError(f"src must be square 2D; got shape {src.shape}")
    valid = set(_INTERP_TO_ORDER) | _CUSTOM_INTERP
    if interpolation not in valid:
        raise ValueError(f"interpolation={interpolation!r} not in {sorted(valid)}")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    src_size = src.shape[0]
    scale = target_size / src_size
    order = _INTERP_TO_ORDER.get(interpolation, 3)
    pad_src = max(order + 1, 4)

    np_dtype = np.dtype(dtype)

    profile = {
        "driver": "GTiff",
        "width": target_size,
        "height": target_size,
        "count": 1,
        "dtype": np_dtype,
        "compress": "lzw",
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
    }

    with rasterio.open(str(out_path), "w", **profile) as dst:
        y_dst = 0
        while y_dst < target_size:
            y_dst_end = min(y_dst + chunk_rows, target_size)
            rows = y_dst_end - y_dst

            y_src_start_f = y_dst / scale
            y_src_end_f = y_dst_end / scale
            y_src_start_i = max(0, int(np.floor(y_src_start_f)) - pad_src)
            y_src_end_i = min(src_size, int(np.ceil(y_src_end_f)) + pad_src)

            chunk_src = src[y_src_start_i:y_src_end_i].astype(np.float32, copy=False)
            chunk_h = chunk_src.shape[0]
            out_chunk_h = max(1, int(round(chunk_h * scale)))
            zoomed = _zoom_or_custom(chunk_src, scale, interpolation,
                                     out_h=out_chunk_h, out_w=target_size)

            local_y_off = int(round(y_dst - y_src_start_i * scale))
            local_y_off = max(0, min(local_y_off, zoomed.shape[0] - rows))
            out_chunk = zoomed[local_y_off : local_y_off + rows, :target_size]

            if out_chunk.shape[1] < target_size:
                short = target_size - out_chunk.shape[1]
                pad_col = np.repeat(out_chunk[:, -1:], short, axis=1)
                out_chunk = np.concatenate([out_chunk, pad_col], axis=1)

            # Clip to source dtype range before casting to avoid wrap-around
            if np.issubdtype(np_dtype, np.integer):
                info = np.iinfo(np_dtype)
                out_chunk = np.clip(out_chunk, info.min, info.max)

            dst.write(out_chunk.astype(np_dtype), 1, window=Window(0, y_dst, target_size, rows))
            y_dst = y_dst_end
