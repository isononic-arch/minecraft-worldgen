"""diag_topdown_blocks.py — Phase 0 stub (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §7 "Primary tool" (Rendering layer 1:
Base block-ID top-down).

Top-down surface-block render for a tile or a 3×3 neighborhood. Used as the
base layer by `tools/world_viewer.py` once that lands. Phase 0 stub emits a
synthetic checker pattern so the I/O path is exercised end-to-end.

CLI:
    py diag_topdown_blocks.py --tile-x 36 --tile-z 20 --out diag_output/36_20/topdown.png
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
    p.add_argument("--out", type=str,
                   default="diag_output/{tx}_{tz}/topdown_blocks.png")
    return p.parse_args()


def render_topdown_stub(tile_x: int, tile_z: int, size: int = 512) -> np.ndarray:
    """Placeholder top-down render.

    Phase 1+ replaces this with a real block-ID lookup via the forthcoming
    `tools/world_block_map.py` BLOCK_COLORS LUT.
    """
    img = np.zeros((size, size, 3), dtype=np.uint8)
    # Deterministic, tile-dependent checker so consecutive tiles look different.
    ys, xs = np.mgrid[0:size, 0:size]
    bucket = ((xs // 32) ^ (ys // 32) ^ tile_x ^ tile_z) & 0x7
    palette = np.array([
        (80, 130, 60),   # grass-ish
        (120, 100, 70),  # dirt-ish
        (150, 150, 155), # stone-ish
        (200, 200, 210), # cobble-ish
        (230, 220, 180), # sand-ish
        (110, 140, 180), # water-ish
        (200, 240, 255), # snow-ish
        (90, 70, 55),    # podzol-ish
    ], dtype=np.uint8)
    img[:] = palette[bucket]
    return img


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
    img = render_topdown_stub(args.tile_x, args.tile_z, args.size)
    _save_png(img, out_path)
    print(f"[diag_topdown_blocks] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
