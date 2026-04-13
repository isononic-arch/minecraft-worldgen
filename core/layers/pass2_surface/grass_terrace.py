"""grass_terrace — Partition layer: grass + coarse dirt on moderate slopes.

Phase 2.5 (S48). Spec: PHYSICAL_REALISM_REFACTOR.md §6 table.

Claims pixels where:
  - biome is in LAND_BIOMES
  - slope is moderate (TALUS_DEG_MAX <= cliff_deg < CLIFF_DEG_THRESHOLD area
    is handled by talus/cliff, so this catches everything below talus)
  - cliff_deg >= TERRACE_DEG_MIN and cliff_deg < TALUS_DEG_MIN (from talus layer)
  - pixel is land, unclaimed

Block selection: grass_block dominant. North-facing (north_factor > 0.3) stays
grassy; south-facing gets coarse_dirt patches.
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
from core.layers.pass2_surface.temperate_cliff_face import LAND_BIOMES, SEA_LEVEL_Y

# Slope range: moderate terrain between flat and talus.
TERRACE_DEG_MIN = 8.0
TERRACE_DEG_MAX = 18.0  # matches TALUS_DEG_MIN from talus_apron

# North factor threshold: above this = north-facing (moist, grassy).
NORTH_FACTOR_GRASS = 0.3

# Coarse dirt fraction on south-facing slopes.
SOUTH_COARSE_FRAC = 0.35


class GrassTerrace:
    """Partition layer: grass + coarse dirt on moderate slopes."""

    id = "grass_terrace"
    pass_num = 2
    priority = 30  # after cliff (10) and talus (20)
    kind: LayerKind = "partition"

    def apply(self, ctx: SurfaceContext) -> LayerResult:
        shape = ctx.biome_grid.shape
        block_out = np.full(shape, EMPTY_BLOCK, dtype=object)

        biome_mask = np.zeros(shape, dtype=bool)
        for b in LAND_BIOMES:
            biome_mask |= (ctx.biome_grid == b)

        cliff_deg = ctx.eco_grads.get("cliff_deg")
        if cliff_deg is None:
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        moderate = (cliff_deg >= TERRACE_DEG_MIN) & (cliff_deg < TERRACE_DEG_MAX)

        surface_y = ctx.eco_grads.get("surface_y")
        land = (surface_y > SEA_LEVEL_Y) if surface_y is not None else np.ones(shape, dtype=bool)

        unclaimed = ctx.prior_ownership == 0
        scope = biome_mask & moderate & land & unclaimed

        if not scope.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # North factor drives grass vs coarse_dirt mix.
        north_factor = ctx.eco_grads.get("north_factor")
        noise = ctx.eco_grads.get("noise_b")
        if noise is None:
            noise = np.random.default_rng(46).random(shape).astype(np.float32)

        # Default: grass_block everywhere in scope.
        block_out[scope] = "grass_block"

        # South-facing moderate slopes: scatter coarse_dirt.
        if north_factor is not None:
            south_facing = scope & (north_factor <= NORTH_FACTOR_GRASS)
            coarse = south_facing & (noise < SOUTH_COARSE_FRAC)
            block_out[coarse] = "coarse_dirt"

        modified = scope.copy()
        return make_result(modified, block_out, self.kind, layer_id=0,
                           debug_meta={"terrace_px": int(modified.sum())})
