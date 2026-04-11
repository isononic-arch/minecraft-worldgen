"""diag_layer_ownership.py — Phase 0 stub (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §7 "Rendering layers #5 Layer ownership"
and §11 Phase 0.

Renders the partition-layer ownership map — each partition layer colored
distinctly — to verify ≥99% partition coverage on the pilot tile and catch
overlapping claims between adjacent layers.

Phase 0 STUB: synthesizes an ownership array from a fake pipeline run so
the CLI + PNG I/O path is demonstrable. The real run calls
`core.surface_pipeline.run_pass()` on a real layer list.

CLI:
    py diag_layer_ownership.py --tile-x 36 --tile-z 20 \
        --out diag_output/36_20/layer_ownership.png
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
                   default="diag_output/{tx}_{tz}/layer_ownership.png")
    return p.parse_args()


# 12-color categorical palette for up to 11 partition layers + unclaimed (0).
OWNERSHIP_PALETTE = np.array([
    (30, 30, 30),      # 0 = unclaimed (dark gray)
    (230, 60, 60),     # 1
    (230, 150, 60),    # 2
    (230, 230, 60),    # 3
    (150, 230, 60),    # 4
    (60, 230, 120),    # 5
    (60, 230, 230),    # 6
    (60, 150, 230),    # 7
    (80, 60, 230),     # 8
    (170, 60, 230),    # 9
    (230, 60, 170),    # 10
    (255, 255, 255),   # 11
], dtype=np.uint8)


def render_ownership_stub(tile_x: int, tile_z: int, size: int = 512) -> np.ndarray:
    # Synthetic 9-layer partition using radial bands + noise hash.
    ys, xs = np.mgrid[0:size, 0:size]
    cx, cy = size // 2, size // 2
    r = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    ownership = ((r // 28).astype(np.int32) + tile_x + tile_z) % 11 + 1
    # Carve ~5% unclaimed so coverage check is meaningful.
    rng = np.random.default_rng(tile_x * 1009 + tile_z)
    unclaimed = rng.random((size, size)) < 0.05
    ownership[unclaimed] = 0

    img = OWNERSHIP_PALETTE[ownership]
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
    img = render_ownership_stub(args.tile_x, args.tile_z, args.size)
    _save_png(img, out_path)
    print(f"[diag_layer_ownership] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
