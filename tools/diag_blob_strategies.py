"""Compare blob-reduction strategies on rock_gap + snow_gap.

Four threshold-decision strategies are generated on an alpine-ridge crop
(3×3 tiles around 25,80) using the same Catmull-Rom upscale. Writes
memory/blob_strategies.png for eyeballing:

  A. Current (linear ramp, narrow band, blue-noise dither)
  B. Widened dither band (~2.2x wider; linear ramp; blue-noise)
  C. Full-range sigmoid (soft asymptote to 0 and 1; blue-noise)
  D. Hard threshold + scattered interior flip (binary + white-noise flip)

Strategy D asymmetry: flip rate depends on how far the pixel's continuous
value sits from threshold. Near threshold → high flip; far from threshold
→ low flip. Gaussian shape.

No production code touched — purely a visual A/B.

Usage:
    py tools/diag_blob_strategies.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKTREE))

from core.upscale import _zoom_or_custom, make_blue_noise_tile  # noqa: E402
import importlib.util
spec = importlib.util.spec_from_file_location("rebuild_gaea_gaps", str(_WORKTREE / "rebuild_gaea_gaps.py"))
rgg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rgg)

OUT = _WORKTREE / "memory" / "blob_strategies.png"

SLOPE_T, SLOPE_W_A = 52000.0, 18000.0   # current width (baseline)
SLOPE_W_B = 40000.0                      # strategy B: widened ~2.2x
SLOPE_SIGMA = 9000.0                     # strategy C: sigmoid shape
SLOPE_FLIP_SIGMA = 12000.0               # strategy D: flip gaussian width
SLOPE_FLIP_MAX = 0.35                    # peak flip probability at threshold

SNOW_T, SNOW_W_A = 1500.0, 800.0
SNOW_W_B = 1800.0
SNOW_SIGMA = 400.0
SNOW_FLIP_SIGMA = 600.0
SNOW_FLIP_MAX = 0.35


def strategy_A(vals, T, W, bn, x0, y0):
    """Current default: linear ramp width W, blue-noise dither."""
    lo = T - 0.5 * W; hi = T + 0.5 * W
    prob = np.clip((vals - lo) / (hi - lo), 0.0, 1.0)
    ys = (y0 + np.arange(vals.shape[0]))[:, None] % bn.shape[0]
    xs = (x0 + np.arange(vals.shape[1]))[None, :] % bn.shape[1]
    return (prob > bn[ys, xs]).astype(np.uint8)


def strategy_B(vals, T, W_wider, bn, x0, y0):
    """Widened linear ramp."""
    return strategy_A(vals, T, W_wider, bn, x0, y0)


def strategy_C(vals, T, sigma, bn, x0, y0):
    """Sigmoid probability — asymptotic, no hard edges."""
    # prob = 1 / (1 + exp(-(v - T) / sigma))
    prob = 1.0 / (1.0 + np.exp(-(vals - T) / sigma))
    ys = (y0 + np.arange(vals.shape[0]))[:, None] % bn.shape[0]
    xs = (x0 + np.arange(vals.shape[1]))[None, :] % bn.shape[1]
    return (prob > bn[ys, xs]).astype(np.uint8)


def strategy_D(vals, T, flip_sigma, flip_max, seed, x0, y0):
    """Hard threshold + proximity-weighted interior flip.
    flip_prob(v) = flip_max * exp(-(v - T)^2 / (2*flip_sigma^2))  (gaussian bump)
    — highest near threshold, decays in both directions.
    Base decision is hard binary; flip overlay flips a proportional fraction.
    """
    base = (vals > T).astype(np.uint8)
    flip_prob = flip_max * np.exp(-((vals - T) ** 2) / (2.0 * flip_sigma ** 2))
    seed_hash = (int(seed) * 2654435761 ^ int(x0) * 73856093 ^ int(y0) * 19349663) & 0xFFFFFFFF
    rng = np.random.default_rng(seed_hash)
    coin = rng.random(vals.shape, dtype=np.float32)
    flip_mask = coin < flip_prob
    return (base ^ flip_mask.astype(np.uint8)).astype(np.uint8)


def main() -> int:
    # Crop covering (24,80) through (26,82) — 3×3 alpine ridge
    tx0, tz0, tx1, tz1 = 23, 79, 26, 82
    tgt_x0, tgt_x1 = tx0 * 512, tx1 * 512
    tgt_z0, tgt_z1 = tz0 * 512, tz1 * 512
    tgt_h, tgt_w = tgt_z1 - tgt_z0, tgt_x1 - tgt_x0
    SCALE = 50000.0 / 8192.0
    SRC_SCALE = 1.0 / SCALE
    pad = 8

    # Source crop
    src_x0 = max(0, int(np.floor(tgt_x0 * SRC_SCALE)) - pad)
    src_x1 = min(8192, int(np.ceil(tgt_x1 * SRC_SCALE)) + pad)
    src_z0 = max(0, int(np.floor(tgt_z0 * SRC_SCALE)) - pad)
    src_z1 = min(8192, int(np.ceil(tgt_z1 * SRC_SCALE)) + pad)

    print("[blob_strategies] stitching 8k Gaea sources...", flush=True)
    slope_8k = rgg._stitch_tiles(rgg.SLOPE_DIR, "Slope_Out")
    dusting_8k = rgg._stitch_tiles(rgg.DUSTING_DIR, "Dusting_Out")

    slope_crop = slope_8k[src_z0:src_z1, src_x0:src_x1].astype(np.float32)
    dusting_crop = dusting_8k[src_z0:src_z1, src_x0:src_x1].astype(np.float32)

    # Upscale to target crop
    src_h, src_w = slope_crop.shape
    out_h = int(round(src_h * SCALE))
    out_w = int(round(src_w * SCALE))
    print(f"[blob_strategies] upscaling (src={slope_crop.shape} -> out=({out_h},{out_w})) ...", flush=True)
    slope_zoomed   = _zoom_or_custom(slope_crop,   SCALE, "catmull_rom", out_h=out_h, out_w=out_w)
    dusting_zoomed = _zoom_or_custom(dusting_crop, SCALE, "catmull_rom", out_h=out_h, out_w=out_w)

    # Slice back to (tgt_h, tgt_w)
    off_z = int(round(tgt_z0 - src_z0 * SCALE))
    off_x = int(round(tgt_x0 - src_x0 * SCALE))
    off_z = max(0, min(off_z, slope_zoomed.shape[0] - tgt_h))
    off_x = max(0, min(off_x, slope_zoomed.shape[1] - tgt_w))
    slope = slope_zoomed[off_z:off_z+tgt_h, off_x:off_x+tgt_w]
    dust  = dusting_zoomed[off_z:off_z+tgt_h, off_x:off_x+tgt_w]

    bn = make_blue_noise_tile(512, seed=42)

    # Generate 4 strategies per mask
    print("[blob_strategies] rock:", flush=True)
    rock_A = strategy_A(slope, SLOPE_T, SLOPE_W_A, bn, tgt_x0, tgt_z0)
    rock_B = strategy_B(slope, SLOPE_T, SLOPE_W_B, bn, tgt_x0, tgt_z0)
    rock_C = strategy_C(slope, SLOPE_T, SLOPE_SIGMA, bn, tgt_x0, tgt_z0)
    rock_D = strategy_D(slope, SLOPE_T, SLOPE_FLIP_SIGMA, SLOPE_FLIP_MAX, 42, tgt_x0, tgt_z0)
    for lbl, m in [("A current", rock_A), ("B wider", rock_B), ("C sigmoid", rock_C), ("D hard+flip", rock_D)]:
        print(f"  {lbl:14s}: coverage = {100.0 * m.mean():.2f}%")

    print("[blob_strategies] snow:", flush=True)
    snow_A = strategy_A(dust, SNOW_T, SNOW_W_A, bn, tgt_x0, tgt_z0)
    snow_B = strategy_B(dust, SNOW_T, SNOW_W_B, bn, tgt_x0, tgt_z0)
    snow_C = strategy_C(dust, SNOW_T, SNOW_SIGMA, bn, tgt_x0, tgt_z0)
    snow_D = strategy_D(dust, SNOW_T, SNOW_FLIP_SIGMA, SNOW_FLIP_MAX, 43, tgt_x0, tgt_z0)
    for lbl, m in [("A current", snow_A), ("B wider", snow_B), ("C sigmoid", snow_C), ("D hard+flip", snow_D)]:
        print(f"  {lbl:14s}: coverage = {100.0 * m.mean():.2f}%")

    # Render PNG
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 4, figsize=(20, 10))
    rock_titles = [
        f"A. Current\n(linear ramp W={SLOPE_W_A:.0f}, blue-noise)  cov={100*rock_A.mean():.1f}%",
        f"B. Widened band\n(W={SLOPE_W_B:.0f}, blue-noise)  cov={100*rock_B.mean():.1f}%",
        f"C. Full-range sigmoid\n(sigma={SLOPE_SIGMA:.0f}, blue-noise)  cov={100*rock_C.mean():.1f}%",
        f"D. Hard threshold + gaussian-bump flip\n(sigma={SLOPE_FLIP_SIGMA:.0f}, max flip={SLOPE_FLIP_MAX:.0%})  cov={100*rock_D.mean():.1f}%",
    ]
    snow_titles = [
        f"A. Current  cov={100*snow_A.mean():.1f}%",
        f"B. Widened  cov={100*snow_B.mean():.1f}%",
        f"C. Sigmoid  cov={100*snow_C.mean():.1f}%",
        f"D. Hard+flip  cov={100*snow_D.mean():.1f}%",
    ]

    for ax, img, title in zip(axes[0], [rock_A, rock_B, rock_C, rock_D], rock_titles):
        ax.imshow(img, cmap="gray_r", vmin=0, vmax=1, interpolation="nearest")
        ax.set_title(title, fontsize=9)
        ax.set_xticks([]); ax.set_yticks([])
    for ax, img, title in zip(axes[1], [snow_A, snow_B, snow_C, snow_D], snow_titles):
        ax.imshow(img, cmap="Blues", vmin=0, vmax=1, interpolation="nearest")
        ax.set_title("SNOW: " + title, fontsize=9)
        ax.set_xticks([]); ax.set_yticks([])

    fig.suptitle(
        f"Blob-reduction strategy A/B — tiles ({tx0},{tz0})..({tx1-1},{tz1-1}) alpine ridge",
        fontsize=13,
    )
    fig.tight_layout()
    OUT.parent.mkdir(exist_ok=True)
    fig.savefig(OUT, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"[blob_strategies] wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
