"""desert_pavement — Partition layer: wind-scoured flat arid ground.

Phase 2.75 (S50). Spec: PHYSICAL_REALISM_REFACTOR.md §11 Phase 2.75.

In real deserts, wind removes fine sand from flat exposed ground, leaving a
surface of tightly packed pebbles, cobbles, and crusted soil. This layer breaks
up the monotone sand/dirt in arid biomes that are NOT sand dune desert (gap==8).

Scope: pixels where:
  - biome is arid (DESERT_STEPPE_TRANSITION, SEMI_ARID_SHRUBLAND, DRY_OAK_SAVANNA,
    DRY_WOODLAND_MAQUIS, DRY_PINE_BARRENS, DESERT_ROCK, COASTAL_HEATH)
  - NOT SAND_DUNE_DESERT (has its own system)
  - cliff_deg < 18° (flat to moderate)
  - land (surface_y > sea level)
  - unclaimed by prior partition layers

Block palette (probabilistic scatter):
  - 35% coarse_dirt
  - 25% packed_mud
  - 15% dead_bush (placed as ground cover concept — surface stays coarse_dirt)
  - 15% short_dry_grass (single block, NOT double-stacked)
  - 10% tall_dry_grass (single block, NOT double-stacked)

Note: dead_bush, short_dry_grass, tall_dry_grass are surface-level blocks here.
The layer places a base surface (coarse_dirt or packed_mud) and the vegetation
items are scattered on top via the block_output. However, since this is a
surface block layer (not ground cover), we treat the vegetation items as surface
block overrides — the column generator will place them correctly.

Actually: dead_bush / short_dry_grass / tall_dry_grass are ground-cover items
(placed at Y+1). This layer paints the SURFACE BLOCK (at Y) as coarse_dirt or
packed_mud. The dry vegetation should be handled by ground_cover palettes in
the legacy decorator. So this layer only places coarse_dirt and packed_mud.
"""
from __future__ import annotations

import numpy as np

from core.layers.protocol import (
    EMPTY_BLOCK,
    LayerKind,
    LayerResult,
    SurfaceContext,
    make_result,
)

SEA_LEVEL_Y = 63

# Arid biomes for desert pavement (NOT sand dune desert).
PAVEMENT_BIOMES: frozenset[str] = frozenset({
    "DESERT_STEPPE_TRANSITION",
    "SEMI_ARID_SHRUBLAND",
    "DRY_OAK_SAVANNA",
    "DRY_WOODLAND_MAQUIS",
    "DRY_PINE_BARRENS",
    "DESERT_ROCK",
    "COASTAL_HEATH",
})

# Maximum slope for pavement formation.
MAX_SLOPE_DEG = 18.0

# Block palette cumulative thresholds.
# 0.00–0.55 = coarse_dirt, 0.55–1.00 = packed_mud
COARSE_DIRT_THR = 0.55


class DesertPavement:
    """Partition layer: wind-scoured flat arid ground."""

    id = "desert_pavement"
    pass_num = 2
    priority = 43  # after river_bar (42), before vertical_fluting (50)
    kind: LayerKind = "partition"

    def apply(self, ctx: SurfaceContext) -> LayerResult:
        shape = ctx.biome_grid.shape
        block_out = np.full(shape, EMPTY_BLOCK, dtype=object)

        cliff_deg = ctx.eco_grads.get("cliff_deg")
        surface_y = ctx.eco_grads.get("surface_y")

        if surface_y is None:
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # Biome filter.
        biome_mask = np.zeros(shape, dtype=bool)
        for b in PAVEMENT_BIOMES:
            biome_mask |= (ctx.biome_grid == b)

        if not biome_mask.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        land = surface_y > SEA_LEVEL_Y
        flat = (cliff_deg < MAX_SLOPE_DEG) if cliff_deg is not None else np.ones(shape, dtype=bool)
        unclaimed = ctx.prior_ownership == 0

        scope = biome_mask & land & flat & unclaimed

        if not scope.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # Dither: not all flat arid ground is pavement. Use moisture_index
        # as a driver — drier = more pavement.
        moisture = ctx.eco_grads.get("moisture_index")
        if moisture is not None:
            # Low moisture → high placement. Invert: place_prob = 1 - moisture.
            # Cap at 70% max placement to leave some normal biome surface.
            place_prob = np.clip(0.70 - moisture * 0.50, 0.15, 0.70)
        else:
            place_prob = np.full(shape, 0.50, dtype=np.float32)

        noise = ctx.eco_grads.get("noise_b")
        if noise is None:
            noise = np.random.default_rng(62).random(shape).astype(np.float32)

        placed = scope & (noise < place_prob)

        if not placed.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # Block selection.
        rng = np.random.default_rng(63)
        block_noise = rng.random(shape).astype(np.float32)

        block_out[placed] = "packed_mud"  # default (45%)
        block_out[placed & (block_noise < COARSE_DIRT_THR)] = "coarse_dirt"  # 55%

        return make_result(placed, block_out, self.kind, layer_id=0,
                           debug_meta={"pavement_px": int(placed.sum())})
