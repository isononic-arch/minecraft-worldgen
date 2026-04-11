"""diag_suitability_field.py — Phase 0 stub (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §7 "Rendering layers #6 Suitability field".

Renders a per-layer density/suitability grayscale heatmap so Phase 3/4
vegetation placement can be visually tuned without .mca regen.

Phase 0 STUB: synthesizes a plausible suitability field. Phase 3 swaps to
reading `core.tree_density_hint.compute_tree_density_hint()` or per-layer
equivalents.

CLI:
    py diag_suitability_field.py --tile-x 36 --tile-z 20 \
        --layer temperate_tree_canopy --out diag_output/36_20/suitability.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tile-x", type=int, default=36)
    p.add_argument("--tile-z", type=int, default=20)
    p.add_argument("--layer", type=str, default="temperate_tree_canopy")
    p.add_argument("--size", type=int, default=512)
    p.add_argument("--out", type=str,
                   default="diag_output/{tx}_{tz}/suitability_{layer}.png")
    return p.parse_args()


def render_suitability_stub(
    tile_x: int, tile_z: int, layer: str, size: int = 512,
) -> np.ndarray:
    """Synthetic suitability field: gaussian blob + noise.

    Returns (size, size) uint8 grayscale image [0..255].
    """
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
    cx = size * 0.3 + 60 * ((tile_x % 3) - 1)
    cy = size * 0.5 + 60 * ((tile_z % 3) - 1)
    sig = size * 0.35
    blob = np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * sig ** 2))

    seed = (abs(hash(layer)) ^ (tile_x * 1009 + tile_z)) & 0xFFFFFFFF
    rng = np.random.default_rng(seed)
    noise = rng.random((size, size)).astype(np.float32) * 0.2

    field = np.clip(blob + noise, 0.0, 1.0)
    gray = (field * 255).astype(np.uint8)
    # Expand to RGB for consistency with other diag PNGs.
    return np.stack([gray, gray, gray], axis=-1)


def _save_png(img: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        Image.fromarray(img, mode="RGB").save(path)
    except ImportError:
        np.save(path.with_suffix(".npy"), img)


def main() -> int:
    args = _parse_args()
    out_path = Path(args.out.format(tx=args.tile_x, tz=args.tile_z, layer=args.layer))
    img = render_suitability_stub(args.tile_x, args.tile_z, args.layer, args.size)
    _save_png(img, out_path)
    print(f"[diag_suitability_field] wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
