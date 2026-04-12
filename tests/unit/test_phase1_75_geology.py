"""
tests/unit/test_phase1_75_geology.py

Phase 1.75 "Real geology content" tests.  Validates that:
  1. Flag-ON with lithology produces a volume with geology stratification
     (deepslate bedrock band, lithology-palette basement, sediment, soil).
  2. Flag-OFF is still byte-identical to pre-edit (regression safety net).
  3. Geology layers are in correct vertical order.
  4. Lithology palette blocks actually appear in the basement range.
  5. Sediment blocks (gravel/dirt/coarse_dirt) appear above basement.
  6. Soil horizon blocks appear just below surface.
  7. Underwater / sub-sea columns are not disrupted by geology fill.

Written S47 (2026-04-12).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import chunk_writer as cw  # noqa: E402


# Constants mirrored from chunk_writer for assertions
Y_MIN = cw.Y_MIN
Y_RANGE = cw.Y_RANGE
SEA_Y = cw.SEA_Y
_BEDROCK_BAND_DEPTH = cw._BEDROCK_BAND_DEPTH


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_CFG = {
    "lithology": {
        "feature_flag_enabled": True,
        "groups": {
            "granitic": {
                "id": 1,
                "palette": ["stone", "andesite", "granite", "diorite"],
            },
            "sedimentary": {
                "id": 2,
                "palette": ["sandstone", "smooth_sandstone", "red_sandstone", "stone"],
            },
        },
    },
    "cliff_banding": {
        "cliff_deg_thr": 45.0,
        "band_scale_y": 12,
    },
}


def _tiny_tile(h: int = 16, w: int = 16) -> dict:
    """Build the smallest valid input set for build_column_array()."""
    surface_y = np.full((h, w), 100, dtype=np.int16)  # well above sea level
    surface_blk = np.full((h, w), "grass_block", dtype=object)
    sub_blk = np.full((h, w), "dirt", dtype=object)
    ground_cover = np.full((h, w), "", dtype=object)
    biome_grid = np.full((h, w), "MIXED_FOREST", dtype=object)
    lithology_tile = np.full((h, w), 1, dtype=np.uint8)  # all granitic
    flow_tile = np.full((h, w), 0.05, dtype=np.float32)  # low flow
    return dict(
        surface_y=surface_y,
        surface_blk=surface_blk,
        sub_blk=sub_blk,
        ground_cover=ground_cover,
        biome_grid=biome_grid,
        lithology_tile=lithology_tile,
        flow_tile=flow_tile,
    )


def _build_with_geology(**overrides) -> tuple[np.ndarray, cw.BlockPalette]:
    kw = _tiny_tile()
    kw.update(overrides)
    return cw.build_column_array(
        surface_y=kw["surface_y"],
        surface_blk=kw["surface_blk"],
        sub_blk=kw["sub_blk"],
        ground_cover=kw["ground_cover"],
        biome_grid=kw["biome_grid"],
        lithology_tile=kw["lithology_tile"],
        use_new_geology=True,
        flow_tile=kw["flow_tile"],
        cfg=_SAMPLE_CFG,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_flag_on_produces_valid_volume():
    """Flag-ON with lithology must produce a valid volume, not raise."""
    vol, pal = _build_with_geology()
    assert vol.ndim == 3
    assert vol.shape == (Y_RANGE, 16, 16)
    assert vol.dtype == np.uint16
    # Bedrock row must be present
    assert (vol[0] != 0).all(), "Bedrock layer missing"


def test_flag_off_still_byte_identical():
    """Flag-OFF must be byte-identical to call without new kwargs."""
    kw = _tiny_tile()
    base_kw = {k: kw[k] for k in ("surface_y", "surface_blk", "sub_blk",
                                    "ground_cover", "biome_grid")}
    vol_base, _ = cw.build_column_array(**base_kw)

    vol_off, _ = cw.build_column_array(
        **base_kw,
        lithology_tile=kw["lithology_tile"],
        use_new_geology=False,
        flow_tile=kw["flow_tile"],
        cfg=_SAMPLE_CFG,
    )
    assert np.array_equal(vol_base, vol_off), "flag-OFF must be byte-identical"


def test_bedrock_band_is_deepslate():
    """Y_MIN+1 to Y_MIN+_BEDROCK_BAND_DEPTH should be deepslate."""
    vol, pal = _build_with_geology()
    ds_idx = pal.idx("deepslate")
    # Check a central column
    r, c = 8, 8
    for y_off in range(1, _BEDROCK_BAND_DEPTH + 1):
        assert vol[y_off, r, c] == ds_idx, \
            f"Y_MIN+{y_off} should be deepslate, got {pal.name_of(vol[y_off, r, c])}"


def test_basement_contains_lithology_palette_blocks():
    """
    The basement range (below sediment, above bedrock band) should contain
    blocks from the lithology group's palette, not uniform stone.
    """
    vol, pal = _build_with_geology()
    # Granitic palette: stone, andesite, granite, diorite
    granitic_indices = {pal.idx(b) for b in ["stone", "andesite", "granite", "diorite"]}

    r, c = 8, 8
    sy = 100  # surface_y for this test
    # Basement roughly from Y_MIN+5 up to somewhere below sediment/soil
    # Just check that at least 2 distinct blocks from the palette appear
    basement_blocks = set()
    for y_abs in range(Y_MIN + _BEDROCK_BAND_DEPTH + 1, sy - 15):
        y_idx = y_abs - Y_MIN
        if 0 <= y_idx < Y_RANGE:
            blk_idx = vol[y_idx, r, c]
            if blk_idx in granitic_indices:
                basement_blocks.add(blk_idx)

    assert len(basement_blocks) >= 2, \
        f"Expected multiple palette blocks in basement, found {len(basement_blocks)}: " \
        f"{[pal.name_of(i) for i in basement_blocks]}"


def test_sediment_layer_present():
    """Sediment blocks (gravel/coarse_dirt/dirt) should exist between basement and soil."""
    kw = _tiny_tile()
    # Use high flow to ensure gravel appears
    kw["flow_tile"][:] = 0.5
    vol, pal = _build_with_geology(**kw)

    gravel_idx = pal.idx("gravel")
    r, c = 8, 8
    sy = 100

    # Check range just above where basement ends but below surface
    found_gravel = False
    for y_abs in range(sy - 20, sy - 3):
        y_idx = y_abs - Y_MIN
        if 0 <= y_idx < Y_RANGE and vol[y_idx, r, c] == gravel_idx:
            found_gravel = True
            break

    assert found_gravel, "High-flow columns should have gravel in sediment layer"


def test_soil_horizon_present():
    """Soil horizon (dirt/coarse_dirt) should be just below surface_y-3."""
    vol, pal = _build_with_geology()
    dirt_idx = pal.idx("dirt")
    coarse_idx = pal.idx("coarse_dirt")

    r, c = 8, 8
    sy = 100
    # Soil should be in the range [surface_y - 3 - soil_depth, surface_y - 3]
    # For flat terrain (slope < 18°), soil_depth = 4
    soil_found = False
    for y_abs in range(sy - 7, sy - 2):  # sy-3 down to sy-7
        y_idx = y_abs - Y_MIN
        if 0 <= y_idx < Y_RANGE:
            blk = vol[y_idx, r, c]
            if blk in (dirt_idx, coarse_idx):
                soil_found = True
                break

    assert soil_found, "Soil horizon (dirt/coarse_dirt) should exist near surface"


def test_vertical_order_is_correct():
    """
    From bottom to top, blocks should transition:
    bedrock → deepslate → basement rock → sediment → soil → sub_blk → surface_blk
    """
    vol, pal = _build_with_geology()
    r, c = 8, 8
    sy = 100

    bedrock_idx = pal.idx("bedrock")
    deepslate_idx = pal.idx("deepslate")
    grass_idx = pal.idx("grass_block")
    dirt_idx = pal.idx("dirt")

    # Y=0 (Y_MIN): bedrock
    assert vol[0, r, c] == bedrock_idx

    # Y=1..4 (Y_MIN+1..Y_MIN+4): deepslate
    for yi in range(1, _BEDROCK_BAND_DEPTH + 1):
        assert vol[yi, r, c] == deepslate_idx

    # Surface (surface_y): grass_block
    sy_idx = sy - Y_MIN
    assert vol[sy_idx, r, c] == grass_idx

    # Sub-surface (sy-1, sy-2): dirt
    assert vol[sy_idx - 1, r, c] == dirt_idx
    assert vol[sy_idx - 2, r, c] == dirt_idx


def test_ocean_columns_unaffected():
    """Columns below sea level should still fill with water, not geology."""
    kw = _tiny_tile()
    kw["surface_y"][:] = 40  # sub-sea
    kw["surface_blk"][:] = "sand"
    vol, pal = _build_with_geology(**kw)

    water_idx = pal.idx("water")
    r, c = 8, 8
    # Water should fill from surface_y+1 to SEA_Y
    for y_abs in range(41, SEA_Y + 1):
        y_idx = y_abs - Y_MIN
        if 0 <= y_idx < Y_RANGE:
            assert vol[y_idx, r, c] == water_idx, \
                f"Y={y_abs} should be water, got {pal.name_of(vol[y_idx, r, c])}"


def test_mixed_lithology_groups():
    """Two different lithology groups should produce different basement blocks."""
    kw = _tiny_tile()
    # Left half = granitic (1), right half = sedimentary (2)
    kw["lithology_tile"][:, :8] = 1
    kw["lithology_tile"][:, 8:] = 2
    vol, pal = _build_with_geology(**kw)

    # Sample basement columns from each half
    sy = 100
    y_mid = (Y_MIN + _BEDROCK_BAND_DEPTH + 10) - Y_MIN  # well into basement

    left_block = pal.name_of(vol[y_mid, 8, 4])
    right_block = pal.name_of(vol[y_mid, 8, 12])

    # Granitic palette: stone/andesite/granite/diorite
    # Sedimentary palette: sandstone/smooth_sandstone/red_sandstone/stone
    granitic_blocks = {"stone", "andesite", "granite", "diorite"}
    sedimentary_blocks = {"sandstone", "smooth_sandstone", "red_sandstone", "stone"}

    assert left_block in granitic_blocks, \
        f"Left (granitic) column at y_mid has {left_block}"
    assert right_block in sedimentary_blocks, \
        f"Right (sedimentary) column at y_mid has {right_block}"


def test_high_flow_produces_thicker_sediment():
    """Higher flow should produce more sediment blocks (gravel)."""
    kw_lo = _tiny_tile()
    kw_lo["flow_tile"][:] = 0.01  # very low flow

    kw_hi = _tiny_tile()
    kw_hi["flow_tile"][:] = 0.8  # high flow

    vol_lo, pal_lo = _build_with_geology(**kw_lo)
    vol_hi, pal_hi = _build_with_geology(**kw_hi)

    r, c = 8, 8
    sy = 100

    # Count sediment-type blocks (gravel, coarse_dirt, dirt) in the near-surface zone
    def count_sediment(vol, pal):
        gravel = pal.idx("gravel")
        coarse = pal.idx("coarse_dirt")
        n = 0
        for y_abs in range(sy - 20, sy - 3):
            y_idx = y_abs - Y_MIN
            if 0 <= y_idx < Y_RANGE and vol[y_idx, r, c] in (gravel, coarse):
                n += 1
        return n

    sed_lo = count_sediment(vol_lo, pal_lo)
    sed_hi = count_sediment(vol_hi, pal_hi)
    assert sed_hi >= sed_lo, \
        f"High-flow sediment ({sed_hi}) should be >= low-flow ({sed_lo})"
