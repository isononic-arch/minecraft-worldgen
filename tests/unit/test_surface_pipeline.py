"""Phase 0 unit tests — surface pipeline orchestrator.

Spec: PHYSICAL_REALISM_REFACTOR.md §5 "Layer Protocol" composition semantics
and §11 Phase 0 exit criteria "orchestrator runs on empty layer list over a
synthetic tile and produces correct zero-output".

Tests partition coverage, overlay preservation, empty-list zero-output,
and the central invariant that a modified pixel always has a real block name.
"""
from __future__ import annotations

import numpy as np
import pytest

from core.layers.protocol import (
    EMPTY_BLOCK,
    LayerResult,
    SurfaceContext,
    make_result,
)
from core.surface_pipeline import partition_coverage, run_pass, run_passes


TILE_H = TILE_W = 16  # tiny synthetic tile — keeps tests fast


def _empty_ctx(tile_x: int = 0, tile_z: int = 0) -> SurfaceContext:
    shape = (TILE_H, TILE_W)
    return SurfaceContext(
        tile_x=tile_x,
        tile_z=tile_z,
        biome_grid=np.full(shape, "TEST", dtype=object),
        lithology_grid=None,
        eco_grads={},
        column_output={},
        prior_surface=np.full(shape, EMPTY_BLOCK, dtype=object),
        prior_ownership=np.zeros(shape, dtype=np.uint16),
        overlay_touched=np.zeros(shape, dtype=np.uint8),
    )


class _ConstantLayer:
    """Test helper: emits `block` on pixels where `scope_fn(ctx)` is True."""

    def __init__(self, layer_id_str: str, kind: str, scope_fn, block: str):
        self.id = layer_id_str
        self.pass_num = 2
        self.priority = 0
        self.kind = kind
        self._scope_fn = scope_fn
        self._block = block

    def apply(self, ctx: SurfaceContext) -> LayerResult:
        mask = self._scope_fn(ctx)
        out = np.full(ctx.biome_grid.shape, EMPTY_BLOCK, dtype=object)
        out[mask] = self._block
        return make_result(mask, out, self.kind)


# ---------------------------------------------------------------------------
# Empty-list zero-output (Phase 0 exit criterion)
# ---------------------------------------------------------------------------

def test_run_pass_empty_list_yields_zero_output():
    ctx = _empty_ctx()
    result = run_pass([], ctx)
    assert (result.ownership == 0).all()
    assert (result.surface == EMPTY_BLOCK).all()
    assert (result.overlay_touched == 0).all()
    assert result.per_layer_debug == []


def test_run_passes_empty_passes_list():
    ctx = _empty_ctx()
    result = run_passes([], ctx)
    assert (result.ownership == 0).all()


# ---------------------------------------------------------------------------
# Partition composition semantics
# ---------------------------------------------------------------------------

def test_partition_claims_are_exclusive():
    """Second partition layer cannot overwrite first layer's pixels."""
    ctx = _empty_ctx()

    # Layer A claims left half.
    def left_half(ctx):
        m = np.zeros((TILE_H, TILE_W), dtype=bool)
        m[:, : TILE_W // 2] = True
        return m

    # Layer B tries to claim everything.
    def all_pixels(ctx):
        return np.ones((TILE_H, TILE_W), dtype=bool)

    layers = [
        _ConstantLayer("A", "partition", left_half, "stone"),
        _ConstantLayer("B", "partition", all_pixels, "dirt"),
    ]
    result = run_pass(layers, ctx)

    # Left half → stone (claimed first), right half → dirt (claimed second).
    assert (result.surface[:, : TILE_W // 2] == "stone").all()
    assert (result.surface[:, TILE_W // 2 :] == "dirt").all()
    assert (result.ownership[:, : TILE_W // 2] == 1).all()
    assert (result.ownership[:, TILE_W // 2 :] == 2).all()


def test_partition_coverage_invariant():
    """Coverage helper reports ≥99% when layers partition the tile fully."""
    ctx = _empty_ctx()
    target = np.ones((TILE_H, TILE_W), dtype=bool)  # treat entire tile as land

    def all_pixels(ctx):
        return np.ones((TILE_H, TILE_W), dtype=bool)

    layers = [_ConstantLayer("A", "partition", all_pixels, "stone")]
    result = run_pass(layers, ctx)
    cov = partition_coverage(result.ownership, target)
    assert cov == 1.0


# ---------------------------------------------------------------------------
# Overlay composition semantics
# ---------------------------------------------------------------------------

def test_overlay_paints_on_top_without_losing_ownership():
    """Overlay writes block_output but does not modify ownership."""
    ctx = _empty_ctx()

    def all_pixels(ctx):
        return np.ones((TILE_H, TILE_W), dtype=bool)

    def top_row(ctx):
        m = np.zeros((TILE_H, TILE_W), dtype=bool)
        m[0, :] = True
        return m

    layers = [
        _ConstantLayer("base", "partition", all_pixels, "stone"),
        _ConstantLayer("snow_cap", "overlay", top_row, "snow"),
    ]
    result = run_pass(layers, ctx)

    # Ownership stays at the partition layer (1) everywhere, even where the
    # overlay painted.
    assert (result.ownership == 1).all()
    # Top row is now snow on the surface; the rest is still stone.
    assert (result.surface[0, :] == "snow").all()
    assert (result.surface[1:, :] == "stone").all()
    # Overlay touched bitmask flipped on top row only.
    assert (result.overlay_touched[0, :] != 0).all()
    assert (result.overlay_touched[1:, :] == 0).all()


# ---------------------------------------------------------------------------
# Invariant enforcement
# ---------------------------------------------------------------------------

def test_invariant_rejects_touched_but_empty_block():
    """A layer that claims pixels but emits EMPTY_BLOCK there must be flagged."""
    ctx = _empty_ctx()

    class BadLayer:
        id = "bad"
        pass_num = 2
        priority = 0
        kind = "partition"

        def apply(self, ctx):
            mask = np.ones((TILE_H, TILE_W), dtype=bool)
            # Intentionally forget to set block_output — all EMPTY_BLOCK.
            out = np.full((TILE_H, TILE_W), EMPTY_BLOCK, dtype=object)
            return LayerResult(
                modified_mask=mask,
                block_output=out,
                kind="partition",
                layer_id=0,
            )

    with pytest.raises(ValueError, match="EMPTY_BLOCK"):
        run_pass([BadLayer()], ctx, strict=True)


def test_invariant_rejects_kind_mismatch():
    ctx = _empty_ctx()

    class MismatchLayer:
        id = "mismatch"
        pass_num = 2
        priority = 0
        kind = "partition"  # declared partition

        def apply(self, ctx):
            mask = np.zeros((TILE_H, TILE_W), dtype=bool)
            out = np.full((TILE_H, TILE_W), EMPTY_BLOCK, dtype=object)
            return LayerResult(
                modified_mask=mask,
                block_output=out,
                kind="overlay",  # ...but claims overlay on return
                layer_id=0,
            )

    with pytest.raises(ValueError, match="declared kind"):
        run_pass([MismatchLayer()], ctx, strict=True)
