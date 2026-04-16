"""grass_terrace — Partition layer: biome-aware block mix on moderate slopes.

Phase 2.5 (S48), revised S52 for biome-aware selection.
Spec: PHYSICAL_REALISM_REFACTOR.md §6 table.

Claims pixels where:
  - biome is in LAND_BIOMES
  - slope is moderate (TERRACE_DEG_MIN <= cliff_deg < TERRACE_DEG_MAX)
  - pixel is land, unclaimed
  - gap_mask not in protected set (snow, rock, sand_dune, beach)

Block selection is BIOME-AWARE: instead of uniform grass_block, the layer
reads prior_surface (what noise_layers + legacy decorator already placed)
and dithers in weathered variants appropriate to each biome category.
Every block in a 3×3 should look different — small-scale probabilistic
scatter, not uniform fills.

Biome categories:
  - Desert/arid: sand base + red_sand/sandstone/coarse_dirt scatter
  - Moss biomes (rainforest): moss_block base + podzol/coarse_dirt scatter
  - Boreal/taiga: podzol base + coarse_dirt/dirt scatter
  - Steppe/dry woodland: coarse_dirt base + gravel/dirt scatter
  - Temperate forest/meadow: grass_block base + coarse_dirt/podzol scatter
  - Arctic/frozen: snow_block base + gravel/stone scatter
  - Alpine meadow: grass_block base + coarse_dirt/gravel scatter
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
# Previously imported from temperate_cliff_face (deleted S56). Inlined.
SEA_LEVEL_Y = 63
LAND_BIOMES: frozenset[str] = frozenset({
    "COASTAL_HEATH", "TEMPERATE_RAINFOREST", "BOREAL_TAIGA",
    "SNOWY_BOREAL_TAIGA", "ARCTIC_TUNDRA", "FROZEN_FLATS",
    "TEMPERATE_DECIDUOUS", "RAINFOREST_COAST", "RIPARIAN_WOODLAND",
    "DRY_OAK_SAVANNA", "KARST_BARRENS", "BIRCH_FOREST",
    "EASTERN_TEMPERATE_COAST", "MIXED_FOREST", "CONTINENTAL_STEPPE",
    "DRY_PINE_BARRENS", "SCRUBBY_HEATHLAND", "LUSH_RAINFOREST_COAST",
    "SAND_DUNE_DESERT", "DESERT_STEPPE_TRANSITION", "SEMI_ARID_SHRUBLAND",
    "DRY_WOODLAND_MAQUIS", "TIDAL_JUNGLE_FRINGE", "MANGROVE_COAST",
    "FRESHWATER_FEN",
})

# Slope range: moderate terrain between flat and talus.
TERRACE_DEG_MIN = 8.0
TERRACE_DEG_MAX = 18.0  # matches TALUS_DEG_MIN from talus_apron

# North factor threshold: above this = north-facing (moist, grassy).
NORTH_FACTOR_GRASS = 0.3

# Gap mask values to SKIP — these zones have their own dedicated handlers
# and terrace logic must never overwrite them.
_PROTECTED_GAPS = frozenset({4, 5, 7, 8, 9})
# 4=floodplain, 5=rock, 7=snow, 8=sand_dune, 9=beach (gap==6 retired S56)

# ── Per-biome-category terrace palettes ──────────────────────────────────
# Each entry: list of (block, probability) tuples applied in order.
# First entry is the dominant block (fills scope), subsequent entries
# overwrite probabilistically for dithered variety.
# Probabilities are per-pixel random thresholds — so 0.20 means ~20% of
# pixels in scope get that block, creating a speckled/dithered look.

_DESERT_BIOMES = frozenset({
    "SAND_DUNE_DESERT", "DESERT_STEPPE_TRANSITION",
    "SEMI_ARID_SHRUBLAND",
})

_MOSS_BIOMES = frozenset({
    "TEMPERATE_RAINFOREST", "RAINFOREST_COAST", "LUSH_RAINFOREST_COAST",
})

_BOREAL_BIOMES = frozenset({
    "BOREAL_TAIGA", "SNOWY_BOREAL_TAIGA",
})

_DRY_BIOMES = frozenset({
    "DRY_OAK_SAVANNA", "DRY_PINE_BARRENS", "DRY_WOODLAND_MAQUIS",
    "CONTINENTAL_STEPPE", "SCRUBBY_HEATHLAND",
})

_ARCTIC_BIOMES = frozenset({
    "ARCTIC_TUNDRA", "FROZEN_FLATS",
})

_ALPINE_BIOMES = frozenset()  # ALPINE_MEADOW retired S56

# Palette: (dominant_block, [(scatter_block, fraction), ...])
# scatter_block overwrites dominant where noise < fraction (cumulative).
_CATEGORY_PALETTES: dict[str, tuple[str, list[tuple[str, float]]]] = {
    "desert":  ("sand",        [("red_sand", 0.10), ("sandstone", 0.08), ("coarse_dirt", 0.06)]),
    "moss":    ("moss_block",  [("podzol", 0.18), ("coarse_dirt", 0.10), ("rooted_dirt", 0.06)]),
    "boreal":  ("podzol",      [("coarse_dirt", 0.20), ("dirt", 0.10), ("rooted_dirt", 0.05)]),
    "dry":     ("coarse_dirt", [("gravel", 0.15), ("dirt", 0.12), ("packed_mud", 0.05)]),
    "arctic":  ("snow_block",  [("gravel", 0.12), ("stone", 0.08), ("coarse_dirt", 0.06)]),
    "alpine":  ("grass_block", [("coarse_dirt", 0.20), ("gravel", 0.10), ("stone", 0.05)]),
    "default": ("grass_block", [("coarse_dirt", 0.15), ("podzol", 0.08), ("rooted_dirt", 0.05)]),
}

# South-facing modifier: extra coarse_dirt/gravel on south slopes.
# Applied as an additional pass on top of the category palette.
_SOUTH_EXTRA_COARSE = 0.20
_SOUTH_EXTRA_GRAVEL = 0.08


def _biome_category(biome: str) -> str:
    """Map a biome name to its terrace palette category."""
    if biome in _DESERT_BIOMES:
        return "desert"
    if biome in _MOSS_BIOMES:
        return "moss"
    if biome in _BOREAL_BIOMES:
        return "boreal"
    if biome in _DRY_BIOMES:
        return "dry"
    if biome in _ARCTIC_BIOMES:
        return "arctic"
    if biome in _ALPINE_BIOMES:
        return "alpine"
    return "default"


class GrassTerrace:
    """Partition layer: biome-aware block mix on moderate slopes."""

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

        # Protect gap mask zones — snow/rock/sand_dune/beach/floodplain
        # have dedicated handlers; terrace must not overwrite them.
        gap_mask = ctx.eco_grads.get("gap_mask")
        gap_ok = np.ones(shape, dtype=bool)
        if gap_mask is not None:
            for g in _PROTECTED_GAPS:
                gap_ok &= (gap_mask != g)

        # Protect riparian pixels — river bank handler in surface_decorator
        # already placed mud/clay/rooted_dirt on these; terrace must not
        # overwrite the carefully dithered river fringe palette.
        _RIPARIAN_BLOCKS = frozenset({"mud", "clay", "rooted_dirt"})
        riparian_ok = np.ones(shape, dtype=bool)
        if ctx.prior_surface is not None:
            for blk in _RIPARIAN_BLOCKS:
                riparian_ok &= (ctx.prior_surface != blk)

        scope = biome_mask & moderate & land & unclaimed & gap_ok & riparian_ok

        if not scope.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # ── Seeded RNG for per-pixel dither ──────────────────────────────
        # Two independent noise fields so scatter patterns don't correlate.
        rng = np.random.default_rng(
            ctx.tile_x * 48271 ^ ctx.tile_z * 31337 ^ 0x7E88ACE)
        noise_a = rng.random(shape).astype(np.float32)
        noise_b = rng.random(shape).astype(np.float32)

        north_factor = ctx.eco_grads.get("north_factor")

        # ── Apply per-biome-category palettes ────────────────────────────
        for biome in np.unique(ctx.biome_grid):
            bname = str(biome)
            if bname not in LAND_BIOMES:
                continue

            bio_scope = scope & (ctx.biome_grid == biome)
            if not bio_scope.any():
                continue

            cat = _biome_category(bname)
            dominant, scatters = _CATEGORY_PALETTES[cat]

            # Dominant block fills the scope.
            block_out[bio_scope] = dominant

            # Scatter blocks — each overwrites probabilistically.
            # Use cumulative thresholds on noise_a so patterns interleave.
            cumulative = 0.0
            for scatter_blk, frac in scatters:
                lo = cumulative
                hi = cumulative + frac
                scatter_px = bio_scope & (noise_a >= lo) & (noise_a < hi)
                if scatter_px.any():
                    block_out[scatter_px] = scatter_blk
                cumulative = hi

            # South-facing extra weathering: more coarse_dirt + gravel.
            if north_factor is not None:
                south = bio_scope & (north_factor <= NORTH_FACTOR_GRASS)
                if south.any():
                    # Use noise_b (independent) for south scatter.
                    south_coarse = south & (noise_b < _SOUTH_EXTRA_COARSE)
                    if cat != "desert":  # desert south gets sandstone, not coarse_dirt
                        block_out[south_coarse] = "coarse_dirt"
                    else:
                        block_out[south_coarse] = "sandstone"
                    south_gravel = south & (noise_b >= _SOUTH_EXTRA_COARSE) & (
                        noise_b < _SOUTH_EXTRA_COARSE + _SOUTH_EXTRA_GRAVEL)
                    if cat not in ("desert", "arctic"):
                        block_out[south_gravel] = "gravel"
                    elif cat == "desert":
                        block_out[south_gravel] = "red_sand"

        modified = scope.copy()
        return make_result(modified, block_out, self.kind, layer_id=0,
                           debug_meta={"terrace_px": int(modified.sum())})
