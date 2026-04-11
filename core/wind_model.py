"""Tradewind single-source-of-truth — Phase 0 scaffolding (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §3 principle #9 and §11 Phase 0.

Vandir tradewinds blow WEST → EAST at 270° (the direction the wind blows
*toward*). This means:
  - West-facing slopes are WINDWARD (exposed, scoured, drier).
  - East-facing slopes are LEEWARD (sheltered, accumulation-biased, wetter).

Every wind-aware layer (windthrow, wave fetch, dune orientation, snow lee
accumulation, leaf-litter drift, sand scour, weathering bleach asymmetry)
imports from this module. Getting the sign wrong makes the map's whole
weathering story run backwards — pinned by unit test in
`tests/unit/test_wind_model.py`.

Aspect convention (consistent with core/eco_gradients.py):
  aspect is in RADIANS, measured counter-clockwise from +x (east).
  aspect =   0      → slope faces EAST
  aspect =   π/2    → slope faces NORTH
  aspect =   π      → slope faces WEST
  aspect =  -π/2    → slope faces SOUTH
"""
from __future__ import annotations

import math
from typing import Tuple

import numpy as np

# Direction the wind BLOWS TOWARD, in degrees (0 = east, 90 = north).
# 270° nautical = winds from the west heading east in meteorology OR the
# direction-toward convention. For Vandir we care about "wind hits west-facing
# slopes first", which is a west-to-east flow. In our aspect convention
# (0 rad = east), west-facing slopes have aspect ≈ π. The wind VECTOR points
# east, which is aspect 0. So the windward unit vector in our frame is (1, 0).
WIND_FLOW_DIRECTION_RAD: float = 0.0  # direction wind blows toward
WIND_FLOW_VECTOR: Tuple[float, float] = (1.0, 0.0)  # (x, z) — +x = east

# For anywhere in docs where we say "270°" (from CLAUDE.md / §3 rule 9), that
# refers to the meteorological "from" direction (wind from the west = 270°).
WIND_SOURCE_HEADING_DEG: float = 270.0


def _as_array(aspect) -> np.ndarray:
    return np.asarray(aspect, dtype=np.float64)


def windward_factor(aspect) -> np.ndarray:
    """Return 1.0 where the slope faces directly into the wind, 0.0 where it
    faces directly away. Windward means WEST-FACING for Vandir.

    A slope with aspect = π (facing west) has an outward normal pointing west,
    i.e. INTO the wind. That's windward=1.

    Formula: `windward = (1 - cos(aspect)) / 2`
      aspect = π (west-facing)  → windward = 1
      aspect = 0 (east-facing)  → windward = 0
      aspect = ±π/2 (N/S)       → windward = 0.5
    """
    a = _as_array(aspect)
    return (1.0 - np.cos(a)) * 0.5


def leeward_factor(aspect) -> np.ndarray:
    """Complement of windward_factor. 1.0 on EAST-facing slopes."""
    a = _as_array(aspect)
    return (1.0 + np.cos(a)) * 0.5


def wind_exposure(aspect, slope) -> np.ndarray:
    """Combined exposure signal: windward_factor × slope_magnitude.

    Use where the weathering rule wants "steep AND facing the wind".
    `slope` is expected in [0, 1] (normalized) or radians; both pass through
    because the result is just scaled.
    """
    w = windward_factor(aspect)
    s = np.asarray(slope, dtype=np.float64)
    return w * s


def fetch_integral(
    water_mask: np.ndarray,
    *,
    max_distance: int = 64,
) -> np.ndarray:
    """Upwind water fetch for every pixel.

    For each pixel, counts the number of consecutive water cells immediately
    upwind (west) of it, capped at `max_distance`. Land pixels directly east
    of a long open-water band see large fetch values (wave-exposed shore);
    pixels inside or west of water see 0.

    Implementation: vectorized row-wise cumulative scan. For each row we
    compute a running count of consecutive True cells west-of-center, which
    is the fetch that a land pixel would inherit if a water band ends at its
    west neighbor. The row scan is O(W) per row with numpy primitives — fast
    enough to call on the full 6250×6250 precompute grid (~40 M pixels ≈
    sub-second on modern CPUs).

    Algorithm per row r:
        run[x] = length of the water run ending at x (0 if water_mask[r,x] is False)
        out[r, x] = run[x-1] if land cell, else 0
        run computed via the standard "cumulative count resetting on zero"
        trick: treat water as 1/land as 0, cumsum, then subtract the last
        cumsum value seen at a land boundary.

    Args:
        water_mask: (H, W) bool array. True where water.
        max_distance: cap in pixels (at 1:8 precompute res, 1 px = 8 blocks).

    Returns:
        (H, W) int32 array. `out[y, x] = # of water pixels in the contiguous
        west-side water run ending at (y, x-1)`, capped at `max_distance`.
        Water cells return 0 — we only care about shore exposure.
    """
    wm = water_mask.astype(bool, copy=False)
    H, W = wm.shape

    # Step 1: run-length count of water cells ending at each column.
    # Standard trick: cumsum of water, minus the cumsum value at the most
    # recent land reset.
    water_int = wm.astype(np.int32)
    cs = np.cumsum(water_int, axis=1)  # (H, W)
    # For each row, at each land cell (water_int == 0) store cs, else 0, then
    # take running max along axis=1 → "most recent reset value".
    reset_vals = np.where(water_int == 0, cs, 0)
    reset_max = np.maximum.accumulate(reset_vals, axis=1)
    run = cs - reset_max  # run[y, x] = length of water run ending at x

    # Step 2: fetch at cell x = run at x-1 (the west neighbor). Shift right.
    out = np.zeros_like(run)
    out[:, 1:] = run[:, :-1]

    # Step 3: only land cells report fetch; water cells zero-out (shore signal).
    out[wm] = 0

    # Step 4: cap.
    if max_distance is not None:
        np.minimum(out, max_distance, out=out)
    return out


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

WEST_FACING_ASPECT = math.pi   # outward normal points west
EAST_FACING_ASPECT = 0.0
NORTH_FACING_ASPECT = math.pi / 2
SOUTH_FACING_ASPECT = -math.pi / 2
