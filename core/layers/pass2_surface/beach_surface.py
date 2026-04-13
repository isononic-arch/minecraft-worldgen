"""beach_surface — Partition layer: sand/gravel beaches at ocean coastlines.

Phase 2.75 (S50). Spec: PHYSICAL_REALISM_REFACTOR.md §11 Phase 2.75.

Beaches exist where flat, low-elevation land meets ocean. No wave_fetch mask
needed — derived purely from physical signals:
  - surface_y near sea level (63–68 MC Y)
  - low slope (cliff_deg < 10°)
  - adjacent to ocean (within ~6px of ocean pixels, computed via EDT)

Block: sand (all coasts). Edge dither: placement probability fades with
distance from ocean.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import distance_transform_edt

from core.layers.protocol import (
    EMPTY_BLOCK,
    LayerKind,
    LayerResult,
    SurfaceContext,
    make_result,
)

SEA_LEVEL_Y = 63

# Max MC Y for beach eligibility (beaches are at or just above sea level).
BEACH_MAX_Y = 68

# Max slope for beach formation.
BEACH_MAX_SLOPE_DEG = 10.0

# Maximum distance from ocean (in pixels) for beach placement.
# At 1:1 scale each pixel = 1 block; at tile scale (512x512) this is cheap.
OCEAN_DIST_MAX_PX = 6

# Biomes that should NEVER get beach treatment (already have their own
# coastal logic or are interior).
EXCLUDED_BIOMES: frozenset[str] = frozenset({
    "SAND_DUNE_DESERT",  # has its own sand system via gap_mask==8
    "MANGROVE_COAST",    # mangrove mud, not sandy beach
})


class BeachSurface:
    """Partition layer: sand/gravel beaches at ocean shorelines."""

    id = "beach_surface"
    pass_num = 2
    priority = 38  # after weathered_top (35), before forest_surface slot (40)
    kind: LayerKind = "partition"

    def apply(self, ctx: SurfaceContext) -> LayerResult:
        shape = ctx.biome_grid.shape
        block_out = np.full(shape, EMPTY_BLOCK, dtype=object)

        surface_y = ctx.eco_grads.get("surface_y")
        cliff_deg = ctx.eco_grads.get("cliff_deg")

        if surface_y is None:
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # Derive ocean adjacency from surface_y.
        ocean = surface_y <= SEA_LEVEL_Y
        if not ocean.any():
            # No ocean in this tile — no beaches.
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # Distance from ocean pixels (in blocks/pixels).
        dist_from_ocean = distance_transform_edt(~ocean).astype(np.float32)

        # Scope: land, low elevation, flat, near ocean, unclaimed.
        land = surface_y > SEA_LEVEL_Y
        low_elev = surface_y <= BEACH_MAX_Y
        flat = (cliff_deg < BEACH_MAX_SLOPE_DEG) if cliff_deg is not None else np.ones(shape, dtype=bool)
        near_ocean = dist_from_ocean <= OCEAN_DIST_MAX_PX
        unclaimed = ctx.prior_ownership == 0

        # Biome exclusion.
        excluded = np.zeros(shape, dtype=bool)
        for b in EXCLUDED_BIOMES:
            excluded |= (ctx.biome_grid == b)

        scope = land & low_elev & flat & near_ocean & ~excluded & unclaimed

        if not scope.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # Dither: probability fades with distance from ocean.
        # At ocean edge (dist=1): 95%. At max distance (dist=6): 25%.
        dist_t = np.clip(dist_from_ocean / OCEAN_DIST_MAX_PX, 0.0, 1.0)
        place_prob = 0.95 - dist_t * 0.70

        noise = ctx.eco_grads.get("noise_b")
        if noise is None:
            noise = np.random.default_rng(60).random(shape).astype(np.float32)

        placed = scope & (noise < place_prob)

        if not placed.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # All beaches are sand.
        block_out[placed] = "sand"

        return make_result(placed, block_out, self.kind, layer_id=0,
                           debug_meta={"beach_px": int(placed.sum())})
