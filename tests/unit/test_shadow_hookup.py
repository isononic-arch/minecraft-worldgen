"""Phase 0.75 shadow-mode hookup smoke tests (S45).

Purpose: pin the invariant that `run_passes()` called with an empty layer
list is identity — `result.surface` element-wise equals `prior_surface`,
ownership stays all zero, overlays stay untouched. This is the mathematical
guarantee that makes the shadow-mode hookup in
`core/surface_decorator.decorate_surface()` structurally unable to mutate
production output: even if the flag is on, an empty passes iterable means
the outer `for layers in passes:` loop in `run_passes` never executes, and
the initial `PipelineResult(surface=prior_surface.copy(), ...)` is returned
verbatim.

These tests are the canary. If a future refactor changes `run_passes` to
unconditionally mutate state before the loop, or to synthesize a fallback
layer, these tests fail and the shadow hookup is unsafe to keep.

See `PHYSICAL_REALISM_REFACTOR.md §11 Phase 0.75`.
"""
from __future__ import annotations

import numpy as np

from core.layers.protocol import EMPTY_BLOCK, SurfaceContext
from core.surface_pipeline import PipelineResult, run_pass, run_passes


def _make_synthetic_ctx(h: int = 8, w: int = 8) -> SurfaceContext:
    """Build a minimal but realistic SurfaceContext for identity testing.

    Uses a non-trivial `prior_surface` (varied block names per pixel) so a
    regression that clobbered the output with uniform EMPTY_BLOCK would be
    caught, not hidden by coincidence.
    """
    rng = np.random.default_rng(20260411)
    biome_grid = rng.integers(1, 27, size=(h, w), dtype=np.uint8)
    eco_grads = {
        "moisture_index": rng.random((h, w), dtype=np.float32),
        "wind_exposure": rng.random((h, w), dtype=np.float32),
    }
    # A varied prior_surface: alternate between a handful of block names so
    # byte-identity can be checked element-wise, not just by shape.
    palette = np.array(
        ["grass_block", "stone", "moss_block", "coarse_dirt", "gravel"],
        dtype=object,
    )
    idx = rng.integers(0, palette.size, size=(h, w))
    prior_surface = palette[idx]
    return SurfaceContext(
        tile_x=36,
        tile_z=20,
        biome_grid=biome_grid,
        lithology_grid=None,
        eco_grads=eco_grads,
        column_output={"surface_y": np.full((h, w), 80, dtype=np.int16)},
        prior_surface=prior_surface,
        prior_ownership=np.zeros((h, w), dtype=np.uint16),
        overlay_touched=np.zeros((h, w), dtype=np.uint8),
    )


# ---------------------------------------------------------------------------
# Core invariant: empty layer list is identity.
# ---------------------------------------------------------------------------

def test_run_pass_empty_layer_list_is_identity() -> None:
    """run_pass([], ctx) must not mutate the ctx's prior_surface."""
    ctx = _make_synthetic_ctx()
    original = ctx.prior_surface.copy()

    result = run_pass([], ctx)

    assert isinstance(result, PipelineResult)
    # Element-wise equality with the input prior_surface — no pixel touched.
    assert np.array_equal(result.surface, original), (
        "run_pass([], ctx) must return prior_surface unchanged"
    )
    # Ownership must remain all-zero: no partition layer claimed anything.
    assert result.ownership.shape == original.shape
    assert result.ownership.dtype == np.uint16
    assert not result.ownership.any(), "ownership must be all zero on empty pass"
    # Overlay bitmask must remain all-zero: no overlay layer touched anything.
    assert not result.overlay_touched.any(), (
        "overlay_touched must be all zero on empty pass"
    )
    # Debug log must be empty: no layers iterated.
    assert result.per_layer_debug == []


def test_run_passes_empty_iterable_is_identity() -> None:
    """run_passes([], ctx) with zero passes must not mutate prior_surface."""
    ctx = _make_synthetic_ctx()
    original = ctx.prior_surface.copy()

    result = run_passes([], ctx)

    assert np.array_equal(result.surface, original)
    assert not result.ownership.any()
    assert not result.overlay_touched.any()
    assert result.per_layer_debug == []


def test_run_passes_multiple_empty_passes_is_identity() -> None:
    """Two empty passes in a row must still be identity.

    Pins the outer loop in run_passes: even when the iterable has items,
    if every item is an empty sequence, state must roll forward unchanged.
    """
    ctx = _make_synthetic_ctx()
    original = ctx.prior_surface.copy()

    result = run_passes([[], [], []], ctx)

    assert np.array_equal(result.surface, original)
    assert not result.ownership.any()
    assert not result.overlay_touched.any()
    assert result.per_layer_debug == []


def test_run_passes_does_not_alias_prior_surface() -> None:
    """Identity on an empty layer list must return a COPY, not a view.

    If run_passes returned a direct reference to ctx.prior_surface, a
    downstream mutation of result.surface would retroactively corrupt the
    input. The shadow hookup relies on this: it discards the result, but
    ctx.prior_surface is actually surface_blocks.copy() from production,
    so even a view-return here would not leak into production — but the
    test pins the copy semantics anyway so a future refactor doesn't
    silently turn this into an aliasing bug.
    """
    ctx = _make_synthetic_ctx()
    result = run_passes([], ctx)
    # Mutate the result; prior_surface must be unaffected.
    result.surface[0, 0] = "sentinel_mutation"
    assert ctx.prior_surface[0, 0] != "sentinel_mutation", (
        "run_passes([], ctx) must return an independent copy of prior_surface"
    )


# ---------------------------------------------------------------------------
# Sentinel: the EMPTY_BLOCK invariant is still imported correctly. Protects
# against a future refactor that removes the EMPTY_BLOCK sentinel without
# updating the protocol module.
# ---------------------------------------------------------------------------

def test_empty_block_sentinel_still_exists() -> None:
    assert EMPTY_BLOCK == ""
