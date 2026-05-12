"""
diag_v13_bbox_equality.py - prove the bbox-optimized bank asymmetry
produces output numerically identical to the full-image version.

Uses synthetic 200x200 grid with a curved skeleton + footprint to
trigger both point-bar and cut-bank branches.

Both versions must produce float32-equal output (within rounding).
"""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import hydro_region_overlay as hro


def reference_full_image_version(
    bed_smooth_8k, footprint_8k, polygons_50k):
    """The PRE-v13 full-image implementation, reproduced verbatim for
    equality testing."""
    from scipy.ndimage import (
        binary_dilation as _bd, gaussian_filter as _gf,
    )
    pts_yx, tangent, kappa, _arclen = hro._compute_polygon_geometry(polygons_50k)
    if pts_yx is None:
        return bed_smooth_8k
    from scipy.spatial import cKDTree
    ring = _bd(footprint_8k,
               iterations=hro._BANK_ASYM_RING_RADIUS_8K) & ~footprint_8k
    if not ring.any():
        return bed_smooth_8k
    kd = cKDTree(pts_yx)
    ry, rx = np.where(ring)
    q = np.column_stack([ry, rx]).astype(np.float32)
    _, idx = kd.query(q, k=1)
    rel_y = ry.astype(np.float32) - pts_yx[idx, 0]
    rel_x = rx.astype(np.float32) - pts_yx[idx, 1]
    t_x = tangent[idx, 0]
    t_y = tangent[idx, 1]
    n_y = t_x
    n_x = -t_y
    perp_8k = rel_y * n_y + rel_x * n_x
    bend = hro._BANK_ASYM_SIGN * kappa[idx] * perp_8k
    out = bed_smooth_8k.copy()
    pointbar_mask = np.zeros_like(out, dtype=np.float32)
    pointbar_mask[ry, rx] = (bend < 0).astype(np.float32)
    if pointbar_mask.any() and hro._BANK_ASYM_SIGMA_POINTBAR_8K > 0:
        bl = _gf(out, sigma=hro._BANK_ASYM_SIGMA_POINTBAR_8K)
        out = (pointbar_mask * bl + (1.0 - pointbar_mask) * out).astype(np.float32)
    cutbank_mask = np.zeros_like(out, dtype=np.float32)
    cutbank_mask[ry, rx] = (bend >= 0).astype(np.float32)
    if cutbank_mask.any() and hro._BANK_ASYM_SIGMA_CUTBANK_8K > 0:
        bl = _gf(out, sigma=hro._BANK_ASYM_SIGMA_CUTBANK_8K)
        out = (cutbank_mask * bl + (1.0 - cutbank_mask) * out).astype(np.float32)
    return out


def test_bbox_equals_full():
    print("=== test_bbox_equals_full ===")
    # Synthetic curved river: arc from (50,30) to (50,170) bowed south
    H = W = 200
    bed = np.full((H, W), 60.0, dtype=np.float32)
    # Add varied bed inside the river footprint so smoothing has something to do
    rng = np.random.default_rng(42)
    bed += rng.standard_normal((H, W)).astype(np.float32) * 2.0
    # Footprint: 8-cell-tall corridor that arches
    footprint = np.zeros((H, W), dtype=bool)
    for x in range(30, 171):
        y_center = int(60 + 15 * np.sin((x - 30) / 140 * np.pi))
        footprint[y_center - 4:y_center + 5, x] = True
    # Build polygon outline (rectangle-ish around the arch, CCW)
    pts = []
    for x in range(30, 171):
        y_center = int(60 + 15 * np.sin((x - 30) / 140 * np.pi))
        pts.append([x, y_center - 4])  # top
    for x in range(170, 29, -1):
        y_center = int(60 + 15 * np.sin((x - 30) / 140 * np.pi))
        pts.append([x, y_center + 4])  # bottom (reversed)
    poly_50k = np.array(pts, dtype=np.float32) * (hro._WORLD_PX / hro._REGION_PX)

    bed_full = reference_full_image_version(
        bed.copy(), footprint, [poly_50k])
    bed_bbox = hro._apply_asymmetric_bank_smoothing_8k(
        bed.copy(), footprint, [poly_50k])

    max_diff = float(np.abs(bed_full - bed_bbox).max())
    n_diff = int((np.abs(bed_full - bed_bbox) > 1e-6).sum())
    print(f"  max abs diff: {max_diff:.6f}")
    print(f"  cells differing > 1e-6: {n_diff}")
    print(f"  bed range: [{bed_bbox.min():.2f}, {bed_bbox.max():.2f}]")
    assert max_diff < 1e-4, (
        f"bbox vs full diverged by {max_diff} (must be < 1e-4)")
    print(f"  PASS: bbox version is numerically equal to full-image version")


if __name__ == "__main__":
    test_bbox_equals_full()
    print("\nALL EQUALITY TESTS PASSED")
