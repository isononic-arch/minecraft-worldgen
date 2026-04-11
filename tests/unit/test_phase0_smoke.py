"""Phase 0 smoke test — imports + field helpers.

Quick end-to-end sanity checks that every Phase 0 module imports cleanly and
the deterministic helpers return sensibly-shaped output without requiring
opensimplex (they have pure-numpy fallbacks).
"""
from __future__ import annotations

import numpy as np

from core import meadow_clearing_field as mcf
from core import tree_density_hint as tdh
from core.layers import noise_profiles as np_lib


def test_meadow_clearing_field_shape_and_range():
    field = mcf.compute_meadow_clearing_field(36, 20, H=64, W=64)
    assert field.shape == (64, 64)
    assert field.dtype == np.float32
    assert (field >= 0).all() and (field <= 1).all()


def test_meadow_clearing_masks():
    field = mcf.compute_meadow_clearing_field(36, 20, H=64, W=64)
    interior = mcf.clearing_interior_mask(field)
    seam = mcf.clearing_seam_mask(field)
    assert interior.shape == field.shape
    assert seam.shape == field.shape


def test_legacy_mixed_forest_noise_shape_and_range():
    field = np_lib.legacy_mixed_forest_noise(36, 20, H=32, W=32)
    assert field.shape == (32, 32)
    assert (field >= 0).all() and (field <= 1).all()


def test_tree_density_hint_zero_outside_forest():
    biome_grid = np.full((8, 8), "DESERT_ROCK", dtype=object)
    slope = np.zeros((8, 8), dtype=np.float32)
    moisture = np.ones((8, 8), dtype=np.float32)
    hint = tdh.compute_tree_density_hint(
        biome_grid, slope=slope, moisture_idx=moisture
    )
    assert (hint == 0).all()


def test_tree_density_hint_nonzero_in_forest_with_moisture():
    biome_grid = np.full((8, 8), "MIXED_FOREST", dtype=object)
    slope = np.zeros((8, 8), dtype=np.float32)
    moisture = np.ones((8, 8), dtype=np.float32)
    hint = tdh.compute_tree_density_hint(
        biome_grid, slope=slope, moisture_idx=moisture
    )
    assert (hint > 0.5).all()


def test_tree_density_hint_steep_slope_suppresses():
    biome_grid = np.full((8, 8), "MIXED_FOREST", dtype=object)
    slope = np.ones((8, 8), dtype=np.float32)     # cliff
    moisture = np.ones((8, 8), dtype=np.float32)
    hint = tdh.compute_tree_density_hint(
        biome_grid, slope=slope, moisture_idx=moisture
    )
    assert (hint < 0.5).all()
