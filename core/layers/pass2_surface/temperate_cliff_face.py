"""temperate_cliff_face — Partition layer: rock face on steep temperate slopes.

Phase 2.0 (S48). Spec: PHYSICAL_REALISM_REFACTOR.md §11 Phase 2.0.

Claims pixels where:
  - biome is in LAND_BIOMES
  - cliff_deg >= CLIFF_DEG_THRESHOLD (35°)
  - pixel is land (surface_y > sea_level)

Block selection: lithology group palette with 70/20/10 primary/secondary/accent
scatter, using noise for edge jitter.
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

# Scope: all land biomes where cliff/talus layers apply.
# Every biome with a lithology group mapping gets physical rock treatment.
LAND_BIOMES: frozenset[str] = frozenset({
    "COASTAL_HEATH",
    "TEMPERATE_RAINFOREST",
    "BOREAL_TAIGA",
    "SNOWY_BOREAL_TAIGA",
    "ALPINE_MEADOW",
    "ARCTIC_TUNDRA",
    "FROZEN_FLATS",
    "TEMPERATE_DECIDUOUS",
    "RAINFOREST_COAST",
    "RIPARIAN_WOODLAND",
    "DRY_OAK_SAVANNA",
    "KARST_BARRENS",
    "BIRCH_FOREST",
    "EASTERN_TEMPERATE_COAST",
    "MIXED_FOREST",
    "CONTINENTAL_STEPPE",
    "DRY_PINE_BARRENS",
    "SCRUBBY_HEATHLAND",
    "LUSH_RAINFOREST_COAST",
    "SAND_DUNE_DESERT",
    "DESERT_STEPPE_TRANSITION",
    "SEMI_ARID_SHRUBLAND",
    "DRY_WOODLAND_MAQUIS",
    "TIDAL_JUNGLE_FRINGE",
    "MANGROVE_COAST",
    "FRESHWATER_FEN",
})

# Slope threshold: steep >= 35deg (CLAUDE.md slope class calibration).
CLIFF_DEG_THRESHOLD = 35.0

# Sea level (MC Y).
SEA_LEVEL_Y = 63

# Scatter fractions for lithology palette: primary 70%, secondary 20%, accent 10%.
PRIMARY_FRAC = 0.70
SECONDARY_FRAC = 0.90  # cumulative: 70 + 20

# Default palette when no lithology grid or group found.
_FALLBACK_PALETTE = ["stone", "andesite", "cobblestone"]


class TemperateCliffFace:
    """Partition layer: bare rock on steep temperate cliffs.

    Vectorized: builds per-group masks and paints all pixels in each group
    in one shot rather than per-pixel iteration.
    """

    id = "temperate_cliff_face"
    pass_num = 2
    priority = 10  # high priority — claims cliff pixels first
    kind: LayerKind = "partition"

    def __init__(self, lithology_config: dict | None = None):
        self._group_palettes: dict[int, list[str]] = {}
        if lithology_config:
            groups = lithology_config.get("groups", {})
            for _name, gdef in groups.items():
                gid = gdef.get("id", 0)
                pal = gdef.get("palette", _FALLBACK_PALETTE)
                self._group_palettes[gid] = pal if pal else _FALLBACK_PALETTE

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
        steep = cliff_deg >= CLIFF_DEG_THRESHOLD

        surface_y = ctx.eco_grads.get("surface_y")
        land = (surface_y > SEA_LEVEL_Y) if surface_y is not None else np.ones(shape, dtype=bool)

        unclaimed = ctx.prior_ownership == 0
        scope = biome_mask & steep & land & unclaimed

        if not scope.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # --- Noise for scatter ---
        noise = ctx.eco_grads.get("noise_b")
        if noise is None:
            noise = np.random.default_rng(42).random(shape).astype(np.float32)

        # --- Vectorized block selection per lithology group ---
        litho_grid = ctx.lithology_grid
        if litho_grid is not None:
            group_ids = np.unique(litho_grid[scope])
        else:
            group_ids = [0]

        primary_mask = noise < PRIMARY_FRAC
        secondary_mask = (noise >= PRIMARY_FRAC) & (noise < SECONDARY_FRAC)
        # accent_mask = everything else

        for gid in group_ids:
            gid_int = int(gid)
            palette = self._group_palettes.get(gid_int, _FALLBACK_PALETTE)
            if not palette:
                palette = _FALLBACK_PALETTE

            if litho_grid is not None:
                group_scope = scope & (litho_grid == gid_int)
            else:
                group_scope = scope

            if not group_scope.any():
                continue

            block_out[group_scope & primary_mask] = palette[0]
            block_out[group_scope & secondary_mask] = palette[1 % len(palette)]
            accent = group_scope & ~primary_mask & ~secondary_mask
            block_out[accent] = palette[2 % len(palette)]

        modified = scope.copy()
        return make_result(modified, block_out, self.kind, layer_id=0,
                           debug_meta={"cliff_px": int(modified.sum())})
