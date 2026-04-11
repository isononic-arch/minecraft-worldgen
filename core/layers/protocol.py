"""Layer Protocol — Phase 0 scaffolding (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §5.

Layers are Python functions conforming to a Protocol (not a declarative JSON
schema — R1-2 resolution). Each layer belongs to a pass (0-5), has a priority
within its pass, and is either a `partition` (claims pixels exclusively) or
`overlay` (paints on already-claimed pixels).

Composition semantics enforced by `core/surface_pipeline.py`:
  - Partition layers: orchestrator writes `block_output[px] = layer.block[px]`
    only where `prior_ownership[px] == 0`. Then sets ownership to layer_id.
  - Overlay layers: orchestrator writes unconditionally where modified_mask.
    Ownership untouched; overlays tracked via separate overlay_touched bitmask.

Invariants (unit-tested per layer):
  - partition coverage: synthetic tile w/ scope always-true → modified_mask.mean() ≥ 0.99
  - scope isolation:    synthetic tile w/ scope never-true  → modified_mask.mean() == 0
  - threshold regression: byte-identical block_output on fixed synthetic input
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import numpy as np

LayerKind = Literal["partition", "overlay"]

# Sentinel used in block_output for "no block emitted" pixels.
EMPTY_BLOCK = ""


@dataclass
class SurfaceContext:
    """Read-only context passed to every layer.apply() call.

    All grids are (tile_h, tile_w). For Vandir tiles tile_h = tile_w = 512.
    eco_grads contains slope, aspect, north_factor, concavity_norm, flow_tile,
    cliff_deg, wind_exposure, surface_y, plus any derived fields.
    """
    tile_x: int
    tile_z: int
    biome_grid: np.ndarray            # zone codes (uint8) or str object array
    lithology_grid: np.ndarray | None # lithology group id (uint8), None if flag off
    eco_grads: dict[str, np.ndarray]
    column_output: dict[str, Any]     # from Pass 1: surface_y, subsurface stack
    prior_surface: np.ndarray         # block name str object array
    prior_ownership: np.ndarray       # uint16; 0 = unclaimed
    overlay_touched: np.ndarray       # uint8 bitmask; which overlay layers have touched
    variant_hints: dict[str, Any] = field(default_factory=dict)
    debug_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class LayerResult:
    """Output of a layer.apply() call.

    modified_mask[px] == True MUST imply block_output[px] is a real block name.
    modified_mask[px] == False MUST imply block_output[px] is EMPTY_BLOCK.
    The orchestrator asserts this invariant on every layer in debug mode.
    """
    modified_mask: np.ndarray    # bool
    block_output: np.ndarray     # str object array
    kind: LayerKind
    layer_id: int
    debug_meta: dict[str, Any] = field(default_factory=dict)


class Layer(Protocol):
    """Shape convention. Layers are plain Python objects with these attrs."""
    id: str
    pass_num: int
    priority: int
    kind: LayerKind

    def apply(self, ctx: SurfaceContext) -> LayerResult: ...


# ---------------------------------------------------------------------------
# Helpers — result constructors keep the invariants centralized so layer
# implementations don't have to re-derive them.
# ---------------------------------------------------------------------------

def empty_result(
    shape: tuple[int, int], kind: LayerKind, layer_id: int = 0
) -> LayerResult:
    """Build a zero-output LayerResult of the correct shape."""
    return LayerResult(
        modified_mask=np.zeros(shape, dtype=bool),
        block_output=np.full(shape, EMPTY_BLOCK, dtype=object),
        kind=kind,
        layer_id=layer_id,
        debug_meta={},
    )


def make_result(
    modified_mask: np.ndarray,
    block_output: np.ndarray,
    kind: LayerKind,
    layer_id: int = 0,
    debug_meta: dict[str, Any] | None = None,
) -> LayerResult:
    """Construct a LayerResult and validate invariants."""
    if modified_mask.shape != block_output.shape:
        raise ValueError(
            f"shape mismatch: mask {modified_mask.shape} vs block {block_output.shape}"
        )
    if modified_mask.dtype != np.bool_:
        raise ValueError(f"modified_mask must be bool, got {modified_mask.dtype}")
    return LayerResult(
        modified_mask=modified_mask,
        block_output=block_output,
        kind=kind,
        layer_id=layer_id,
        debug_meta=debug_meta or {},
    )
