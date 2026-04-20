"""Raw kernel A/B — drop the blue-noise dither, show continuous + hard-threshold
output of cubic_spline vs catmull_rom upscale on an alpine ridge crop.

Output: memory/kernel_raw_compare.png

8-panel figure:
  row 0: continuous rock mask (cubic_spline | catmull_rom | diff)
  row 1: continuous snow mask (cubic_spline | catmull_rom | diff)
  plus 2 threshold-only binary panels (no dither) for rock.

Crop: tiles (23-26, 79-82) — alpine ridge near (24,80) / (25,80).

Usage:
    py tools/diag_kernel_raw_compare.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

_WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKTREE))
from core.upscale import _zoom_or_custom  # noqa: E402

# Import _stitch_tiles lazily — rebuild_gaea_gaps is at repo root
sys.path.insert(0, str(_WORKTREE))
import importlib.util
spec = importlib.util.spec_from_file_location(
    "rebuild_gaea_gaps", str(_WORKTREE / "rebuild_gaea_gaps.py"),
)
rgg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rgg)


OUT = _WORKTREE / "memory" / "kernel_raw_compare.png"

SRC_SIZE = 8192
TARGET_SIZE = 50000
SCALE = TARGET_SIZE / SRC_SIZE   # ~6.10

SLOPE_T = 52000.0
SNOW_T = 1500.0


def main() -> int:
    # Tile crop: (23,79) through (25,81) inclusive — 3×3 tiles, 1536×1536 target pixels
    tx0, tz0, tx1, tz1 = 23, 79, 26, 82
    tgt_x0 = tx0 * 512
    tgt_x1 = tx1 * 512
    tgt_z0 = tz0 * 512
    tgt_z1 = tz1 * 512
    tgt_h = tgt_z1 - tgt_z0
    tgt_w = tgt_x1 - tgt_x0

    # Source crop corresponding to that target region, with 8-px padding for cubic context.
    pad = 8
    src_x0 = max(0, int(np.floor(tgt_x0 / SCALE)) - pad)
    src_x1 = min(SRC_SIZE, int(np.ceil(tgt_x1 / SCALE)) + pad)
    src_z0 = max(0, int(np.floor(tgt_z0 / SCALE)) - pad)
    src_z1 = min(SRC_SIZE, int(np.ceil(tgt_z1 / SCALE)) + pad)
    src_h = src_z1 - src_z0
    src_w = src_x1 - src_x0

    print(f"[kernel_raw] stitching 8k Gaea masks (slope + dusting) ...", flush=True)
    t0 = time.time()
    slope_8k = rgg._stitch_tiles(rgg.SLOPE_DIR, "Slope_Out")
    dusting_8k = rgg._stitch_tiles(rgg.DUSTING_DIR, "Dusting_Out")
    print(f"  stitched in {time.time()-t0:.1f}s", flush=True)

    slope_crop = slope_8k[src_z0:src_z1, src_x0:src_x1].astype(np.float32)
    dusting_crop = dusting_8k[src_z0:src_z1, src_x0:src_x1].astype(np.float32)

    # Target crop dimensions at scale (may be slightly larger than tgt_h/tgt_w)
    out_crop_h = int(round(src_h * SCALE))
    out_crop_w = int(round(src_w * SCALE))

    def _run(src_crop: np.ndarray, kernel: str) -> np.ndarray:
        """Upscale a crop via _zoom_or_custom, then slice back to (tgt_h, tgt_w)."""
        t0 = time.time()
        zoomed = _zoom_or_custom(src_crop, SCALE, kernel, out_h=out_crop_h, out_w=out_crop_w)
        # Offset within the zoomed crop corresponding to the target slice
        off_z = int(round(tgt_z0 - src_z0 * SCALE))
        off_x = int(round(tgt_x0 - src_x0 * SCALE))
        off_z = max(0, min(off_z, zoomed.shape[0] - tgt_h))
        off_x = max(0, min(off_x, zoomed.shape[1] - tgt_w))
        out = zoomed[off_z:off_z + tgt_h, off_x:off_x + tgt_w]
        print(f"  {kernel:14s}: {time.time()-t0:.2f}s  min={out.min():.0f} max={out.max():.0f}", flush=True)
        return out

    print("[kernel_raw] rock (slope) upscales:", flush=True)
    rock_cubic   = _run(slope_crop, "cubic_spline")
    rock_catmull = _run(slope_crop, "catmull_rom")
    print("[kernel_raw] snow (dusting) upscales:", flush=True)
    snow_cubic   = _run(dusting_crop, "cubic_spline")
    snow_catmull = _run(dusting_crop, "catmull_rom")

    # Hard-threshold binary (no dither, no band)
    rock_bin_cubic   = (rock_cubic   > SLOPE_T).astype(np.uint8)
    rock_bin_catmull = (rock_catmull > SLOPE_T).astype(np.uint8)
    snow_bin_cubic   = (snow_cubic   > SNOW_T).astype(np.uint8)
    snow_bin_catmull = (snow_catmull > SNOW_T).astype(np.uint8)

    # Numerical comparison
    for label, a, b in [
        ("rock continuous", rock_cubic, rock_catmull),
        ("snow continuous", snow_cubic, snow_catmull),
    ]:
        diff = b - a
        print(f"  {label}: mean|diff|={np.mean(np.abs(diff)):.1f}  max|diff|={np.max(np.abs(diff)):.0f}  overshoot (catmull outside [0, 65535])={int(((b<0)|(b>65535)).sum())} px")
    for label, a, b in [
        ("rock hard-threshold", rock_bin_cubic, rock_bin_catmull),
        ("snow hard-threshold", snow_bin_cubic, snow_bin_catmull),
    ]:
        disagree = int((a != b).sum())
        print(f"  {label}: cubic={100.0*a.sum()/a.size:.3f}%  catmull={100.0*b.sum()/b.size:.3f}%  disagree={disagree:,} ({100.0*disagree/a.size:.3f}%)")

    # Render 8-panel PNG
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 4, figsize=(18, 14))

    # Row 0: continuous rock
    # Clip gray range to [T-width/2, T+width/2] band so we SEE the transition, not clipped pure black/white
    band = 18000.0
    vmin_r, vmax_r = SLOPE_T - band, SLOPE_T + band
    for ax, img, title in [
        (axes[0, 0], rock_cubic,   f"Rock continuous — cubic_spline (band ±{band/2:.0f})"),
        (axes[0, 1], rock_catmull, f"Rock continuous — catmull_rom (band ±{band/2:.0f})"),
    ]:
        ax.imshow(img, cmap="gray_r", vmin=vmin_r, vmax=vmax_r, interpolation="nearest")
        ax.set_title(title, fontsize=10); ax.set_xticks([]); ax.set_yticks([])
    # Diff map rock
    diff_rock = rock_catmull - rock_cubic
    lim_r = float(np.max(np.abs(diff_rock))) or 1.0
    axes[0, 2].imshow(diff_rock, cmap="RdBu_r", vmin=-lim_r, vmax=lim_r, interpolation="nearest")
    axes[0, 2].set_title(f"Rock diff (catmull - cubic)  |max|={lim_r:.0f}", fontsize=10)
    axes[0, 2].set_xticks([]); axes[0, 2].set_yticks([])

    # Row 0 col 3: binary threshold XOR for rock
    xor_rock = (rock_bin_cubic ^ rock_bin_catmull)
    axes[0, 3].imshow(xor_rock, cmap="Reds", vmin=0, vmax=1, interpolation="nearest")
    axes[0, 3].set_title(f"Rock hard-threshold XOR (red = disagree)", fontsize=10)
    axes[0, 3].set_xticks([]); axes[0, 3].set_yticks([])

    # Row 1: continuous snow
    band_s = 800.0
    vmin_s, vmax_s = SNOW_T - band_s, SNOW_T + band_s
    for ax, img, title in [
        (axes[1, 0], snow_cubic,   f"Snow continuous — cubic_spline (band ±{band_s/2:.0f})"),
        (axes[1, 1], snow_catmull, f"Snow continuous — catmull_rom (band ±{band_s/2:.0f})"),
    ]:
        ax.imshow(img, cmap="gray_r", vmin=vmin_s, vmax=vmax_s, interpolation="nearest")
        ax.set_title(title, fontsize=10); ax.set_xticks([]); ax.set_yticks([])
    diff_snow = snow_catmull - snow_cubic
    lim_s = float(np.max(np.abs(diff_snow))) or 1.0
    axes[1, 2].imshow(diff_snow, cmap="RdBu_r", vmin=-lim_s, vmax=lim_s, interpolation="nearest")
    axes[1, 2].set_title(f"Snow diff (catmull - cubic)  |max|={lim_s:.0f}", fontsize=10)
    axes[1, 2].set_xticks([]); axes[1, 2].set_yticks([])
    xor_snow = (snow_bin_cubic ^ snow_bin_catmull)
    axes[1, 3].imshow(xor_snow, cmap="Reds", vmin=0, vmax=1, interpolation="nearest")
    axes[1, 3].set_title(f"Snow hard-threshold XOR (red = disagree)", fontsize=10)
    axes[1, 3].set_xticks([]); axes[1, 3].set_yticks([])

    # Row 2: binary hard-threshold (no dither)
    for ax, img, title in [
        (axes[2, 0], rock_bin_cubic,   "Rock binary — cubic_spline (no dither)"),
        (axes[2, 1], rock_bin_catmull, "Rock binary — catmull_rom (no dither)"),
        (axes[2, 2], snow_bin_cubic,   "Snow binary — cubic_spline (no dither)"),
        (axes[2, 3], snow_bin_catmull, "Snow binary — catmull_rom (no dither)"),
    ]:
        ax.imshow(img, cmap="gray_r", vmin=0, vmax=1, interpolation="nearest")
        ax.set_title(title, fontsize=10); ax.set_xticks([]); ax.set_yticks([])

    fig.suptitle(
        f"Raw kernel comparison — tiles ({tx0},{tz0})..({tx1-1},{tz1-1})  crop {tgt_w}×{tgt_h} px",
        fontsize=13,
    )
    fig.tight_layout()
    OUT.parent.mkdir(exist_ok=True)
    fig.savefig(OUT, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"[kernel_raw] wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
