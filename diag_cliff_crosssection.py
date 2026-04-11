"""diag_cliff_crosssection.py — Phase 0 stub (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §7 "Secondary tools" and §8
"Vertical Fluting — Detailed Spec".

Renders a vertical slice of a tile along a 2-endpoint sample line so Claude
and Nick can eyeball lithology stacks, soil horizon depth, river-bed
geometry, and (critically) vertical fluting stripes WITHOUT flying into
Minecraft.

Phase 0 STUB behavior: emits a placeholder PNG from a synthetic column
stack, proving the I/O path works end-to-end. The real implementation
(Phase 1+) reads `column_output` from the pipeline.

CLI:
    py diag_cliff_crosssection.py --tile-x 36 --tile-z 20 \
        --line 100 100 400 400 --out diag_output/36_20/cliff_xsec.png
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tile-x", type=int, default=36)
    p.add_argument("--tile-z", type=int, default=20)
    p.add_argument("--line", type=int, nargs=4, metavar=("X0", "Z0", "X1", "Z1"),
                   default=(100, 100, 400, 400))
    p.add_argument("--out", type=str,
                   default="diag_output/{tx}_{tz}/cliff_crosssection.png")
    return p.parse_args()


def render_crosssection_stub(
    tile_x: int, tile_z: int, line: tuple[int, int, int, int],
) -> np.ndarray:
    """Synthesize a placeholder cross-section RGB image.

    Height axis = Y (MC block Y, 448 down to -64 = 512 rows).
    Width axis = distance along the sample line.

    Draws a deterministic lithology-stack pattern so Nick can verify the
    PNG output path, color legend, and axis orientation before the real
    column reader lands in Phase 1.
    """
    x0, z0, x1, z1 = line
    length = int(max(abs(x1 - x0), abs(z1 - z0)) + 1)
    height = 512  # y range -64..447

    # RGB canvas.
    img = np.zeros((height, length, 3), dtype=np.uint8)

    # Mock surface profile (smooth wave) so stripes show up.
    xs = np.linspace(0, np.pi * 2, length)
    surface_y = (180 + 80 * np.sin(xs * 1.5) + 30 * np.cos(xs * 3.7)).astype(int)

    # Lithology palette: bedrock / basement / sediment / soil / air.
    PAL = {
        "bedrock": (40, 40, 48),
        "deepslate": (70, 70, 80),
        "basement": (110, 110, 120),
        "sediment": (160, 140, 100),
        "soil": (90, 60, 40),
        "surface": (80, 130, 60),
        "air": (200, 220, 240),
    }

    for col in range(length):
        sy = surface_y[col]
        # MC Y 0 in image == top row; flip so high Y is up.
        def row_for_y(y_mc: int) -> int:
            return height - 1 - int(y_mc + 64)  # -64..447 -> 511..0

        for y in range(-64, 448):
            r = row_for_y(y)
            if y < -60:
                c = PAL["bedrock"]
            elif y < 0:
                c = PAL["deepslate"]
            elif y < sy - 5:
                c = PAL["basement"]
            elif y < sy - 2:
                c = PAL["sediment"]
            elif y < sy:
                c = PAL["soil"]
            elif y == sy:
                c = PAL["surface"]
            else:
                c = PAL["air"]
            img[r, col] = c
    return img


def _save_png(img: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        Image.fromarray(img, mode="RGB").save(path)
    except ImportError:
        # Last-ditch fallback: write raw numpy to .npy so the file materializes.
        np.save(path.with_suffix(".npy"), img)


def main() -> int:
    args = _parse_args()
    out_path = Path(args.out.format(tx=args.tile_x, tz=args.tile_z))
    img = render_crosssection_stub(args.tile_x, args.tile_z, tuple(args.line))
    _save_png(img, out_path)
    meta = {
        "tile_x": args.tile_x, "tile_z": args.tile_z,
        "line": list(args.line),
        "shape": list(img.shape),
        "phase": 0,
        "stub": True,
    }
    out_path.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    print(f"[diag_cliff_crosssection] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
