"""diag_gaea_gaps_debug.py — S56 fast Gaea slope/dusting threshold iteration.

Runs only the upscale + threshold + blue-noise dither logic on a single
tile window — no mask stitching (reads the already-written 8k TIFs) and
no full 50k upscale. ~0.5s per run, for fast iteration on threshold + dither
constants before committing to a full `py rebuild_gaea_gaps.py` cycle (~1-2 min).

Outputs per-tile PNGs for both slope (rock) and dusting (snow):

    rock_gap_{tx}_{tz}.png       binary mask after threshold+dither
    rock_prob_{tx}_{tz}.png      probability ramp (pre-dither) grayscale
    rock_src_{tx}_{tz}.png       upscaled continuous slope value (pre-threshold)
    snow_gap_{tx}_{tz}.png       (same three for dusting)
    snow_prob_{tx}_{tz}.png
    snow_src_{tx}_{tz}.png

Also dumps a `world_overview.png` — 1024x1024 downsampled view of both
stitched 8k masks side-by-side — useful for orientation checks (compare
against height.tif mentally).

Usage:
    py diag_gaea_gaps_debug.py --tile-x 24 --tile-z 80
    py diag_gaea_gaps_debug.py --tile-x 24 --tile-z 80 --slope-t 15000 --slope-d 3000
    py diag_gaea_gaps_debug.py --world
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.ndimage import zoom
from PIL import Image

from core.upscale import make_blue_noise_tile

# Defaults kept in sync with rebuild_gaea_gaps.py
TILE = 512
SRC_SIZE = 8192
TARGET_SIZE = 50000
SCALE = TARGET_SIZE / SRC_SIZE  # ~6.104

DEFAULT_SLOPE_T = 20000.0
DEFAULT_SLOPE_D = 5000.0
DEFAULT_DUSTING_T = 500.0
DEFAULT_DUSTING_D = 200.0


def _tile_upscale_and_dither(
    src_8k: np.ndarray,
    tile_x: int,
    tile_z: int,
    threshold: float,
    dither_width: float,
    blue_noise_size: int = 512,
    seed: int = 42,
    interp_order: int = 3,
) -> dict:
    """Run the same upscale + threshold + dither as core.upscale, but only on
    the source window that covers the requested 512x512 target tile. Returns
    `continuous`, `prob`, and `mask` arrays all at (TILE, TILE) target res.
    """
    y_dst_start = tile_z * TILE
    y_dst_end = y_dst_start + TILE
    x_dst_start = tile_x * TILE
    x_dst_end = x_dst_start + TILE

    pad_src = max(interp_order + 1, 4)
    y_src_s = max(0, int(np.floor(y_dst_start / SCALE)) - pad_src)
    y_src_e = min(SRC_SIZE, int(np.ceil(y_dst_end / SCALE)) + pad_src)
    x_src_s = max(0, int(np.floor(x_dst_start / SCALE)) - pad_src)
    x_src_e = min(SRC_SIZE, int(np.ceil(x_dst_end / SCALE)) + pad_src)

    chunk = src_8k[y_src_s:y_src_e, x_src_s:x_src_e].astype(np.float32, copy=False)
    zoomed = zoom(
        chunk, (SCALE, SCALE), order=interp_order, mode="reflect",
        prefilter=(interp_order > 1),
    )

    local_y = int(round(y_dst_start - y_src_s * SCALE))
    local_x = int(round(x_dst_start - x_src_s * SCALE))
    local_y = max(0, min(local_y, zoomed.shape[0] - TILE))
    local_x = max(0, min(local_x, zoomed.shape[1] - TILE))
    cont = zoomed[local_y : local_y + TILE, local_x : local_x + TILE]

    if cont.shape != (TILE, TILE):
        # Degenerate case near edges — pad with edge-replicate.
        pad_y = TILE - cont.shape[0]
        pad_x = TILE - cont.shape[1]
        if pad_y > 0:
            cont = np.pad(cont, ((0, pad_y), (0, 0)), mode="edge")
        if pad_x > 0:
            cont = np.pad(cont, ((0, 0), (0, pad_x)), mode="edge")

    t_lo = float(threshold) - 0.5 * float(dither_width)
    t_hi = float(threshold) + 0.5 * float(dither_width)
    prob = np.clip((cont - t_lo) / (t_hi - t_lo), 0.0, 1.0)

    bn = make_blue_noise_tile(blue_noise_size, seed=seed)
    ys = (y_dst_start + np.arange(TILE, dtype=np.int64))[:, None] % blue_noise_size
    xs = (x_dst_start + np.arange(TILE, dtype=np.int64))[None, :] % blue_noise_size
    bn_chunk = bn[ys, xs]

    mask = (prob > bn_chunk).astype(np.uint8)

    return {"continuous": cont, "prob": prob, "mask": mask}


def _render_src(cont: np.ndarray, out: Path, vmax: float | None = None) -> None:
    """Grayscale render of the continuous upscaled field. If vmax is None,
    autoscale to 95th percentile to make the interesting range visible."""
    if vmax is None:
        vmax = float(np.percentile(cont, 98))
        if vmax <= 0:
            vmax = float(cont.max()) if cont.max() > 0 else 1.0
    g = np.clip(cont / vmax, 0.0, 1.0)
    img = (g * 255).astype(np.uint8)
    Image.fromarray(img, mode="L").save(out)


def _render_prob(prob: np.ndarray, out: Path) -> None:
    img = (np.clip(prob, 0.0, 1.0) * 255).astype(np.uint8)
    Image.fromarray(img, mode="L").save(out)


def _render_mask(mask: np.ndarray, out: Path) -> None:
    """Binary mask as black/white with a subtle orange tint where ON, so the
    visual separation from the grayscale prob PNG is obvious when flicking
    between the two."""
    rgb = np.full((*mask.shape, 3), 30, dtype=np.uint8)  # dark bg
    on = mask > 0
    rgb[on] = (240, 180, 50)  # orange for ON
    Image.fromarray(rgb).save(out)


def _world_overview(
    slope_8k: np.ndarray,
    dusting_8k: np.ndarray,
    out: Path,
    slope_t: float,
    slope_d: float,
    dusting_t: float,
    dusting_d: float,
    downscale_to: int = 1024,
) -> None:
    """Quick downsampled 4-panel view:
    top-left:  slope continuous (autoscale),  top-right:  slope post-dither
    bot-left:  dusting continuous (autoscale), bot-right:  dusting post-dither

    Downsamples via block-mean (no fancy interpolation) — this is just for
    eyeballing orientation + relative distributions.
    """
    # Block-mean downsample: SRC_SIZE / downscale_to = 8 for 1024
    assert SRC_SIZE % downscale_to == 0, "downscale_to must divide 8192"
    f = SRC_SIZE // downscale_to

    def _blockmean(a: np.ndarray) -> np.ndarray:
        return a.reshape(downscale_to, f, downscale_to, f).mean(axis=(1, 3))

    slope_small = _blockmean(slope_8k.astype(np.float32))
    dusting_small = _blockmean(dusting_8k.astype(np.float32))

    # Threshold previews at downsampled res (no dither — just for shape check)
    slope_bin = (slope_small > slope_t).astype(np.uint8)
    dusting_bin = (dusting_small > dusting_t).astype(np.uint8)

    def _gray(a: np.ndarray) -> np.ndarray:
        vmax = float(np.percentile(a, 98)) or 1.0
        return (np.clip(a / vmax, 0, 1) * 255).astype(np.uint8)

    def _binary_tint(m: np.ndarray, color=(240, 180, 50)) -> np.ndarray:
        rgb = np.full((*m.shape, 3), 30, dtype=np.uint8)
        rgb[m > 0] = color
        return rgb

    top_left = np.stack([_gray(slope_small)] * 3, axis=-1)
    top_right = _binary_tint(slope_bin, color=(240, 180, 50))  # orange = rock
    bot_left = np.stack([_gray(dusting_small)] * 3, axis=-1)
    bot_right = _binary_tint(dusting_bin, color=(230, 240, 255))  # pale = snow

    H, W = downscale_to, downscale_to
    img = np.zeros((2 * H, 2 * W, 3), dtype=np.uint8)
    img[:H, :W] = top_left
    img[:H, W:] = top_right
    img[H:, :W] = bot_left
    img[H:, W:] = bot_right
    Image.fromarray(img).save(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tile-x", type=int, default=24)
    ap.add_argument("--tile-z", type=int, default=80)
    ap.add_argument("--masks", type=Path, default=Path("masks"))
    ap.add_argument("--out", type=Path, default=Path("diag_output/gaea_gaps"))
    ap.add_argument("--slope-t", type=float, default=DEFAULT_SLOPE_T, help="slope threshold")
    ap.add_argument("--slope-d", type=float, default=DEFAULT_SLOPE_D, help="slope dither width")
    ap.add_argument("--dusting-t", type=float, default=DEFAULT_DUSTING_T, help="dusting threshold")
    ap.add_argument("--dusting-d", type=float, default=DEFAULT_DUSTING_D, help="dusting dither width")
    ap.add_argument(
        "--world", action="store_true",
        help="Also emit world_overview.png (4-panel 1024x1024 view of full 8k)",
    )
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    tx, tz = args.tile_x, args.tile_z

    t0 = time.time()
    slope_path = args.masks / "gaea_slope_8k.tif"
    dusting_path = args.masks / "gaea_dusting_8k.tif"
    if not slope_path.exists() or not dusting_path.exists():
        raise SystemExit(
            f"Missing {slope_path} and/or {dusting_path}. "
            "Run `py rebuild_gaea_gaps.py --skip-50k` first."
        )
    with rasterio.open(str(slope_path)) as r:
        slope_8k = r.read(1)
    with rasterio.open(str(dusting_path)) as r:
        dusting_8k = r.read(1)
    t1 = time.time()

    rock = _tile_upscale_and_dither(slope_8k, tx, tz, args.slope_t, args.slope_d)
    snow = _tile_upscale_and_dither(dusting_8k, tx, tz, args.dusting_t, args.dusting_d)
    t2 = time.time()

    _render_src(rock["continuous"], args.out / f"rock_src_{tx}_{tz}.png")
    _render_prob(rock["prob"], args.out / f"rock_prob_{tx}_{tz}.png")
    _render_mask(rock["mask"], args.out / f"rock_gap_{tx}_{tz}.png")
    _render_src(snow["continuous"], args.out / f"snow_src_{tx}_{tz}.png")
    _render_prob(snow["prob"], args.out / f"snow_prob_{tx}_{tz}.png")
    _render_mask(snow["mask"], args.out / f"snow_gap_{tx}_{tz}.png")
    t3 = time.time()

    if args.world:
        _world_overview(
            slope_8k, dusting_8k, args.out / "world_overview.png",
            args.slope_t, args.slope_d, args.dusting_t, args.dusting_d,
        )
        t4 = time.time()
    else:
        t4 = t3

    rock_frac = float(rock["mask"].mean())
    snow_frac = float(snow["mask"].mean())

    print(f"Tile ({tx},{tz})")
    print(
        f"  rock:  T={args.slope_t:g}  width={args.slope_d:g}  "
        f"coverage={rock_frac*100:5.2f}%  "
        f"cont min={rock['continuous'].min():.0f} max={rock['continuous'].max():.0f} "
        f"p95={np.percentile(rock['continuous'], 95):.0f}"
    )
    print(
        f"  snow:  T={args.dusting_t:g}  width={args.dusting_d:g}  "
        f"coverage={snow_frac*100:5.2f}%  "
        f"cont min={snow['continuous'].min():.0f} max={snow['continuous'].max():.0f} "
        f"p95={np.percentile(snow['continuous'], 95):.0f}"
    )
    print()
    print(f"Load   : {t1-t0:.2f}s")
    print(f"Compute: {t2-t1:.2f}s")
    print(f"Render : {t3-t2:.2f}s")
    if args.world:
        print(f"World  : {t4-t3:.2f}s")
    print(f"TOTAL  : {t4-t0:.2f}s")
    print(f"Output : {args.out}")


if __name__ == "__main__":
    main()
