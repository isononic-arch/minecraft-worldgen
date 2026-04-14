"""temperate_talus_apron — Partition layer: loose debris below cliff faces.

Phase 2.0 (S48). Spec: PHYSICAL_REALISM_REFACTOR.md §11 Phase 2.0.

Claims pixels where:
  - biome is in LAND_BIOMES
  - 18° <= cliff_deg < 35° (moderate slope, below cliff threshold)
  - concavity_norm > 0 (concave = material accumulation zone)
  - pixel is land
  - NOT near a river (riparian_proximity < threshold) — S54 fix: river carving
    creates channel drops that read as moderate slopes, overwriting riparian dither.

Block selection: cobblestone + gravel scatter (50/50 by noise), matching the
legacy talus zone behavior but driven purely by physical signals.
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

# Reuse the same biome scope as cliff_face.
from core.layers.pass2_surface.temperate_cliff_face import (
    LAND_BIOMES,
    SEA_LEVEL_Y,
)

# Slope thresholds for talus apron (moderate slope class, CLAUDE.md).
TALUS_DEG_MIN = 18.0
TALUS_DEG_MAX = 35.0

# Concavity threshold — positive = concave = deposition zone.
CONCAVITY_THRESHOLD = 0.0

# Gravel fraction within talus scatter.
GRAVEL_FRAC = 0.50

# Riparian exclusion: pixels with riparian_proximity >= this threshold are
# excluded from talus painting.  Same rationale as temperate_cliff_face.  S54 fix.
RIPARIAN_EXCLUSION_THRESHOLD = 0.3

# Scatter density — fraction of scope pixels that get talus blocks.
# Rest stay as prior surface (claimed but passthrough, so partition still works).
SCATTER_DENSITY = 0.60


class TemperateTalusApron:
    """Partition layer: cobblestone + gravel talus at cliff bases."""

    id = "temperate_talus_apron"
    pass_num = 2
    priority = 20  # lower priority than cliff_face (runs after)
    kind: LayerKind = "partition"

    def apply(self, ctx: SurfaceContext) -> LayerResult:
        shape = ctx.biome_grid.shape
        block_out = np.full(shape, EMPTY_BLOCK, dtype=object)

        # --- Scope mask ---
        biome_mask = np.zeros(shape, dtype=bool)
        for b in LAND_BIOMES:
            biome_mask |= (ctx.biome_grid == b)

        cliff_deg = ctx.eco_grads.get("cliff_deg")
        if cliff_deg is None:
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        moderate = (cliff_deg >= TALUS_DEG_MIN) & (cliff_deg < TALUS_DEG_MAX)

        # Concavity: positive = concave depression where debris accumulates.
        concavity = ctx.eco_grads.get("concavity_norm")
        if concavity is not None:
            concave = concavity > CONCAVITY_THRESHOLD
        else:
            # Without concavity data, fall back to slope-only scope.
            concave = np.ones(shape, dtype=bool)

        surface_y = ctx.eco_grads.get("surface_y")
        land = (surface_y > SEA_LEVEL_Y) if surface_y is not None else np.ones(shape, dtype=bool)

        # S54: exclude river-adjacent pixels — carved channels create steep banks
        # that are not real geological slopes.
        rip = ctx.eco_grads.get("riparian_proximity")
        not_riparian = (rip < RIPARIAN_EXCLUSION_THRESHOLD) if rip is not None else np.ones(shape, dtype=bool)

        unclaimed = ctx.prior_ownership == 0
        scope = biome_mask & moderate & concave & land & not_riparian & unclaimed

        if not scope.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # --- Scatter ---
        noise = ctx.eco_grads.get("noise_b")
        if noise is None:
            noise = np.random.default_rng(43).random(shape).astype(np.float32)

        noise_c = ctx.eco_grads.get("noise_c")
        if noise_c is None:
            noise_c = np.random.default_rng(44).random(shape).astype(np.float32)

        scatter = scope & (noise < SCATTER_DENSITY)
        gravel = scatter & (noise_c < GRAVEL_FRAC)
        cobble = scatter & ~gravel

        block_out[gravel] = "gravel"
        block_out[cobble] = "cobblestone"

        # Non-scatter scope pixels: claim them with prior surface block to prevent
        # other partition layers from overwriting, but keep original block.
        passthrough = scope & ~scatter
        block_out[passthrough] = ctx.prior_surface[passthrough]

        modified = scope.copy()
        return make_result(modified, block_out, self.kind, layer_id=0,
                           debug_meta={
                               "talus_scatter_px": int(scatter.sum()),
                               "talus_passthrough_px": int(passthrough.sum()),
                           })
