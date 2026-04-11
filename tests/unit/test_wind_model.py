"""Phase 0 unit tests — wind model sign pinning.

Spec: PHYSICAL_REALISM_REFACTOR.md §3 principle #9 and R3-2.

PINS: Vandir tradewinds blow west → east. West-facing slopes are windward
(exposed to wind). A single sign flip here runs the entire weathering
story backwards across the map, so this test exists to catch it at the
earliest possible moment.
"""
from __future__ import annotations

import math

import numpy as np

from core import wind_model as wm


def test_west_facing_is_windward():
    """aspect = π (outward normal pointing west) → windward_factor == 1."""
    assert np.isclose(wm.windward_factor(wm.WEST_FACING_ASPECT), 1.0)


def test_east_facing_is_leeward():
    """aspect = 0 (outward normal pointing east) → windward_factor == 0."""
    assert np.isclose(wm.windward_factor(wm.EAST_FACING_ASPECT), 0.0)
    assert np.isclose(wm.leeward_factor(wm.EAST_FACING_ASPECT), 1.0)


def test_north_south_are_neutral():
    assert np.isclose(wm.windward_factor(wm.NORTH_FACING_ASPECT), 0.5)
    assert np.isclose(wm.windward_factor(wm.SOUTH_FACING_ASPECT), 0.5)


def test_windward_and_leeward_sum_to_one():
    aspects = np.linspace(-math.pi, math.pi, 41)
    w = wm.windward_factor(aspects)
    l = wm.leeward_factor(aspects)
    assert np.allclose(w + l, 1.0)


def test_wind_exposure_scales_with_slope():
    aspect = np.array([wm.WEST_FACING_ASPECT, wm.WEST_FACING_ASPECT])
    slope = np.array([0.2, 0.8])
    exp = wm.wind_exposure(aspect, slope)
    assert exp[1] > exp[0]
    assert np.isclose(exp[1] / exp[0], 4.0, atol=1e-6)


def test_fetch_integral_direction_is_west_to_east():
    """A land pixel east of a long open-water band should see nonzero fetch.
    A land pixel WEST of water should see zero fetch (water is downwind)."""
    H, W = 4, 16
    water = np.zeros((H, W), dtype=bool)
    water[:, 2:10] = True  # open water in columns 2..9

    fetch = wm.fetch_integral(water, max_distance=32)

    # Land pixel at column 10 (east of the water band) sees 8 px of upwind water.
    assert (fetch[:, 10] == 8).all(), f"east-of-water should see fetch=8, got {fetch[:, 10]}"
    # Land pixel at column 1 (west of water) sees 0 — water is downwind, not upwind.
    assert (fetch[:, 1] == 0).all(), f"west-of-water should see fetch=0, got {fetch[:, 1]}"


def test_wind_flow_vector_points_east():
    assert wm.WIND_FLOW_VECTOR == (1.0, 0.0)
    assert wm.WIND_SOURCE_HEADING_DEG == 270.0
