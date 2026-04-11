"""Phase 0 unit tests — NO_GROW sentinel.

Spec: PHYSICAL_REALISM_REFACTOR.md §6 Pass 4 "No-grow rule" + R3-3.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.layers.protocol import EMPTY_BLOCK
from core.layers.vegetation_blocks import (
    NO_GROW_ALLOWLIST,
    NO_GROW_BLOCKLIST,
    assert_palette_safe,
    validate_no_grow,
)


def test_allowlist_and_blocklist_disjoint():
    assert NO_GROW_ALLOWLIST.isdisjoint(NO_GROW_BLOCKLIST)


def test_validate_no_grow_accepts_safe_output():
    out = np.full((4, 4), "short_grass", dtype=object)
    mask = np.ones((4, 4), dtype=bool)
    validate_no_grow(out, mask, source_layer="test")  # no raise


def test_validate_no_grow_accepts_empty_block():
    out = np.full((4, 4), EMPTY_BLOCK, dtype=object)
    mask = np.zeros((4, 4), dtype=bool)
    validate_no_grow(out, mask, source_layer="test")  # no raise


def test_validate_no_grow_rejects_sapling():
    out = np.full((4, 4), "short_grass", dtype=object)
    out[0, 0] = "oak_sapling"
    mask = np.ones((4, 4), dtype=bool)
    with pytest.raises(ValueError, match="oak_sapling"):
        validate_no_grow(out, mask, source_layer="forest_layer")


def test_assert_palette_safe_catches_crop():
    with pytest.raises(AssertionError, match="wheat"):
        assert_palette_safe(["short_grass", "wheat"], source="bad_palette")


def test_assert_palette_safe_passes_on_clean_palette():
    assert_palette_safe(
        ["short_grass", "fern", "large_fern", "pink_petals"],
        source="clean_palette",
    )
