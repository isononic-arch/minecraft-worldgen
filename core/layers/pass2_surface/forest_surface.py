"""forest_surface — Partition layer: forest-floor block mix for forested biomes.

Phase 2.5 (S48). Spec: PHYSICAL_REALISM_REFACTOR.md §6 table.

Claims unclaimed land pixels in forested biomes. Block mix driven by
tree_density_hint:
  - High density (>0.6): grass 50%, coarse_dirt 25%, podzol 15%, rooted_dirt 10%
  - Low density (<0.3): grass 90%, coarse_dirt 8%, mossy_cobble 2%
  - Transition: linear interpolation

This is effectively the "catchall" partition layer for forested land — it
claims everything not already taken by cliff/talus/terrace/weathered.
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
from core.layers.pass2_surface.temperate_cliff_face import SEA_LEVEL_Y

# Forested biomes that get the forest-floor treatment.
FORESTED_BIOMES: frozenset[str] = frozenset({
    "MIXED_FOREST",
    "TEMPERATE_RAINFOREST",
    "TEMPERATE_DECIDUOUS",
    "BIRCH_FOREST",
    "BOREAL_TAIGA",
    "SNOWY_BOREAL_TAIGA",
    "LUSH_RAINFOREST_COAST",
    "RAINFOREST_COAST",
    "DRY_PINE_BARRENS",
    "DRY_OAK_SAVANNA",
    "RIPARIAN_WOODLAND",
    "DRY_WOODLAND_MAQUIS",
    "TIDAL_JUNGLE_FRINGE",
})

# Tree density thresholds for mix interpolation.
DENSITY_HIGH = 0.6
DENSITY_LOW = 0.3

# Block palettes: [grass_block_frac, coarse_dirt_frac, podzol_frac, rooted_dirt_frac]
# (mossy_cobble fills the remaining in low-density)
_HIGH_DENSITY = {"grass_block": 0.50, "coarse_dirt": 0.75, "podzol": 0.90}
# rest = rooted_dirt (10%)
_LOW_DENSITY = {"grass_block": 0.90, "coarse_dirt": 0.98}
# rest = mossy_cobblestone (2%)


class ForestSurface:
    """Partition layer: forest-floor mix for forested biomes."""

    id = "forest_surface"
    pass_num = 2
    priority = 40  # last partition — catches remaining forested pixels
    kind: LayerKind = "partition"

    def apply(self, ctx: SurfaceContext) -> LayerResult:
        shape = ctx.biome_grid.shape
        block_out = np.full(shape, EMPTY_BLOCK, dtype=object)

        biome_mask = np.zeros(shape, dtype=bool)
        for b in FORESTED_BIOMES:
            biome_mask |= (ctx.biome_grid == b)

        surface_y = ctx.eco_grads.get("surface_y")
        land = (surface_y > SEA_LEVEL_Y) if surface_y is not None else np.ones(shape, dtype=bool)

        unclaimed = ctx.prior_ownership == 0
        scope = biome_mask & land & unclaimed

        if not scope.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        noise = ctx.eco_grads.get("noise_b")
        if noise is None:
            noise = np.random.default_rng(48).random(shape).astype(np.float32)

        # Tree density hint — drives palette interpolation.
        tree_hint = ctx.eco_grads.get("tree_density_hint")
        if tree_hint is None:
            # Fallback: assume moderate density everywhere.
            tree_hint = np.full(shape, 0.45, dtype=np.float32)

        # Vectorized palette: compute per-pixel thresholds by interpolating
        # between high-density and low-density palettes based on tree_hint.
        t = np.clip((tree_hint - DENSITY_LOW) / (DENSITY_HIGH - DENSITY_LOW), 0.0, 1.0)

        # Interpolate cumulative thresholds.
        grass_thr = _LOW_DENSITY["grass_block"] + t * (_HIGH_DENSITY["grass_block"] - _LOW_DENSITY["grass_block"])
        coarse_thr = _LOW_DENSITY["coarse_dirt"] + t * (_HIGH_DENSITY["coarse_dirt"] - _LOW_DENSITY["coarse_dirt"])
        podzol_thr = coarse_thr + t * (_HIGH_DENSITY["podzol"] - _HIGH_DENSITY["coarse_dirt"])

        # Default: grass_block.
        block_out[scope] = "grass_block"

        # Coarse dirt.
        coarse = scope & (noise >= grass_thr) & (noise < coarse_thr)
        block_out[coarse] = "coarse_dirt"

        # Podzol (only where tree density is meaningful).
        podzol = scope & (noise >= coarse_thr) & (noise < podzol_thr)
        block_out[podzol] = "podzol"

        # Rooted dirt (high density remainder) or mossy_cobblestone (low density remainder).
        remainder = scope & (noise >= podzol_thr)
        high_dens = remainder & (tree_hint >= DENSITY_HIGH)
        low_dens = remainder & (tree_hint < DENSITY_HIGH)
        block_out[high_dens] = "rooted_dirt"
        block_out[low_dens] = "mossy_cobblestone"

        modified = scope.copy()
        return make_result(modified, block_out, self.kind, layer_id=0,
                           debug_meta={"forest_px": int(modified.sum())})
