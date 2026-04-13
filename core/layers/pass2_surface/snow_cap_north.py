"""snow_cap_north — Overlay layer: aspect-driven snow extension on north faces.

Phase 2.75 (S50). Spec: PHYSICAL_REALISM_REFACTOR.md §11 Phase 2.75.

In reality, snow lingers longer on north-facing slopes (less solar radiation).
The base snow system (gap_mask == 7) places snow purely by elevation via the
snow_caps mask. This overlay extends snow downslope on north-facing terrain
where snow_caps_gradient is *below* the gap==7 threshold but north_factor is
high enough that snow would realistically persist.

Scope: pixels where:
  - snow_caps_gradient is in the "near snow line" band (0.20 .. 0.40)
  - north_factor >= 0.55 (north-facing)
  - surface_y > SNOW_EXTENSION_MIN_Y (only high terrain)
  - land (surface_y > sea level)

Block: snow_block with probabilistic dither — probability rises with both
snow_caps_gradient and north_factor. South-facing slopes (low north_factor)
never get extended snow.
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

# Sea level for land check.
SEA_LEVEL_Y = 63

# Minimum MC Y for snow extension (don't paint snow at mid-elevations).
SNOW_EXTENSION_MIN_Y = 160

# Snow caps gradient band: below the gap==7 trigger (~0.40) but close enough
# that north-face accumulation is plausible.
GRAD_MIN = 0.20
GRAD_MAX = 0.40

# North factor threshold: only north-facing slopes get extended snow.
NORTH_FACTOR_MIN = 0.55

# Dither: probability = base_prob * north_boost * grad_boost
# At the most favorable conditions (grad=0.40, nf=1.0): ~85% snow
# At the margins (grad=0.20, nf=0.55): ~15% snow
BASE_PROB = 0.15
NORTH_BOOST_RANGE = 0.70   # nf 0.55→1.0 maps to 0→0.70 added
GRAD_BOOST_RANGE = 0.40    # grad 0.20→0.40 maps to 0→0.40 added


class SnowCapNorth:
    """Overlay layer: extend snow caps onto north-facing slopes."""

    id = "snow_cap_north"
    pass_num = 2
    priority = 55  # after vertical_fluting (50), overlays on any surface
    kind: LayerKind = "overlay"

    def apply(self, ctx: SurfaceContext) -> LayerResult:
        shape = ctx.biome_grid.shape
        block_out = np.full(shape, EMPTY_BLOCK, dtype=object)

        snow_grad = ctx.eco_grads.get("snow_caps_gradient")
        north_factor = ctx.eco_grads.get("north_factor")
        surface_y = ctx.eco_grads.get("surface_y")

        if snow_grad is None or north_factor is None or surface_y is None:
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # Scope: near-snow-line band, north-facing, high elevation, land.
        land = surface_y > SEA_LEVEL_Y
        high = surface_y >= SNOW_EXTENSION_MIN_Y
        in_band = (snow_grad >= GRAD_MIN) & (snow_grad < GRAD_MAX)
        north_facing = north_factor >= NORTH_FACTOR_MIN

        scope = land & high & in_band & north_facing

        if not scope.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # Probabilistic dither: higher north_factor + higher gradient = more snow.
        nf_t = np.clip(
            (north_factor - NORTH_FACTOR_MIN) / (1.0 - NORTH_FACTOR_MIN),
            0.0, 1.0,
        )
        grad_t = np.clip(
            (snow_grad - GRAD_MIN) / (GRAD_MAX - GRAD_MIN),
            0.0, 1.0,
        )
        prob = BASE_PROB + nf_t * NORTH_BOOST_RANGE + grad_t * GRAD_BOOST_RANGE

        noise = ctx.eco_grads.get("noise_b")
        if noise is None:
            noise = np.random.default_rng(57).random(shape).astype(np.float32)

        snow_px = scope & (noise < prob)

        if not snow_px.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        block_out[snow_px] = "snow_block"

        return make_result(snow_px, block_out, self.kind, layer_id=0,
                           debug_meta={"snow_north_px": int(snow_px.sum())})
