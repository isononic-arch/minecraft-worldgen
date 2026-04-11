"""Surface pipeline orchestrator — Phase 0 scaffolding (S44).

Walks an ordered list of `Layer` objects per pass, enforces partition/overlay
composition semantics, and returns a final tile surface + ownership map.

Phase 0: skeleton only. Not yet called from production code. Production still
runs through `core/surface_decorator.py` until Phase 2 flips the
`use_new_surface_pipeline` feature flag for temperate-mountain biomes.

Spec: PHYSICAL_REALISM_REFACTOR.md §5 "Layer Protocol" and §11 Phase 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from core.layers.protocol import (
    EMPTY_BLOCK,
    Layer,
    LayerResult,
    SurfaceContext,
)


@dataclass
class PipelineResult:
    """Aggregated output after a pass (or all passes) have run."""
    surface: np.ndarray          # str object — final surface block name per px
    ownership: np.ndarray        # uint16 — partition layer_id per px (0 = unclaimed)
    overlay_touched: np.ndarray  # uint8 — bitmask of overlay layers that touched
    per_layer_debug: list[dict] = field(default_factory=list)


def run_pass(
    layers: Sequence[Layer],
    ctx: SurfaceContext,
    *,
    strict: bool = True,
) -> PipelineResult:
    """Run one pass's ordered layer list.

    Layers are assigned layer_id = 1 + index (0 reserved for 'unclaimed').
    Partition layers write only where ctx.prior_ownership == 0.
    Overlay layers write unconditionally where their modified_mask is set.

    `strict=True` (default) validates every LayerResult against its declared
    kind and shape; turn off only for micro-benchmarks.
    """
    shape = ctx.biome_grid.shape
    surface = ctx.prior_surface.copy()
    ownership = ctx.prior_ownership.copy()
    overlay_touched = ctx.overlay_touched.copy()
    debug: list[dict] = []

    # Orchestrator drives ctx in-place across layers so each layer sees the
    # prior_surface / prior_ownership from the previous layer.
    working_ctx = SurfaceContext(
        tile_x=ctx.tile_x,
        tile_z=ctx.tile_z,
        biome_grid=ctx.biome_grid,
        lithology_grid=ctx.lithology_grid,
        eco_grads=ctx.eco_grads,
        column_output=ctx.column_output,
        prior_surface=surface,
        prior_ownership=ownership,
        overlay_touched=overlay_touched,
        variant_hints=ctx.variant_hints,
        debug_meta=ctx.debug_meta,
    )

    for idx, layer in enumerate(layers):
        layer_id = idx + 1  # 0 is reserved
        result = layer.apply(working_ctx)

        if strict:
            _validate_result(layer, result, shape)

        if result.kind == "partition":
            # Only paint where no earlier partition claimed the pixel.
            claim = result.modified_mask & (ownership == 0)
            surface[claim] = result.block_output[claim]
            ownership[claim] = layer_id
        elif result.kind == "overlay":
            paint = result.modified_mask
            surface[paint] = result.block_output[paint]
            # Bitmask overflow is user's problem past 8 overlays per pass;
            # for MVP (Phase 2 has 2 overlays) this is fine.
            bit = np.uint8(1 << (idx % 8))
            overlay_touched[paint] |= bit
        else:
            raise ValueError(f"layer {layer.id!r}: unknown kind {result.kind!r}")

        # Update the working context views (the arrays are aliased via copy;
        # the layer-facing ctx must reflect latest state for subsequent layers).
        working_ctx.prior_surface = surface
        working_ctx.prior_ownership = ownership
        working_ctx.overlay_touched = overlay_touched

        debug.append({
            "layer_id": layer_id,
            "layer_name": getattr(layer, "id", f"layer_{idx}"),
            "kind": result.kind,
            "touched_px": int(result.modified_mask.sum()),
            **result.debug_meta,
        })

    return PipelineResult(
        surface=surface,
        ownership=ownership,
        overlay_touched=overlay_touched,
        per_layer_debug=debug,
    )


def run_passes(
    passes: Iterable[Sequence[Layer]],
    ctx: SurfaceContext,
    *,
    strict: bool = True,
) -> PipelineResult:
    """Run multiple ordered passes sequentially, carrying state across them."""
    cur_ctx = ctx
    result = PipelineResult(
        surface=ctx.prior_surface.copy(),
        ownership=ctx.prior_ownership.copy(),
        overlay_touched=ctx.overlay_touched.copy(),
    )
    for layers in passes:
        pr = run_pass(layers, cur_ctx, strict=strict)
        # Roll forward state into next pass.
        cur_ctx = SurfaceContext(
            tile_x=cur_ctx.tile_x,
            tile_z=cur_ctx.tile_z,
            biome_grid=cur_ctx.biome_grid,
            lithology_grid=cur_ctx.lithology_grid,
            eco_grads=cur_ctx.eco_grads,
            column_output=cur_ctx.column_output,
            prior_surface=pr.surface,
            prior_ownership=pr.ownership,
            overlay_touched=pr.overlay_touched,
            variant_hints=cur_ctx.variant_hints,
            debug_meta=cur_ctx.debug_meta,
        )
        result.surface = pr.surface
        result.ownership = pr.ownership
        result.overlay_touched = pr.overlay_touched
        result.per_layer_debug.extend(pr.per_layer_debug)
    return result


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

def _validate_result(layer: Layer, result: LayerResult, shape: tuple[int, int]) -> None:
    if result.modified_mask.shape != shape:
        raise ValueError(
            f"layer {layer.id!r}: modified_mask shape {result.modified_mask.shape} "
            f"!= ctx shape {shape}"
        )
    if result.block_output.shape != shape:
        raise ValueError(
            f"layer {layer.id!r}: block_output shape {result.block_output.shape} "
            f"!= ctx shape {shape}"
        )
    if result.kind != layer.kind:
        raise ValueError(
            f"layer {layer.id!r}: declared kind {layer.kind!r} "
            f"but result.kind = {result.kind!r}"
        )
    # Verify the central invariant: every modified pixel has a real block name.
    touched = result.modified_mask
    if touched.any():
        empties = (result.block_output == EMPTY_BLOCK) & touched
        if empties.any():
            n = int(empties.sum())
            raise ValueError(
                f"layer {layer.id!r}: {n} pixels marked modified but "
                f"block_output is EMPTY_BLOCK"
            )


def partition_coverage(ownership: np.ndarray, target_mask: np.ndarray) -> float:
    """Return fraction of target_mask pixels claimed by a partition layer.

    Used by the ≥99% land-pixel coverage unit test (PHYSICAL_REALISM §11 Phase 2
    exit criteria).
    """
    if not target_mask.any():
        return 1.0
    claimed = (ownership != 0) & target_mask
    return float(claimed.sum() / target_mask.sum())
