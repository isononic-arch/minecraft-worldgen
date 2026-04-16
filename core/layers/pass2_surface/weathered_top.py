"""weathered_top — Partition layer: windswept exposed rock/grass at high elevation.

Phase 2.5 (S48). Spec: PHYSICAL_REALISM_REFACTOR.md §6 table.

Claims pixels where:
  - biome is in LAND_BIOMES
  - slope_class == flat (cliff_deg < TERRACE_DEG_MIN from grass_terrace)
  - high elevation (surface_y >= EXPOSED_MIN_Y)
  - wind_exposure > WIND_THRESHOLD (exposed ridgetops)
  - unclaimed

Block selection: mossy_cobblestone + stone + grass_block mix, driven by
wind_exposure intensity.
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
from core.layers.pass2_surface.grass_terrace import LAND_BIOMES, SEA_LEVEL_Y

# Elevation threshold for "high enough to be windswept".
EXPOSED_MIN_Y = 180

# Wind exposure threshold.
WIND_THRESHOLD = 0.5

# Block fractions for the windswept mix.
STONE_FRAC = 0.25
MOSSY_COBBLE_FRAC = 0.55  # cumulative: 25% stone + 30% mossy_cobble
# rest = grass_block (45%)


class WeatheredTop:
    """Partition layer: windswept mossy rock on exposed high-elevation flats."""

    id = "weathered_top"
    pass_num = 2
    priority = 35  # after terrace (30), before forest (40)
    kind: LayerKind = "partition"

    def apply(self, ctx: SurfaceContext) -> LayerResult:
        shape = ctx.biome_grid.shape
        block_out = np.full(shape, EMPTY_BLOCK, dtype=object)

        biome_mask = np.zeros(shape, dtype=bool)
        for b in LAND_BIOMES:
            biome_mask |= (ctx.biome_grid == b)

        surface_y = ctx.eco_grads.get("surface_y")
        if surface_y is None:
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        high_elev = surface_y >= EXPOSED_MIN_Y
        land = surface_y > SEA_LEVEL_Y

        wind_exposure = ctx.eco_grads.get("wind_exposure")
        if wind_exposure is None:
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        exposed = wind_exposure > WIND_THRESHOLD

        # Flat slope (below terrace range).
        cliff_deg = ctx.eco_grads.get("cliff_deg")
        if cliff_deg is not None:
            flat = cliff_deg < 8.0  # below TERRACE_DEG_MIN
        else:
            flat = np.ones(shape, dtype=bool)

        unclaimed = ctx.prior_ownership == 0
        scope = biome_mask & high_elev & exposed & flat & land & unclaimed

        if not scope.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        noise = ctx.eco_grads.get("noise_b")
        if noise is None:
            noise = np.random.default_rng(47).random(shape).astype(np.float32)

        # Windswept mix: stone + mossy_cobblestone + grass_block.
        block_out[scope] = "grass_block"  # default
        block_out[scope & (noise < STONE_FRAC)] = "stone"
        mossy = scope & (noise >= STONE_FRAC) & (noise < MOSSY_COBBLE_FRAC)
        block_out[mossy] = "mossy_cobblestone"

        modified = scope.copy()
        return make_result(modified, block_out, self.kind, layer_id=0,
                           debug_meta={"weathered_px": int(modified.sum())})
