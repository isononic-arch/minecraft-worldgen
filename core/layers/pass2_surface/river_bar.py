"""river_bar — Partition layer: gravel/sand/mud bars in arid riverbeds.

Phase 2.75 (S50). Spec: PHYSICAL_REALISM_REFACTOR.md §11 Phase 2.75.

In arid landscapes, dry riverbeds (wadis) develop exposed sediment bars —
coarse material deposited when the river flowed. This layer places coarse_dirt,
packed_mud, and sand scatter on flat terrain near river channels in arid biomes,
giving dry riverbeds visible texture instead of blending into the desert floor.

Scope: pixels where:
  - biome is arid (SAND_DUNE_DESERT, DESERT_STEPPE_TRANSITION, SEMI_ARID_SHRUBLAND,
    DRY_OAK_SAVANNA, DRY_WOODLAND_MAQUIS, DRY_PINE_BARRENS, DESERT_ROCK)
  - riparian_proximity >= 0.4 (near river channel)
  - cliff_deg < 18 (flat to moderate — bars don't form on steep banks)
  - land (surface_y > sea level)
  - unclaimed by prior partition layers

Block palette (probabilistic scatter):
  - Non-dune arid biomes: 40% coarse_dirt, 30% packed_mud, 30% sand
  - SAND_DUNE_DESERT: 55% coarse_dirt, 45% packed_mud (no sand — already sand
    everywhere, river bars are the compacted/exposed sediment)
Probability of placement dithers with riparian_proximity — strongest right
at the channel edge, fading out toward the corridor boundary.
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

# Arid biomes where river bars form.
ARID_BIOMES: frozenset[str] = frozenset({
    "SAND_DUNE_DESERT",
    "DESERT_STEPPE_TRANSITION",
    "SEMI_ARID_SHRUBLAND",
    "DRY_OAK_SAVANNA",
    "DRY_WOODLAND_MAQUIS",
    "DRY_PINE_BARRENS",
    "DESERT_ROCK",
})

# Minimum riparian proximity to place bars (1.0 = at channel, 0.0 = far).
RIP_PROX_MIN = 0.4

# Maximum slope for bar formation.
MAX_SLOPE_DEG = 18.0

# Block palette cumulative thresholds (noise-based selection).
# 0.00–0.40 = coarse_dirt, 0.40–0.70 = packed_mud, 0.70–1.00 = sand
COARSE_DIRT_THR = 0.40
PACKED_MUD_THR = 0.70
# remainder = sand


class RiverBar:
    """Partition layer: sediment bars in arid riverbeds."""

    id = "river_bar"
    pass_num = 2
    priority = 42  # after weathered_top (35), before vertical_fluting (50)
    kind: LayerKind = "partition"

    def apply(self, ctx: SurfaceContext) -> LayerResult:
        shape = ctx.biome_grid.shape
        block_out = np.full(shape, EMPTY_BLOCK, dtype=object)

        rip_prox = ctx.eco_grads.get("riparian_proximity")
        cliff_deg = ctx.eco_grads.get("cliff_deg")
        surface_y = ctx.eco_grads.get("surface_y")

        if rip_prox is None or surface_y is None:
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # Biome filter.
        biome_mask = np.zeros(shape, dtype=bool)
        for b in ARID_BIOMES:
            biome_mask |= (ctx.biome_grid == b)

        if not biome_mask.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        land = surface_y > SEA_LEVEL_Y
        near_channel = rip_prox >= RIP_PROX_MIN
        flat = (cliff_deg < MAX_SLOPE_DEG) if cliff_deg is not None else np.ones(shape, dtype=bool)
        unclaimed = ctx.prior_ownership == 0

        scope = biome_mask & land & near_channel & flat & unclaimed

        if not scope.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # Dither: placement probability scales with riparian proximity.
        # At channel edge (rip=1.0): 90% placement. At threshold (rip=0.4): 30%.
        rip_t = np.clip((rip_prox - RIP_PROX_MIN) / (1.0 - RIP_PROX_MIN), 0.0, 1.0)
        place_prob = 0.30 + rip_t * 0.60

        noise = ctx.eco_grads.get("noise_b")
        if noise is None:
            noise = np.random.default_rng(58).random(shape).astype(np.float32)

        placed = scope & (noise < place_prob)

        if not placed.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # Block selection: second noise field for palette scatter.
        rng = np.random.default_rng(59)
        block_noise = rng.random(shape).astype(np.float32)

        # Dune desert: coarse_dirt/packed_mud only (no sand — already sand everywhere).
        dune = np.zeros(shape, dtype=bool)
        dune |= (ctx.biome_grid == "SAND_DUNE_DESERT")
        dune_placed = placed & dune
        other_placed = placed & ~dune

        if dune_placed.any():
            block_out[dune_placed] = "packed_mud"  # 45%
            block_out[dune_placed & (block_noise < COARSE_DIRT_THR)] = "coarse_dirt"  # 55%

        if other_placed.any():
            block_out[other_placed] = "sand"  # 30%
            block_out[other_placed & (block_noise < PACKED_MUD_THR)] = "packed_mud"
            block_out[other_placed & (block_noise < COARSE_DIRT_THR)] = "coarse_dirt"

        return make_result(placed, block_out, self.kind, layer_id=0,
                           debug_meta={"river_bar_px": int(placed.sum())})
