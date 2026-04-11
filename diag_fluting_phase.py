"""diag_fluting_phase.py — Phase 0 stub (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §7 Secondary Tools #2 and §8
"Vertical Fluting — Detailed Spec".

Renders the cliff-tangent direction (as hue) and stripe phase (as value)
across a tile so we can tune vertical fluting tangent math BEFORE feeding
the variant hint into Pass 1's column writer.

Phase 0 STUB: emits a synthetic tangent field from a fake heightmap so the
output PNG demonstrates the intended visualization (HSV: hue = tangent
angle, value = phase mod stripe_width).

CLI:
    py diag_fluting_phase.py --tile-x 36 --tile-z 20 \
        --out diag_output/36_20/fluting_phase.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tile-x", type=int, default=36)
    p.add_argument("--tile-z", type=int, default=20)
    p.add_argument("--size", type=int, default=512)
    p.add_argument("--stripe-width", type=int, default=4)
    p.add_argument("--out", type=str,
                   default="diag_output/{tx}_{tz}/fluting_phase.png")
    return p.parse_args()


def _hsv_to_rgb(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Vectorized HSV → RGB, all inputs in [0, 1]. Returns uint8 (H, W, 3)."""
    i = (h * 6.0).astype(np.int32) % 6
    f = h * 6.0 - (h * 6.0).astype(np.int32)
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    r = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [v, q, p, p, t, v])
    g = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [t, v, v, q, p, p])
    b = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [p, p, t, v, v, q])
    rgb = np.stack([r, g, b], axis=-1)
    return (rgb * 255).clip(0, 255).astype(np.uint8)


def render_fluting_stub(
    tile_x: int, tile_z: int, size: int = 512, stripe_width: int = 4,
) -> np.ndarray:
    # Fake smooth heightmap with multiple "cliff" features.
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
    fake_h = (
        200 + 30 * np.sin(xs / 50.0 + tile_x)
        + 25 * np.cos(ys / 60.0 + tile_z)
        + 15 * np.sin((xs + ys) / 40.0)
    )
    gy, gx = np.gradient(fake_h)
    # Tangent = perpendicular to gradient in the horizontal plane.
    tan_x = -gy
    tan_z = gx
    tan_len = np.hypot(tan_x, tan_z) + 1e-9
    tan_x /= tan_len
    tan_z /= tan_len

    # Hue = tangent angle, value = phase mod stripe_width.
    angle = np.arctan2(tan_z, tan_x)  # [-π, π]
    hue = (angle + np.pi) / (2 * np.pi)  # [0, 1]
    phase = (xs * tan_x + ys * tan_z)
    val = (np.mod(phase, stripe_width) / stripe_width).astype(np.float32)
    sat = np.full_like(hue, 0.85, dtype=np.float32)
    return _hsv_to_rgb(hue.astype(np.float32), sat, val)


def _save_png(img: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        Image.fromarray(img, mode="RGB").save(path)
    except ImportError:
        np.save(path.with_suffix(".npy"), img)


def main() -> int:
    args = _parse_args()
    out_path = Path(args.out.format(tx=args.tile_x, tz=args.tile_z))
    img = render_fluting_stub(args.tile_x, args.tile_z, args.size, args.stripe_width)
    _save_png(img, out_path)
    print(f"[diag_fluting_phase] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
