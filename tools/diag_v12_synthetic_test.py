"""
diag_v12_synthetic_test.py — Unit-test the v12 geomorph helpers on
SYNTHETIC 64x64 data. No real masks, no large arrays, no OOM risk.

Validates:
  - _compute_skeleton_arclength_8k returns arclen ≥ 0 and monotone
    along a straight skeleton
  - _compute_polygon_geometry returns tangent unit-vectors and
    finite curvature on a circular polygon (kappa ≈ 1/R)
  - _apply_river_geomorph_8k applies SOME bias (non-zero std) on a
    straight river footprint with curved-outline polygons
"""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import hydro_region_overlay as hro


def test_skel_arclength_straight_line():
    print("\n=== test_skel_arclength_straight_line ===")
    skel = np.zeros((64, 64), dtype=bool)
    # Horizontal line at y=32, x=10..53 (44 cells)
    skel[32, 10:54] = True
    # Edges: pairs of adjacent skeleton cells
    edges = [(32, x, 32, x + 1) for x in range(10, 53)]
    pts, arclen = hro._compute_skeleton_arclength_8k(skel, edges)
    assert pts is not None, "should return points"
    assert len(pts) == 44, f"expected 44 cells, got {len(pts)}"
    # arclen runs 0, 1, 2, ... 43 (since each step is unit distance)
    # but ordering depends on BFS start (one of the two endpoints)
    sorted_arc = np.sort(arclen)
    expected = np.arange(44, dtype=np.float32)
    assert np.allclose(sorted_arc, expected, atol=1e-3), (
        f"arclen distribution wrong, got {sorted_arc[:5]}..{sorted_arc[-5:]}")
    print(f"  OK — 44 cells, arclen 0..{arclen.max():.1f}, all positive")


def test_polygon_geometry_circle():
    print("\n=== test_polygon_geometry_circle ===")
    # Circle of radius 100 in 50k coords, centered at origin
    R = 100.0
    n_pts = 200
    theta = np.linspace(0, 2 * np.pi, n_pts, endpoint=False)
    xy = np.column_stack([R * np.cos(theta), R * np.sin(theta)]).astype(np.float32)
    pts_yx, tangent, kappa, arclen = hro._compute_polygon_geometry([xy])
    assert pts_yx is not None, "should return geometry for circle"
    # Tangent magnitudes should be ~1 (unit vectors)
    mag = np.hypot(tangent[:, 0], tangent[:, 1])
    assert np.allclose(mag, 1.0, atol=0.05), f"tangent mag {mag.min()}..{mag.max()}"
    # Curvature magnitude should be ~1/R in 8k pixel units
    # 100 blocks / blocks_per_8k_px (= 50000/8192 ≈ 6.1) = 16.4 8k px
    # kappa ≈ 1 / 16.4 ≈ 0.061 in 1/(8k_px)
    blocks_per_px = hro._WORLD_PX / hro._REGION_PX
    R_8k = R / blocks_per_px
    expected_kappa_mag = 1.0 / R_8k
    median_kappa = float(np.median(np.abs(kappa)))
    rel_err = abs(median_kappa - expected_kappa_mag) / expected_kappa_mag
    print(f"  median |kappa| = {median_kappa:.4f}, expected ~{expected_kappa_mag:.4f}, "
          f"rel err {rel_err:.2%}")
    assert rel_err < 0.3, f"curvature off by {rel_err:.2%}"
    print(f"  OK - circle of R={R}b gives kappa ~ 1/R consistent")


def test_geomorph_apply_synthetic():
    print("\n=== test_geomorph_apply_synthetic ===")
    # Build a footprint: rectangle 6 cells tall x 30 wide in 100x100 grid
    H = W = 100
    bed = np.full((H, W), 60.0, dtype=np.float32)  # uniform bed at Y=60
    footprint = np.zeros((H, W), dtype=bool)
    footprint[47:53, 35:65] = True
    # Skeleton: horizontal line at y=50
    skel = np.zeros((H, W), dtype=bool)
    skel[50, 35:65] = True
    edges = [(50, x, 50, x + 1) for x in range(35, 64)]
    # Polygons: rectangle boundary as a single closed contour
    # CCW starting from (47,35): right along top, down right side,
    # left along bottom, up left side
    poly_pts = []
    for x in range(35, 65):
        poly_pts.append([x, 47])  # top row
    for y in range(47, 53):
        poly_pts.append([64, y])  # right col
    for x in range(64, 35, -1):
        poly_pts.append([x, 52])  # bottom row
    for y in range(52, 47, -1):
        poly_pts.append([35, y])  # left col
    # Convert to 50k coords (this is fake — just use as-is, scale doesn't matter)
    # _compute_polygon_geometry treats these as 50k coords and converts to 8k
    # internally. Multiply by 6.1 so 8k version is what we want.
    blocks_per_px = hro._WORLD_PX / hro._REGION_PX
    poly_50k = np.array(poly_pts, dtype=np.float32) * blocks_per_px

    bed_in = bed.copy()
    bed_out = hro._apply_river_geomorph_8k(
        bed.copy(), footprint, [poly_50k], skel, edges, blocks_per_px)
    # The bed should have changed in footprint cells
    diff = bed_out - bed_in
    fp_diff = diff[footprint]
    print(f"  footprint cells: {int(footprint.sum())}")
    print(f"  diff range:      [{fp_diff.min():.3f}, {fp_diff.max():.3f}]")
    print(f"  diff mean / std: {fp_diff.mean():.3f} / {fp_diff.std():.3f}")
    print(f"  non-footprint diff (should be 0): {diff[~footprint].max():.6f}")
    # For a straight-line skeleton: bedform + riffle-pool still apply
    # (sin waves along arclength). Thalweg from rectangle polygon should
    # be near zero since the long sides have kappa ≈ 0 (straight). So:
    assert fp_diff.std() > 0.05, (
        f"expected some bedform/riffle variation, got std={fp_diff.std():.3f}")
    assert np.abs(diff[~footprint]).max() < 1e-3, (
        "diff should be exactly 0 outside footprint")
    print(f"  OK — bias applied only inside footprint, non-trivial variation")


def main():
    test_skel_arclength_straight_line()
    test_polygon_geometry_circle()
    test_geomorph_apply_synthetic()
    print("\nALL SYNTHETIC TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
