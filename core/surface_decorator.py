"""
surface_decorator.py — Step 7: Surface Decoration
Vandir World Generation Pipeline — /core/surface_decorator.py

Responsibilities:
  1. Apply per-biome block palette to surface + subsurface (block mixing)
  2. River bank features: mud/clay strip, gravel bars on bends, reeds/tall grass
  3. Ground cover placement: per-biome palettes modulated by decoration density noise

Inputs:
  - chunk_array:     (H, W, Y) amulet chunk block array (modified in-place)
  - surface_y:       (H, W) int16 array — surface Y per pixel
  - biome_grid:      (H, W) str array — resolved biome name per pixel
  - erosion_tile:    (H, W) float32 [0,1] — erosion mask tile
  - moisture_tile:   (H, W) float32 [0,1] — moisture (flow) mask tile
  - height_tile:     (H, W) float32 [0,1] — normalized height mask tile
  - river_meta:      (H, W) uint8 — from river_carver:
                       0 = no water, 1 = stream bank, 2 = river bank, 3 = lake bank
  - flow_tile:       (H, W) float32 [0,1] — raw flow values (for bend detection)
  - noise_fields:    dict of OpenSimplex generators keyed by field name
  - cfg:             dict from thresholds.json
  - tile_x, tile_y:  int — tile coordinates (for seeded RNG)

All operations use NumPy array ops. No nested per-block Python loops.
No GUI imports. No full raster loads.
"""

from __future__ import annotations

import numpy as np
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # opensimplex imported at runtime to avoid hard dep in stubs


# ---------------------------------------------------------------------------
# BLOCK PALETTES (approved Session 5+)
# Tuples: (surface_block, subsurface_block, condition_tag)
# No bare `dirt` as surface block. No powder_snow anywhere.
# Priority order: altitude > erosion2 > erosion > moisture2 > moisture > noise3 > noise2 > noise > base
# ---------------------------------------------------------------------------

BIOME_BLOCK_PALETTES: dict[str, list[tuple[str, str, str]]] = {
    # ── Coastal / Heath ──────────────────────────────────────────────────
    "COASTAL_HEATH": [
        ("grass_block",       "dirt",         "base"),
        ("coarse_dirt",       "gravel",       "eco_ridge"),       # wind-exposed headlands
        ("podzol",            "coarse_dirt",  "eco_moist"),       # north-facing sheltered slopes
        ("gravel",            "stone",        "eco_shallow_soil"),# thin soil on rock
        ("coarse_dirt",       "gravel",       "erosion"),
        ("gravel",            "stone",        "erosion2"),
        ("cobblestone",       "andesite",     "noise"),
        ("stone",             "andesite",     "noise2"),
        ("podzol",            "coarse_dirt",  "moisture"),
        ("sand",              "sandstone",    "noise3"),
    ],
    # ── Lush Forests ─────────────────────────────────────────────────────
    "TEMPERATE_RAINFOREST": [
        ("moss_block",        "rooted_dirt",  "base"),
        ("podzol",            "rooted_dirt",  "eco_deep_soil"),   # valley floor leaf litter
        ("mud",               "mud",          "eco_basin"),       # waterlogged basins
        ("coarse_dirt",       "coarse_dirt",  "eco_ridge"),       # exposed ridgelines
        ("podzol",            "rooted_dirt",  "moisture"),
        ("mud",               "mud",          "moisture2"),
        ("coarse_dirt",       "coarse_dirt",  "erosion"),
        ("mud",               "stone",        "erosion2"),
        ("grass_block",       "rooted_dirt",  "noise"),
        ("mossy_cobblestone", "stone",        "noise2"),
        ("rooted_dirt",       "dirt",         "noise3"),
    ],
    "BOREAL_TAIGA": [
        ("podzol",            "coarse_dirt",  "base"),
        ("grass_block",       "dirt",         "eco_moist"),       # north-facing slopes
        ("coarse_dirt",       "gravel",       "eco_ridge"),       # wind-exposed ridges
        ("podzol",            "rooted_dirt",  "eco_deep_soil"),   # sheltered basins
        ("grass_block",       "dirt",         "moisture"),
        ("coarse_dirt",       "coarse_dirt",  "noise"),
        ("gravel",            "stone",        "erosion"),
        ("diorite",           "stone",        "erosion2"),
        ("moss_block",        "rooted_dirt",  "noise2"),
        ("snow_block",        "dirt",         "altitude"),
    ],
    "SNOWY_BOREAL_TAIGA": [
        ("podzol",            "podzol",       "base"),
        ("podzol",            "coarse_dirt",  "noise"),
        ("coarse_dirt",       "gravel",       "noise2"),
    ],
    # ── Arctic (ALPINE_MEADOW retired S56) ──────────────────────────────
    # S71-3 swap: AT keeps the FF-clone palette (snow_block + coarse_dirt/dirt
    # for plantable scrub + gravel/stone) so non-mountain AT cells still have
    # vegetation.  At HIGH altitude, snowgap (gap==7) overrides → snow_carpet
    # fills.  AT thus reads as scrubby at lowland plateaus + snow caps at peaks.
    "ARCTIC_TUNDRA": [
        ("snow_block",        "stone",        "base"),
        ("coarse_dirt",       "dirt",         "noise"),
        ("gravel",            "stone",        "noise2"),
    ],
    # S71-3 swap: FROZEN_FLATS redesigned as "Tundra Valley" — permafrost meadow
    # with dark-greenish swamp tint MC biome, scattered snow_carpet GC, lots of
    # short_grass + bushes + small pines.  Surface palette mirrors BIRCH_FOREST
    # noise patterns (grass_block/podzol/coarse_dirt mix) for forest-floor feel.
    "FROZEN_FLATS": [
        ("grass_block",       "dirt",         "base"),
        ("podzol",            "dirt",         "noise"),
        ("coarse_dirt",       "dirt",         "noise2"),
    ],
    # ── Temperate Forests ────────────────────────────────────────────────
    "TEMPERATE_DECIDUOUS": [
        ("grass_block",       "dirt",         "base"),
        ("podzol",            "rooted_dirt",  "eco_deep_soil"),   # deep leaf litter in basins
        ("grass_block",       "rooted_dirt",  "eco_moist"),       # north-facing, near water
        ("coarse_dirt",       "gravel",       "eco_ridge"),       # exposed ridgelines
        ("podzol",            "rooted_dirt",  "noise"),
        ("coarse_dirt",       "dirt",         "moisture"),
        ("grass_block",       "rooted_dirt",  "moisture2"),
        ("mossy_cobblestone", "stone",        "erosion"),
        ("gravel",            "stone",        "erosion2"),
        ("rooted_dirt",       "dirt",         "noise2"),
        ("coarse_dirt",       "coarse_dirt",  "noise3"),
    ],
    # S70: reduced mud surfaces per user — 3 mud-surface entries → 1.
    # eco_basin and moisture use podzol/grass instead; only the most extreme
    # moisture2 keeps mud surface for waterlogged feel.
    "RAINFOREST_COAST": [
        ("moss_block",        "rooted_dirt",  "base"),
        ("podzol",            "mud",          "eco_basin"),       # was mud surface
        ("podzol",            "rooted_dirt",  "eco_deep_soil"),   # deep humus
        ("rooted_dirt",       "dirt",         "eco_ridge"),       # wind-exposed
        ("grass_block",       "dirt",         "moisture"),         # was mud surface
        ("mud",               "clay",         "moisture2"),       # extreme wet kept
        ("podzol",            "rooted_dirt",  "erosion"),
        ("gravel",            "stone",        "erosion2"),
        ("grass_block",       "rooted_dirt",  "noise"),
        ("grass_block",       "stone",        "noise2"),
        ("rooted_dirt",       "dirt",         "noise3"),
    ],
    "RIPARIAN_WOODLAND": [
        ("grass_block",       "mud",          "base"),
        ("mud",               "clay",         "eco_moist"),       # saturated zones
        ("clay",              "mud",          "eco_basin"),       # low points
        ("moss_block",        "mud",          "noise"),
        ("clay",              "mud",          "moisture"),
        ("mud",               "clay",         "moisture2"),
        ("gravel",            "stone",        "erosion"),
        ("podzol",            "gravel",       "erosion2"),
        ("podzol",            "rooted_dirt",  "noise2"),
        ("coarse_dirt",       "dirt",         "noise3"),
    ],
    # ── Dry / Arid ───────────────────────────────────────────────────────
    "DRY_OAK_SAVANNA": [
        ("coarse_dirt",       "terracotta",   "base"),
        ("grass_block",       "dirt",         "eco_basin"),       # water collects in hollows
        ("podzol",            "stone",        "eco_ridge"),       # exposed laterite
        ("coarse_dirt",       "stone",        "eco_dry"),         # desiccated patches
        ("grass_block",       "dirt",         "moisture"),
        ("red_sand",          "orange_terracotta", "noise"),
        ("terracotta",        "orange_terracotta", "erosion"),
        ("orange_terracotta", "terracotta",   "erosion2"),
        ("dirt_path",         "coarse_dirt",  "noise2"),
        ("gravel",            "terracotta",   "noise3"),
    ],
    "KARST_BARRENS": [
        ("calcite",           "stone",        "base"),
        ("coarse_dirt",       "stone",        "eco_deep_soil"),   # soil pockets in solution hollows
        ("calcite",           "dripstone_block","eco_shallow_soil"),# exposed limestone
        ("grass_block",       "stone",        "erosion"),
        ("stone",             "calcite",      "erosion2"),
        ("grass_block",       "dripstone_block","noise"),
        ("cobblestone",       "stone",        "noise2"),
        ("coarse_dirt",       "stone",        "moisture"),
        ("dripstone_block",   "stone",        "noise3"),
    ],
    # ── Temperate Forests (cont.) ────────────────────────────────────────
    "BIRCH_FOREST": [
        ("grass_block",       "dirt",         "base"),
        ("podzol",            "rooted_dirt",  "eco_deep_soil"),   # rich leaf mould
        ("coarse_dirt",       "gravel",       "eco_ridge"),       # thin soil ridges
        ("grass_block",       "rooted_dirt",  "eco_moist"),       # north-facing
        ("podzol",            "rooted_dirt",  "noise"),
        ("coarse_dirt",       "dirt",         "moisture"),
        ("moss_block",        "rooted_dirt",  "moisture2"),
        ("diorite",           "stone",        "erosion"),
        ("gravel",            "diorite",      "erosion2"),
        ("rooted_dirt",       "dirt",         "noise2"),
    ],
    "EASTERN_TEMPERATE_COAST": [
        ("grass_block",       "dirt",         "base"),
        ("sand",              "sandstone",    "eco_dry"),         # dry coastal patches
        ("gravel",            "stone",        "eco_shallow_soil"),# rocky headlands
        ("sand",              "sandstone",    "erosion"),
        ("gravel",            "stone",        "erosion2"),
        ("cobblestone",       "andesite",     "noise"),
        ("clay",              "dirt",         "noise2"),
        ("coarse_dirt",       "gravel",       "moisture"),
        ("stone",             "andesite",     "noise3"),
    ],
    "MIXED_FOREST": [
        ("grass_block",       "dirt",         "base"),
        ("podzol",            "rooted_dirt",  "eco_deep_soil"),   # valley bottoms
        ("moss_block",        "rooted_dirt",  "eco_moist"),       # moist north slopes
        ("coarse_dirt",       "gravel",       "eco_ridge"),       # exposed ridges
        ("podzol",            "rooted_dirt",  "noise"),
        ("coarse_dirt",       "dirt",         "erosion"),
        ("podzol",            "stone",        "erosion2"),
        ("moss_block",        "rooted_dirt",  "moisture"),
        ("mossy_cobblestone", "stone",        "noise2"),
        ("rooted_dirt",       "dirt",         "moisture2"),
        ("coarse_dirt",       "coarse_dirt",  "noise3"),
    ],
    # ── Grassland / Steppe ───────────────────────────────────────────────
    "CONTINENTAL_STEPPE": [
        ("grass_block",       "dirt",         "base"),
        ("grass_block",       "rooted_dirt",  "eco_moist"),       # moisture corridors
        ("podzol",            "granite",      "eco_dry"),         # desiccated patches
        ("granite",           "stone",        "eco_shallow_soil"),# thin soil over rock
        ("dirt_path",         "dirt",         "noise"),
        ("podzol",            "granite",      "erosion"),
        ("gravel",            "granite",      "erosion2"),
        ("granite",           "stone",        "noise2"),
        ("podzol",            "dirt",         "moisture"),
        ("grass_block",       "coarse_dirt",  "noise3"),
    ],
    "DRY_PINE_BARRENS": [
        ("coarse_dirt",       "sand",         "base"),
        ("podzol",            "coarse_dirt",  "eco_deep_soil"),   # deeper soil basins
        ("sand",              "sandstone",    "eco_dry"),         # dry exposed sand
        ("granite",           "stone",        "eco_shallow_soil"),# rocky outcrops
        ("podzol",            "coarse_dirt",  "moisture"),
        ("gravel",            "stone",        "erosion"),
        ("granite",           "stone",        "erosion2"),
        ("sand",              "sandstone",    "noise"),
        ("stone",             "granite",      "noise2"),
        ("dirt_path",         "coarse_dirt",  "noise3"),
    ],
    "SCRUBBY_HEATHLAND": [
        ("grass_block",       "dirt",         "base"),
        ("podzol",            "coarse_dirt",  "eco_moist"),       # north-facing hollows
        ("tuff",              "andesite",     "eco_ridge"),       # wind-battered ridges
        ("coarse_dirt",       "stone",        "eco_shallow_soil"),# thin soil
        ("coarse_dirt",       "gravel",       "erosion"),
        ("coarse_dirt",       "stone",        "erosion2"),
        ("tuff",              "andesite",     "noise"),
        ("dirt_path",         "dirt",         "noise2"),
        ("andesite",          "stone",        "noise3"),
        ("sand",              "sandstone",    "noise4"),
        ("podzol",            "coarse_dirt",  "moisture"),
    ],
    # ── Lush Coastal ─────────────────────────────────────────────────────
    "LUSH_RAINFOREST_COAST": [
        ("moss_block",        "rooted_dirt",  "base"),
        ("mud",               "clay",         "eco_basin"),       # waterlogged depressions
        ("podzol",            "rooted_dirt",  "eco_deep_soil"),   # thick humus
        ("coarse_dirt",       "coarse_dirt",  "eco_ridge"),       # windswept ridges
        ("mud",               "clay",         "moisture"),
        ("podzol",            "rooted_dirt",  "moisture2"),
        ("grass_block",       "rooted_dirt",  "noise"),
        ("clay",              "mud",          "noise2"),
        ("mossy_cobblestone", "stone",        "noise3"),
        ("coarse_dirt",       "coarse_dirt",  "erosion"),
        ("gravel",            "stone",        "erosion2"),
    ],
    # ── Desert ───────────────────────────────────────────────────────────
    "SAND_DUNE_DESERT": [
        ("sand",              "sandstone",        "base"),
    ],
    "DESERT_STEPPE_TRANSITION": [
        ("coarse_dirt",       "sandstone",    "base"),
        ("grass_block",       "dirt",         "eco_moist"),       # drainage gully vegetation
        ("sand",              "sandstone",    "eco_dry"),         # deflated sand patches
        ("red_sand",          "sandstone",    "eco_shallow_soil"),# exposed bedrock
        ("sand",              "sandstone",    "noise"),
        ("grass_block",       "dirt",         "moisture"),
        ("terracotta",        "sandstone",    "erosion"),
        ("gravel",            "stone",        "erosion2"),
        ("red_sand",          "sandstone",    "noise2"),
        ("dirt_path",         "coarse_dirt",  "noise3"),
    ],
    "SEMI_ARID_SHRUBLAND": [
        # S76: ALL coarse_dirt/packed_mud swapped to grass_block per user.
        # Sand + gravel kept on dry/erosion channels for desert pockets.
        # Net: 8 grass_block channels out of 10 → grassy meadow look.
        ("grass_block",       "dirt",         "base"),
        ("grass_block",       "dirt",         "eco_moist"),
        ("sand",              "sandstone",    "eco_dry"),         # sand pockets
        ("grass_block",       "dirt",         "eco_shallow_soil"),
        ("grass_block",       "dirt",         "moisture"),
        ("grass_block",       "dirt",         "moisture2"),
        ("sand",              "sandstone",    "erosion"),
        ("gravel",            "stone",        "erosion2"),
        ("grass_block",       "dirt",         "noise"),
        ("grass_block",       "dirt",         "noise2"),
        ("grass_block",       "dirt",         "noise3"),
    ],
    "DRY_WOODLAND_MAQUIS": [
        ("coarse_dirt",       "terracotta",   "base"),
        ("grass_block",       "dirt",         "eco_moist"),       # shaded N-facing slopes
        ("terracotta",        "orange_terracotta", "eco_dry"),    # sun-baked S-facing
        ("dripstone_block",   "stone",        "eco_shallow_soil"),# exposed bedrock
        ("grass_block",       "dirt",         "noise"),
        ("terracotta",        "orange_terracotta", "noise2"),
        ("gravel",            "stone",        "erosion"),
        ("stone",             "terracotta",   "erosion2"),
        ("dripstone_block",   "stone",        "noise3"),
        ("granite",           "stone",        "noise4"),
    ],
    # ── Tropical / Wetland ───────────────────────────────────────────────
    "TIDAL_JUNGLE_FRINGE": [
        ("mud",               "mud",          "base"),
        ("clay",              "mud",          "eco_basin"),       # flooded depressions
        ("podzol",            "rooted_dirt",  "eco_deep_soil"),   # raised hummocks
        ("moss_block",        "mud",          "noise"),
        ("clay",              "mud",          "moisture"),
        ("mud",               "clay",         "moisture2"),
        ("podzol",            "rooted_dirt",  "noise2"),
        ("coarse_dirt",       "mud",          "erosion"),
        ("gravel",            "stone",        "noise3"),
    ],
    "MANGROVE_COAST": [
        ("mud",               "mud",          "base"),
        ("clay",              "mud",          "eco_basin"),       # tidal pools
        ("sand",              "clay",         "eco_dry"),         # sand bars above waterline
        ("clay",              "mud",          "moisture"),
        ("mud",               "clay",         "moisture2"),
        ("moss_block",        "mud",          "noise"),
        ("gravel",            "stone",        "erosion"),
        ("rooted_dirt",       "mud",          "noise2"),
        ("sand",              "clay",         "noise3"),
    ],
    "FRESHWATER_FEN": [
        ("mud",               "mud",          "base"),
        ("clay",              "mud",          "eco_basin"),       # saturated low points
        ("grass_block",       "mud",          "eco_moist"),       # lush growth near water
        ("podzol",            "rooted_dirt",  "eco_deep_soil"),   # peat accumulation
        ("grass_block",       "mud",          "noise"),
        ("clay",              "mud",          "moisture"),
        ("moss_block",        "mud",          "moisture2"),
        ("podzol",            "rooted_dirt",  "noise2"),
        ("coarse_dirt",       "mud",          "erosion"),
        ("gravel",            "clay",         "noise3"),
    ],
}

# ---------------------------------------------------------------------------
# HIGH-MOISTURE BIOMES (use mud at riverbank waterline instead of clay)
# ---------------------------------------------------------------------------
HIGH_MOISTURE_BIOMES: frozenset[str] = frozenset({
    "TEMPERATE_RAINFOREST", "RIPARIAN_WOODLAND", "FRESHWATER_FEN",
    "MANGROVE_COAST", "TIDAL_JUNGLE_FRINGE", "LUSH_RAINFOREST_COAST",
    "RAINFOREST_COAST",
})

# ---------------------------------------------------------------------------
# ROCKY SHORELINE BIOMES (gravel beach instead of sand)
# ---------------------------------------------------------------------------
ROCKY_SHORE_BIOMES: frozenset[str] = frozenset({
    "COASTAL_HEATH", "KARST_BARRENS", "SCRUBBY_HEATHLAND",
    "EASTERN_TEMPERATE_COAST",
})

# ---------------------------------------------------------------------------
# GROUND COVER PALETTES
# Each entry: (block_id, base_density)
# Density is further multiplied by decoration_density noise at runtime.
# ---------------------------------------------------------------------------
GROUND_COVER_PALETTES: dict[str, list[tuple[str, float]]] = {
    # ── Boreal / Cold ────────────────────────────────────────────────────
    # S60: global resin_clump removal per user. Density bumps on all taiga
    # biomes (BOREAL_TAIGA, SNOWY_BOREAL_TAIGA, BOREAL_ALPINE).
    # S71: GC density ~1.4x bumped per user — differentiates from BOREAL_ALPINE
    # which has its own (sparser) palette.  MC biome also changed to
    # minecraft:stony_shore for visual differentiation.
    # S86: bumped short_grass + fern.  No tall_grass added (user override).
    # No new flowers.  Existing lily_of_the_valley / oxeye_daisy kept rare.
    "BOREAL_TAIGA": [
        ("fern", 0.85), ("large_fern", 0.35), ("leaf_litter", 0.49),
        ("moss_carpet", 0.35), ("pale_moss_carpet", 0.14), ("bush", 0.30),
        ("sweet_berry_bush", 0.02), ("short_grass", 0.65),
        ("dead_bush", 0.005),
        ("lily_of_the_valley", 0.005), ("oxeye_daisy", 0.005),
    ],
    # S86: bumped fern + short_grass.  No tall grass.
    "SNOWY_BOREAL_TAIGA": [
        ("fern", 0.22), ("leaf_litter", 0.08), ("bush", 0.12),
        ("short_grass", 0.35), ("short_dry_grass", 0.08),
        ("tall_dry_grass", 0.07), ("dead_bush", 0.01),
    ],
    # S86 Item 3D: BA differentiation from BT/SBT.
    # Surface palette additions handled in the rock/lithology + noise blocks
    # via per-biome adders below; GC palette gets a green_moss accent.
    # Podzol blob scale reduction handled in noise_layers_biome config.
    # Vegetation: bumped existing flowers modestly + short_grass + fern.
    "BOREAL_ALPINE": [
        ("fern", 0.28), ("leaf_litter", 0.16), ("bush", 0.20),
        ("short_grass", 0.30), ("dead_bush", 0.02),
        # S60 dry grass for alpine wind-pruned look
        ("short_dry_grass", 0.10), ("tall_dry_grass", 0.10),
        # S86: green moss accent for warmer look vs SBT
        ("moss_carpet", 0.18),
        # S60 alpine wildflowers — slightly bumped per S86 differentiation
        ("poppy", 0.015), ("allium", 0.015),
        ("cornflower", 0.012), ("oxeye_daisy", 0.012),
    ],
    # "ALPINE_MEADOW" retired S56
    # S70: scrubland — bumped dead_bush + short_dry_grass + bush so ground
    # reads as sparse arctic scrub (paired with tree-density-near-zero in
    # schematic_placement Item I).
    # S71-3 swap: AT keeps the FF-clone GC palette so non-snow-cap AT cells
    # still have scattered scrubland.  Snowgap-affected cells (gap==7) get
    # _SNOW_CAP_SPECIES multiplier (mostly zeros) → snow_carpet fallback fills
    # the snow look.  Result: peaks read snowy, plateaus read scrubby.
    # S71-3 follow-up: dead_bush → tall_dry_grass per user direction.
    # S86: bumped short_grass + short_dry_grass per user (33,13) — too barren.
    # Sparse bush via BUSH_DENSITY_MULT (0.5x). No flowers added.
    "ARCTIC_TUNDRA": [
        ("tall_dry_grass", 0.16),
        ("short_dry_grass", 0.22),
        ("short_grass", 0.18),
        ("bush", 0.05),
    ],
    # S71-3 swap: FROZEN_FLATS = "Tundra Valley" permafrost meadow.  Defining
    # characteristic = scattered snow_carpet (snow[layers=1]) over a moss/grass
    # underlay.  MC biome = swamp (dark-greenish tint); surface = grass_block
    # + podzol + coarse_dirt mix; schematics = smallest pines + bushes very
    # very sparsely.  Snow_carpet is explicit GC (FF removed from snow_carpet
    # config so the 95% fallback doesn't dominate).
    "FROZEN_FLATS": [
        ("snow[layers=1]", 0.30),                # defining "cold" cue (S71-3 was 0.20, bumped per user)
        ("short_grass",     0.45),                # lots
        ("bush",            0.12),                # some bush block
        ("fern",            0.10),                # taiga forest-floor staple
        ("large_fern",      0.04),                # sparse hardy ferns (no moss carpet — user direction)
        ("short_dry_grass", 0.03),                # "dead short" — very sparse
        ("tall_dry_grass",  0.02),                # "dead tall" — very sparse
        ("sweet_berry_bush", 0.005),              # taiga signature, very rare
        ("lily_of_the_valley", 0.005),            # cool-temperate flower
    ],
    # ── Temperate Forests ────────────────────────────────────────────────
    "TEMPERATE_DECIDUOUS": [
        ("leaf_litter", 0.35), ("short_grass", 0.30), ("tall_grass", 0.08),
        ("fern", 0.12), ("bush", 0.18), ("moss_carpet", 0.08),
        ("azalea", 0.04), ("peony", 0.005),  # S71 peony /2 per user
        ("flowering_azalea", 0.003),
        # S60 woodland wildflowers (very rare)
        ("oxeye_daisy", 0.01), ("lily_of_the_valley", 0.008),
        ("azure_bluet", 0.008),
    ],
    "BIRCH_FOREST": [
        ("short_grass", 0.48), ("tall_grass", 0.08), ("leaf_litter", 0.18),
        ("bush", 0.20), ("fern", 0.10),
        ("lily_of_the_valley", 0.02), ("azalea", 0.04),
    ],
    "MIXED_FOREST": [
        ("leaf_litter", 0.40), ("short_grass", 0.50), ("tall_grass", 0.15),
        ("fern", 0.30), ("bush", 0.20), ("large_fern", 0.08),
        ("moss_carpet", 0.12), ("sweet_berry_bush", 0.005),
        ("azalea", 0.03), ("flowering_azalea", 0.005),
        # S60 spring-meadow wildflowers (very rare)
        ("oxeye_daisy", 0.01), ("dandelion", 0.01),
        ("poppy", 0.008), ("lily_of_the_valley", 0.005),
    ],
    "TEMPERATE_RAINFOREST": [
        ("fern", 0.50), ("large_fern", 0.15), ("moss_carpet", 0.30),
        ("leaf_litter", 0.35), ("tall_grass", 0.10), ("bush", 0.15),
        ("short_grass", 0.08), ("short_dry_grass", 0.08),
        ("azalea", 0.03), ("flowering_azalea", 0.005),
    ],
    # ── Coastal / Heath ──────────────────────────────────────────────────
    # S60: up short_grass density, bush rare per user + rare coastal meadow flowers.
    # S70: flowers reduced ×0.2 (very sparse per user). Bush density
    # doubled in schematic_placement (Item K), not here.
    # S86: short_grass bumped per user (36,7). NO tall_grass added.
    # Plant schematic count 2x is handled by BASE_DENSITY bump (0.10 -> 0.25).
    "COASTAL_HEATH": [
        ("short_grass", 0.70), ("short_dry_grass", 0.20), ("tall_grass", 0.02),
        ("bush", 0.04), ("dead_bush", 0.01),
        ("cornflower", 0.002), ("allium", 0.002),
        ("oxeye_daisy", 0.002), ("dandelion", 0.0016),
    ],
    # S60: coastline-vibe (Cape Cod / Outer Banks dune-barrens), heavy flowers.
    # Ammophila beach grass dominant, bayberry/beach plum bush, salt-marsh
    # flower accents.
    # S71: WAY fewer flowers per user walk — they were dominating the visual.
    # Cut all flowers ~6x and roll the absent density into short_grass.  Still
    # has the "wow" coastline feel via short_dry_grass dominance, just not a
    # technicolor mess.
    "EASTERN_TEMPERATE_COAST": [
        ("short_dry_grass", 0.52), ("short_grass", 0.36), ("tall_grass", 0.04),
        ("tall_dry_grass", 0.10), ("bush", 0.12), ("dead_bush", 0.01),
        ("azure_bluet", 0.008), ("dandelion", 0.006),
        ("allium", 0.006), ("cornflower", 0.005), ("oxeye_daisy", 0.005),
    ],
    # S60: ensure dense per user; removed duplicate bush entry.
    "LUSH_RAINFOREST_COAST": [
        ("fern", 0.52), ("large_fern", 0.20), ("tall_grass", 0.12),
        ("moss_carpet", 0.22), ("leaf_litter", 0.20), ("bush", 0.15),
        ("short_grass", 0.10), ("flowering_azalea", 0.02),
        ("short_dry_grass", 0.04),
    ],
    "RAINFOREST_COAST": [
        ("fern", 0.35), ("large_fern", 0.10), ("tall_grass", 0.06),
        ("bush", 0.10), ("short_grass", 0.08), ("moss_carpet", 0.10),
        ("leaf_litter", 0.12),
        # S60 damp-woodland flowers (very rare)
        ("azure_bluet", 0.005), ("lily_of_the_valley", 0.005),
    ],
    # ── Dry / Arid ───────────────────────────────────────────────────────
    # S60: up density of all + very rare prairie wildflowers per user.
    # S70: flowers ×0.5 per user.
    "DRY_OAK_SAVANNA": [
        ("short_dry_grass", 0.52), ("tall_dry_grass", 0.34),
        ("short_grass", 0.22), ("bush", 0.10), ("dead_bush", 0.02),
        # S70 oak-savanna wildflowers (halved from S60)
        ("dandelion", 0.005), ("oxeye_daisy", 0.005), ("poppy", 0.004),
    ],
    # S60: way more short_grass + tall_dry_grass (single-block) + very rare steppe flowers.
    # S71-3: intense grassland vibe per user walk — flowers ÷10, bush block
    # tripled (0.08 → 0.30), trees gated to almost zero in placement.  Goal:
    # vast tall+short grass with frequent bush blocks (Minecraft 1.20.5 bush
    # block, not bush schematic) and only the rarest flower accents.
    "CONTINENTAL_STEPPE": [
        ("short_grass", 0.85), ("tall_grass", 0.04), ("tall_dry_grass", 0.08),
        ("short_dry_grass", 0.10), ("bush", 0.30), ("sunflower", 0.001),
        ("dead_bush", 0.005), ("cornflower", 0.0015),
        ("dandelion", 0.0012), ("poppy", 0.001), ("oxeye_daisy", 0.001),
    ],
    # S60: remove resin_clump, add tall_dry_grass, up all per user.
    "DRY_PINE_BARRENS": [
        ("short_dry_grass", 0.34), ("tall_dry_grass", 0.14),
        ("leaf_litter", 0.18), ("dead_bush", 0.02),
        ("short_grass", 0.18), ("bush", 0.10), ("fern", 0.08),
    ],
    # S60: "wow damn there's flowers everywhere" heathland — heather-style
    # purple + gorse-style yellow + bilberry white.
    # S71: WAY fewer flowers, replaced with short_grass + bush per user.  Still
    # has heathland palette (allium / dandelion accents) but no longer a
    # superbloom — just a moor with the occasional flower.
    "SCRUBBY_HEATHLAND": [
        ("short_grass", 0.78), ("short_dry_grass", 0.22), ("bush", 0.28),
        ("tall_grass", 0.02), ("dead_bush", 0.01),
        ("allium", 0.012), ("dandelion", 0.010),
        ("azure_bluet", 0.008), ("oxeye_daisy", 0.008),
        ("cornflower", 0.006),
    ],
    # S60: bump base palette ~5x to counter the 0.05 eco_density_mod multiplier
    # that was suppressing ground cover to near-zero. Still very rare in-world
    # after the multiplier. S61: dead_bush rare-ified, grass shifted up.
    # S66: more dry grass variety per user — keep sparse overall.
    # S69: bumped dry grass densities ~1.5x for more visible "occasional grass
    # patches" per user — still sparse after 0.05 eco_density_mod cut.
    "SAND_DUNE_DESERT": [
        ("dead_bush", 0.03), ("short_dry_grass", 0.42),
        ("tall_dry_grass", 0.22), ("cactus", 0.02),
    ],
    # S60: add bush infrequent, up short_dry_grass density per user (matches
    # scrubby pattern).
    # S86: bumped short_dry_grass + short_grass per user (18,62) - more
    # visible plant presence between trees.
    "DESERT_STEPPE_TRANSITION": [
        ("short_dry_grass", 0.55), ("tall_dry_grass", 0.13),
        ("dead_bush", 0.02), ("short_grass", 0.18), ("bush", 0.04),
        ("cactus", 0.005),
    ],
    "SEMI_ARID_SHRUBLAND": [
        ("short_dry_grass", 0.17), ("tall_dry_grass", 0.09),
        ("dead_bush", 0.02), ("bush", 0.08), ("short_grass", 0.09),
        ("tall_grass", 0.01),
        # S60 desert-bloom wildflowers (very rare)
        ("dandelion", 0.008), ("poppy", 0.008),
    ],
    # S60: add tall_dry_grass, up short_grass, bush more infrequent per user.
    # Mediterranean-scrub flora — halved existing flowers per user's global rare-ify.
    # S71-2: torchflower removed per user — too saturated for dry biome.
    # S86: bumped short_grass modestly per user (36,75) - paired with
    # tree density REDUCTION (3A) and increased bush via BUSH_DENSITY_MULT.
    "DRY_WOODLAND_MAQUIS": [
        ("short_grass", 0.58), ("short_dry_grass", 0.22), ("tall_dry_grass", 0.13),
        ("bush", 0.06), ("tall_grass", 0.02), ("leaf_litter", 0.06),
        ("dead_bush", 0.01),
        ("allium", 0.015), ("poppy", 0.02),
        ("oxeye_daisy", 0.008),
    ],
    # S60: up all, add short_grass (was 0.02, now 0.12) + tall_dry_grass per user.
    # S66: way more bushes per user — scrubland feel.  bush 0.05 → 0.30.
    # S70: more short_grass, less dry/dead grass per user.
    # S86: bumped short_dry_grass per user (34,9) - more "lived in" look.
    # Bush schematic density 2.5x is in BUSH_DENSITY_MULT.
    "KARST_BARRENS": [
        ("dead_bush", 0.02), ("short_dry_grass", 0.18), ("bush", 0.30),
        ("short_grass", 0.55), ("tall_dry_grass", 0.10),
    ],
    # ── Wetland / Riparian ───────────────────────────────────────────────
    # S60: removed duplicate bush entry. Otherwise unchanged per user.
    "RIPARIAN_WOODLAND": [
        ("short_grass", 0.30), ("tall_grass", 0.01), ("sugar_cane", 0.15),
        ("fern", 0.10), ("bush", 0.08), ("large_fern", 0.02),
        ("moss_carpet", 0.10), ("leaf_litter", 0.08),
        ("blue_orchid", 0.04),
    ],
    # S60: lots of bush, up grass per user.
    "FRESHWATER_FEN": [
        ("short_grass", 0.55), ("tall_grass", 0.03), ("sugar_cane", 0.20),
        ("fern", 0.12), ("moss_carpet", 0.15), ("bush", 0.22),
        ("blue_orchid", 0.05), ("lilac", 0.005),
    ],
    "MANGROVE_COAST": [
        ("tall_grass", 0.10), ("sugar_cane", 0.08), ("short_grass", 0.05),
        ("bush", 0.04), ("short_dry_grass", 0.05), ("moss_carpet", 0.03),
    ],
    "TIDAL_JUNGLE_FRINGE": [
        ("tall_grass", 0.12), ("fern", 0.22), ("large_fern", 0.06),
        ("sugar_cane", 0.10), ("bush", 0.08), ("short_grass", 0.10),
        ("moss_carpet", 0.08), ("leaf_litter", 0.06),
    ],
}

# Double-tall blocks — chunk_writer must place [half=upper] at Y+1 above these
DOUBLE_TALL_BLOCKS: frozenset[str] = frozenset({
    "tall_grass", "large_fern",
    "sunflower", "peony", "rose_bush", "lilac", "pitcher_plant",
})


# ---------------------------------------------------------------------------
# NOISE HELPERS
# ---------------------------------------------------------------------------

def _fbm(gen, x: float, y: float, octaves: int = 3) -> float:
    """Fractional Brownian Motion — returns [0, 1]."""
    val, amp, freq = 0.0, 1.0, 1.0
    for _ in range(octaves):
        val += gen.noise2(x * freq, y * freq) * amp
        amp *= 0.5
        freq *= 2.0
    # raw range roughly [-2, 2] for 3 octaves — normalise to [0, 1]
    return max(0.0, min(1.0, (val + 2.0) / 4.0))


_ECOTONE_MEANDER_GEN_CACHE: dict = {}


def _ecotone_meander_gen(cfg: dict):
    """Lazily-created OpenSimplex generator dedicated to ecotone boundary
    meander (S58). Separate seed so the meander field is uncorrelated with
    the decoration_density generator used for surface-block noise — two
    fBm streams at different scales overlapping on the same tile would
    otherwise lock into visible moiré patterns.
    """
    seeds_cfg = cfg.get("noise_seeds", {}) if isinstance(cfg, dict) else {}
    seed = int(seeds_cfg.get("ecotone_meander", 42007))
    cached = _ECOTONE_MEANDER_GEN_CACHE.get(seed)
    if cached is not None:
        return cached
    try:
        from opensimplex import OpenSimplex
    except ImportError:
        raise ImportError("opensimplex package required for ecotone meander")
    gen = OpenSimplex(seed=seed)
    _ECOTONE_MEANDER_GEN_CACHE[seed] = gen
    return gen


def _noise_tile(gen, tile_h: int, tile_w: int,
                px_offset: int, py_offset: int,
                scale: float, octaves: int = 3) -> np.ndarray:
    """
    Build a (tile_h, tile_w) float32 noise array in [0,1].
    px_offset / py_offset are the world-space pixel coords of the tile origin.
    Uses opensimplex.noise2array for vectorised evaluation.
    """
    try:
        import opensimplex as ox
    except ImportError:
        # Fallback to per-pixel loop if opensimplex unavailable
        out = np.empty((tile_h, tile_w), dtype=np.float32)
        for row in range(tile_h):
            world_y = (py_offset + row) / scale
            for col in range(tile_w):
                world_x = (px_offset + col) / scale
                out[row, col] = _fbm(gen, world_x, world_y, octaves)
        return out

    # Multi-octave fBm via noise2array — ~100x faster than per-pixel loop
    xs_base = (np.arange(tile_w, dtype=np.float64) + px_offset) / scale
    ys_base = (np.arange(tile_h, dtype=np.float64) + py_offset) / scale

    # Extract seed from the OpenSimplex instance to reseed ox.seed()
    # gen._seed is the internal seed attribute
    base_seed = getattr(gen, '_seed', 42002)

    accumulated = np.zeros((tile_h, tile_w), dtype=np.float64)
    amplitude = 1.0
    freq = 1.0
    max_amp = 0.0
    persistence = 0.5
    lacunarity = 2.0

    for octave in range(octaves):
        ox.seed(base_seed + octave * 7919)
        raw = ox.noise2array(xs_base * freq, ys_base * freq)
        accumulated += raw * amplitude
        max_amp += amplitude
        amplitude *= persistence
        freq *= lacunarity

    # Normalize to [0, 1]
    out = accumulated.astype(np.float32)
    lo, hi = out.min(), out.max()
    if hi - lo > 1e-9:
        return (out - lo) / (hi - lo)
    return np.full((tile_h, tile_w), 0.5, dtype=np.float32)


# ---------------------------------------------------------------------------
# NOISE LAYER SYSTEM — reads noise_layers_biome from thresholds.json
# ---------------------------------------------------------------------------

def _gen_layer_noise(noise_type: str, scale: float, seed: int,
                     H: int, W: int, px_off: int, py_off: int) -> np.ndarray:
    """Generate a [0,1] noise field for a palette layer in world-space.

    Matches the visual character of the palette editor's noise types but
    uses world-space coordinates for tile-seamless generation.

    Args:
        noise_type: "simplex_fbm" (canonical), "simplex", "voronoi", "mix", or
                    "white"/"per_pixel". "gaussian" is a back-compat alias for
                    "simplex_fbm" — historical misnomer kept so old configs load.
        scale:      Spatial scale parameter (matches editor scale slider)
        seed:       Deterministic seed
        H, W:       Tile dimensions (typically 512×512)
        px_off, py_off: World-space pixel origin of this tile

    Returns:
        (H, W) float32 array in [0, 1]
    """
    if noise_type == "white" or noise_type == "per_pixel":
        # True per-pixel uniform random — salt-and-pepper pattern.  Every
        # pixel independently rolls, no spatial coherence.  Deterministic
        # via seed.  World-space determinism via (px_off, py_off) hash.
        # Use this for overlays where you want interleaved 1-block variation,
        # NOT coherent blobs.
        _seed_hash = (seed * 2654435761 ^ (px_off * 73856093) ^ (py_off * 19349663)) & 0xFFFFFFFF
        rng = np.random.default_rng(_seed_hash)
        return rng.random((H, W)).astype(np.float32)

    if noise_type in ("simplex_fbm", "simplex", "gaussian"):
        # Multi-octave fBm (fractional Brownian motion) for natural fractal
        # edges.  3 octaves with 0.5 persistence gives detail at the base
        # scale plus two finer layers that break up blob edges.
        # "simplex_fbm" is the canonical name; "simplex" and "gaussian" are
        # back-compat aliases kept so old configs and GUI code still load.
        # For actual per-pixel (salt-and-pepper) noise use "white"/"per_pixel".
        try:
            import opensimplex as ox
        except ImportError:
            rng = np.random.default_rng(seed)
            return rng.random((H, W)).astype(np.float32)

        octaves     = 3
        persistence = 0.5
        lacunarity  = 2.0

        base_freq = 1.0 / max(scale, 1.0)
        xs_base = (np.arange(W, dtype=np.float64) + px_off)
        ys_base = (np.arange(H, dtype=np.float64) + py_off)

        accumulated = np.zeros((H, W), dtype=np.float64)
        amplitude   = 1.0
        freq        = base_freq
        max_amp     = 0.0

        for octave in range(octaves):
            ox.seed(seed + octave * 7919)  # different seed per octave
            xs = xs_base * freq
            ys = ys_base * freq
            accumulated += ox.noise2array(xs, ys) * amplitude
            max_amp   += amplitude
            amplitude *= persistence
            freq      *= lacunarity

        # Normalize to [0, 1]
        raw = accumulated.astype(np.float32)
        lo, hi = raw.min(), raw.max()
        if hi - lo > 1e-9:
            return (raw - lo) / (hi - lo)
        return np.full((H, W), 0.5, dtype=np.float32)

    elif noise_type == "voronoi":
        # Deterministic cellular noise in world-space.
        # Place seed points on a jittered grid so tiles are seamless.
        cell_size = max(scale * 2, 4.0)
        # Determine which grid cells could affect this tile (with margin)
        margin = int(cell_size * 2)
        x0 = int((px_off - margin) / cell_size) - 1
        x1 = int((px_off + W + margin) / cell_size) + 2
        z0 = int((py_off - margin) / cell_size) - 1
        z1 = int((py_off + H + margin) / cell_size) + 2

        # Generate deterministic jittered points per cell
        pts = []
        for gx in range(x0, x1):
            for gz in range(z0, z1):
                cell_seed = (gx * 73856093 ^ gz * 19349663 ^ seed) & 0x7FFFFFFF
                rng_c = np.random.default_rng(cell_seed)
                jx = gx * cell_size + rng_c.random() * cell_size
                jz = gz * cell_size + rng_c.random() * cell_size
                pts.append((jx, jz))

        if len(pts) < 2:
            return np.zeros((H, W), dtype=np.float32)

        from scipy.spatial import cKDTree
        pts_arr = np.array(pts)
        tree = cKDTree(pts_arr)

        # Query each pixel
        row_idx = np.arange(H) + py_off
        col_idx = np.arange(W) + px_off
        cc, rr = np.meshgrid(col_idx.astype(np.float64),
                             row_idx.astype(np.float64))
        coords = np.column_stack([cc.ravel(), rr.ravel()])
        dists, _ = tree.query(coords)
        dists = dists.reshape(H, W).astype(np.float32)

        # Normalize to [0, 1]
        lo, hi = dists.min(), dists.max()
        if hi - lo > 1e-9:
            return (dists - lo) / (hi - lo)
        return np.zeros((H, W), dtype=np.float32)

    elif noise_type == "mix":
        # 60% simplex + 40% voronoi (matching editor)
        f1 = _gen_layer_noise("simplex", scale, seed, H, W, px_off, py_off)
        f2 = _gen_layer_noise("voronoi", scale, seed + 10000, H, W, px_off, py_off)
        mixed = 0.6 * f1 + 0.4 * f2
        lo, hi = mixed.min(), mixed.max()
        if hi - lo > 1e-9:
            return ((mixed - lo) / (hi - lo)).astype(np.float32)
        return mixed.astype(np.float32)

    else:
        # Unknown type — white noise fallback
        rng = np.random.default_rng(seed)
        return rng.random((H, W)).astype(np.float32)


def _shadow_blocks_for_biome(
    biome_name:        str,
    boundary_mask:     np.ndarray,   # (H, W) bool — pixels to compute for
    noise_layers:      dict,         # noise_layers_biome from thresholds.json
    H: int, W: int,
    px_off: int, py_off: int,
    noise_cache:       dict | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """S85: compute (surface, subsurface) blocks AS IF every boundary pixel
    were `biome_name`. Other pixels are filled with empty-string sentinel.

    Used by `_apply_ecotone_dither` (Option A) to compute "what would biome X
    have placed at this exact pixel" without random sampling. Preserves biome
    X's natural simplex blob structure when used as the seam-swap target.

    Returns (surface, subsurface) — both (H, W) object arrays, only
    `boundary_mask` pixels are populated; others are "" (empty).

    Falls back to empty arrays if biome has no noise_layers config.
    """
    surface = np.full((H, W), "", dtype=object)
    subsurface = np.full((H, W), "", dtype=object)
    if not boundary_mask.any():
        return surface, subsurface

    layers = noise_layers.get(biome_name)
    if not layers:
        return surface, subsurface  # caller will fall back to random sample

    # Find base layer
    base_blk = "grass_block"
    base_sub = "dirt"
    for layer in layers:
        if layer.get("is_base") and layer.get("enabled", True):
            base_blk = layer["block"]
            base_sub = layer.get("sub", "dirt")
            break

    surface[boundary_mask] = base_blk
    subsurface[boundary_mask] = base_sub

    if noise_cache is None:
        noise_cache = {}

    # Apply non-base layers (highest index = lowest priority, lowest index = top)
    for i in range(len(layers) - 1, -1, -1):
        layer = layers[i]
        if not layer.get("enabled", True) or layer.get("is_base"):
            continue
        noise_type = layer.get("noise", "simplex_fbm")
        scale      = layer.get("scale", 60)
        seed       = layer.get("seed", 42 + i)
        coverage   = layer.get("coverage", 0.5)
        block      = layer["block"]
        sub        = layer.get("sub", "dirt")
        threshold  = 1.0 - coverage
        cache_key = (noise_type, scale, seed)
        if cache_key in noise_cache:
            field = noise_cache[cache_key]
        else:
            field = _gen_layer_noise(noise_type, scale, seed,
                                     H, W, px_off, py_off)
            noise_cache[cache_key] = field
        apply = boundary_mask & (field >= threshold)
        if apply.any():
            surface[apply]    = block
            subsurface[apply] = sub
    return surface, subsurface


def _apply_noise_layers(
    surface_blocks:    np.ndarray,   # (H, W) object — modified in-place
    subsurface_blocks: np.ndarray,   # (H, W) object — modified in-place
    biome_grid:        np.ndarray,   # (H, W) object (str)
    noise_layers:      dict,         # noise_layers_biome from thresholds.json
    H: int, W: int,
    px_off: int, py_off: int,
) -> None:
    """Apply per-biome noise layer stacks from thresholds.json.

    Each biome's layer list is ordered with index 0 = highest priority.
    Base layer (is_base=True) fills the biome, then layers are applied
    from lowest priority (highest index) to highest (index 0), each
    overwriting pixels where its noise >= (1 - coverage).

    This replaces the old BIOME_BLOCK_PALETTES + condition tag system
    for biomes that have noise_layers_biome entries.
    """
    # Cache generated noise fields to avoid regenerating for multiple biomes
    # with same noise params
    noise_cache: dict[tuple, np.ndarray] = {}

    for biome in np.unique(biome_grid):
        biome_str = str(biome)
        layers = noise_layers.get(biome_str)
        if layers is None:
            continue  # no noise layers for this biome — handled by fallback

        bmask = biome_grid == biome
        if not bmask.any():
            continue

        # Find base layer
        base_blk = "grass_block"
        base_sub = "dirt"
        for layer in layers:
            if layer.get("is_base") and layer.get("enabled", True):
                base_blk = layer["block"]
                base_sub = layer.get("sub", "dirt")
                break

        # Apply base
        surface_blocks[bmask]    = base_blk
        subsurface_blocks[bmask] = base_sub

        # Apply non-base layers: iterate from highest index (lowest priority)
        # to lowest index (highest priority), each overwriting previous
        for i in range(len(layers) - 1, -1, -1):
            layer = layers[i]
            if not layer.get("enabled", True) or layer.get("is_base"):
                continue

            noise_type = layer.get("noise", "simplex_fbm")
            scale      = layer.get("scale", 60)
            seed       = layer.get("seed", 42 + i)
            coverage   = layer.get("coverage", 0.5)
            block      = layer["block"]
            sub        = layer.get("sub", "dirt")

            threshold = 1.0 - coverage

            # Generate or retrieve cached noise field
            cache_key = (noise_type, scale, seed)
            if cache_key in noise_cache:
                field = noise_cache[cache_key]
            else:
                field = _gen_layer_noise(noise_type, scale, seed,
                                         H, W, px_off, py_off)
                noise_cache[cache_key] = field

            # Apply where noise >= threshold within this biome
            apply = bmask & (field >= threshold)
            if apply.any():
                surface_blocks[apply]    = block
                subsurface_blocks[apply] = sub


# ---------------------------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------------------------

def _apply_sbt_mountaincap_remap(
    biome_grid: np.ndarray,
    surface_y:  np.ndarray,
    tile_x: int,
    tile_y: int,
    cfg: dict,
) -> None:
    """S68: mountaincap remap for SNOWY_BOREAL_TAIGA.  Pixels where surface_y
    is below MIN_Y OR in the [MIN_Y, MAX_Y] dither band with simplex noise
    failing the threshold get remapped to BOREAL_ALPINE (in biome_grid).
    Above MAX_Y always stays SBT.  Single unified decision drives both snow
    carpet placement (gated on biome_grid) and MC biome tag (also gated on
    biome_grid).  Mutates biome_grid in place."""
    sbt_cfg = cfg.get("sbt_mountaincap", {})
    if not sbt_cfg.get("enabled", True):
        return
    MIN_Y = int(sbt_cfg.get("min_y", 200))
    MAX_Y = int(sbt_cfg.get("max_y", 250))
    NOISE_SCALE = float(sbt_cfg.get("noise_scale", 40.0))
    THRESHOLD = float(sbt_cfg.get("threshold", 0.0))
    TARGET = str(sbt_cfg.get("target", "BOREAL_ALPINE"))

    sbt_mask = (biome_grid == "SNOWY_BOREAL_TAIGA")
    if not sbt_mask.any():
        return

    # Below MIN_Y: always remap (no snow at low altitude)
    below = sbt_mask & (surface_y < MIN_Y)
    biome_grid[below] = TARGET

    # In dither band: per-pixel simplex decides
    band = sbt_mask & (surface_y >= MIN_Y) & (surface_y < MAX_Y)
    if band.any():
        try:
            from opensimplex import OpenSimplex
        except ImportError:
            return
        sim = OpenSimplex(seed=(tile_x * 17 ^ tile_y * 31 ^ 0xCAFE5B7))
        tile_wx = tile_x * biome_grid.shape[1]
        tile_wz = tile_y * biome_grid.shape[0]
        # Vectorised coarse sample + bilinear upsample for speed
        from scipy.ndimage import zoom as _zoom
        H, W = biome_grid.shape
        step = 4
        gy = np.arange(0, H, step)
        gx = np.arange(0, W, step)
        coarse = np.empty((len(gy), len(gx)), dtype=np.float32)
        for i, y in enumerate(gy):
            for j, x in enumerate(gx):
                coarse[i, j] = sim.noise2((tile_wx + x) / NOISE_SCALE,
                                          (tile_wz + y) / NOISE_SCALE)
        noise_full = _zoom(coarse, (H / coarse.shape[0], W / coarse.shape[1]), order=1)[:H, :W]
        # Below threshold → remap (no snow); above → keep SBT
        remap_in_band = band & (noise_full < THRESHOLD)
        biome_grid[remap_in_band] = TARGET


def _apply_snow_carpet(
    surface_blocks: np.ndarray,
    ground_cover:   np.ndarray,
    biome_grid:     np.ndarray,
    cfg:            dict,
    tile_x:         int,
    tile_y:         int,
) -> None:
    """S64: place `snow[layers=1]` ground cover on snowy-biome pixels.
    Dithered at biome boundary via a distance-transform ramp — snow prob
    decays from 1.0 at interior to 0 at `ramp_blocks` outside the biome.
    Per-pixel coin decides placement within the ramp.  Does not overwrite
    existing ground_cover.  Skips water/ice/air surface blocks.
    """
    snow_cfg = cfg.get("snow_carpet", {})
    if not snow_cfg.get("enabled", False):
        return
    biomes = set(snow_cfg.get("biomes", []))
    if not biomes:
        return
    snowy = np.zeros(biome_grid.shape, dtype=bool)
    for b in biomes:
        snowy |= (biome_grid == b)
    if not snowy.any():
        return
    # S67: strict snowy-biome gate — no outward bleed into adjacent biomes.
    # Previously used a distance ramp that placed snow on BT/BA pixels within
    # 30 blocks of SBT boundary; user wants clean biome-boundary separation.
    rng = np.random.default_rng((tile_x * 7919) ^ (tile_y * 31337) ^ 0x5700FA11)
    coin = rng.random(biome_grid.shape).astype(np.float32)
    # Within snowy biomes: 95% placement with slight per-pixel dither for
    # visual texture (not outward bleed).
    place_snow = snowy & (coin < 0.95) & (ground_cover == "")
    _NOT_SNOW_ON = frozenset({"water", "lava", "air",
                              "ice", "packed_ice", "blue_ice", "snow_block"})
    for blk in _NOT_SNOW_ON:
        place_snow &= (surface_blocks != blk)
    if place_snow.any():
        ground_cover[place_snow] = "snow[layers=1]"


def _flatten_dune_regions(
    surface_y: np.ndarray,
    gap_mask: np.ndarray,
    sigma_baseline: float = 30.0,
    flatten_strength: float = 0.7,
    local_smooth_sigma: float = 8.0,
    mask_dilation_blocks: int = 4,
) -> None:
    """S69: Root-cause fix for sand-dune boundary seams.  Gaea's raw height.tif
    contains sharp dune geometry in gap_mask==8 regions.  Pre-S69 we hid these
    with a universal boundary smoother cranked to sigma=16 × 6 passes — but
    that smeared 36 blocks into every mountain interior adjacent to any biome
    boundary.  Instead: pull Y in dune regions toward a wide-sigma neighbourhood
    baseline, then gaussian-smooth the dune interior only.  Mountain ridges
    outside gap_mask==8 are untouched."""
    dune = (gap_mask == 8)
    if not dune.any():
        return
    from scipy.ndimage import binary_dilation, gaussian_filter
    sy_f = surface_y.astype(np.float32)
    # 1. Baseline = wide-sigma gaussian of surface_y (what the terrain would
    #    look like without dune bumps riding on top).
    baseline = gaussian_filter(sy_f, sigma=sigma_baseline, mode='nearest')
    # 2. Blend dune pixels toward baseline.  strength=0.7 = 70% baseline + 30%
    #    original.  Preserves some dune character without leaving sharp bumps.
    s = float(np.clip(flatten_strength, 0.0, 1.0))
    sy_f[dune] = sy_f[dune] * (1.0 - s) + baseline[dune] * s
    # 3. Local smoothing pass inside a slightly dilated dune mask so the
    #    flattened region blends smoothly with adjacent untouched terrain.
    local_ring = binary_dilation(dune, iterations=max(1, int(mask_dilation_blocks)))
    if local_ring.any():
        local_blur = gaussian_filter(sy_f, sigma=local_smooth_sigma, mode='nearest')
        weight = local_ring.astype(np.float32)
        sy_f = weight * local_blur + (1.0 - weight) * sy_f
    surface_y[:] = np.round(sy_f).astype(surface_y.dtype)


def _smooth_all_biome_boundaries_y(
    surface_y: np.ndarray,
    biome_grid: np.ndarray,
    buffer_blocks: int = 24,
    sigma: float = 8.0,
    passes: int = 3,
) -> None:
    """S67: Gaussian-smooth Y at EVERY biome boundary pixel.  Replaces per-
    biome target list — now biome-agnostic.  Detects boundaries via 4-neighbour
    biome difference, builds a wide ring on both sides, blends Y toward
    blurred Y with a taper weight that peaks at the boundary."""
    if biome_grid.size == 0:
        return
    # Detect boundary: pixels whose 4-neighbour has a different biome.
    boundary = np.zeros(biome_grid.shape, dtype=bool)
    boundary[:-1, :] |= (biome_grid[:-1, :] != biome_grid[1:, :])
    boundary[1:, :]  |= (biome_grid[:-1, :] != biome_grid[1:, :])
    boundary[:, :-1] |= (biome_grid[:, :-1] != biome_grid[:, 1:])
    boundary[:, 1:]  |= (biome_grid[:, :-1] != biome_grid[:, 1:])
    if not boundary.any():
        return
    from scipy.ndimage import binary_dilation, gaussian_filter, distance_transform_edt
    ring = binary_dilation(boundary, iterations=buffer_blocks)
    if not ring.any():
        return
    # Weight peaks at boundary pixels, fades to edge of ring
    dist_from_boundary = distance_transform_edt(~boundary).astype(np.float32)
    max_dist = max(float(buffer_blocks), 1.0)
    weight = np.clip(1.0 - dist_from_boundary / max_dist, 0.0, 1.0)
    weight[~ring] = 0.0
    sy_f = surface_y.astype(np.float32)
    for _ in range(max(1, passes)):
        blurred = gaussian_filter(sy_f, sigma=sigma, mode='nearest')
        sy_f = weight * blurred + (1.0 - weight) * sy_f
    surface_y[:] = np.round(sy_f).astype(surface_y.dtype)


def _smooth_ocean_coastline_y(
    surface_y: np.ndarray,
    buffer_blocks: int = 18,
    sigma: float = 6.0,
    passes: int = 2,
) -> None:
    """S66: Gaussian-smooth the Y-step at the ocean coastline (surface_y=63
    threshold).  Softens beach cliffs and eliminates isolated Y=64 land
    pixels surrounded by ocean.  Mutates in place."""
    SEA_LEVEL = 63
    below_sea = surface_y < SEA_LEVEL
    if not below_sea.any() or below_sea.all():
        return  # all-land or all-ocean tile
    from scipy.ndimage import binary_dilation, gaussian_filter, distance_transform_edt
    inside  = binary_dilation(~below_sea, iterations=buffer_blocks) & below_sea
    outside = binary_dilation(below_sea,  iterations=buffer_blocks) & ~below_sea
    ring = inside | outside
    if not ring.any():
        return
    dist_from_edge = distance_transform_edt(ring).astype(np.float32)
    max_dist = dist_from_edge[ring].max() if ring.any() else 1.0
    weight = np.clip(dist_from_edge / max(max_dist, 1.0), 0.0, 1.0)
    weight[~ring] = 0.0
    sy_f = surface_y.astype(np.float32)
    for _ in range(max(1, passes)):
        blurred = gaussian_filter(sy_f, sigma=sigma, mode='nearest')
        sy_f = weight * blurred + (1.0 - weight) * sy_f
    surface_y[:] = np.round(sy_f).astype(surface_y.dtype)


def _smooth_biome_boundary_y(
    surface_y: np.ndarray,
    biome_grid: np.ndarray,
    target_biome: str,
    buffer_blocks: int = 24,
    sigma: float = 8.0,
    passes: int = 3,
    taper: float = 1.0,
) -> None:
    """
    S65: worldedit-smooth-brush-style Gaussian erosion on a biome boundary.
    Smooths the Y-step across a wide ring centered on the biome boundary —
    BOTH sides (inside target + outside target within `buffer_blocks`).

    Algorithm:
      1. Compute `boundary_ring` = pixels within `buffer_blocks` of the
         SAND_DUNE_DESERT↔neighbour boundary, on either side.
      2. Blur surface_y with Gaussian(sigma=sigma) to get smoothed Y field.
      3. At ring pixels, blend surface_y toward blurred Y with a weight
         that PEAKS at the boundary and fades to zero at the ring edge.
      4. Iterate `passes` times — each pass applied on the updated Y, so
         the smoothing compounds into a natural arc.

    The taper weight mimics worldedit's soft brush: 1.0 at the boundary,
    0.0 at `buffer_blocks` distance.  Interior dune (far from boundary)
    and interior non-dune (far from boundary) untouched.
    """
    tgt_mask = (biome_grid == target_biome)
    if not tgt_mask.any():
        return
    from scipy.ndimage import binary_dilation, gaussian_filter, distance_transform_edt

    # Ring: within `buffer_blocks` of boundary on EITHER side
    inside_buf  = binary_dilation(~tgt_mask, iterations=buffer_blocks) & tgt_mask
    outside_buf = binary_dilation(tgt_mask,  iterations=buffer_blocks) & ~tgt_mask
    ring = inside_buf | outside_buf
    if not ring.any():
        return

    # Weight field: 1.0 at boundary, 0.0 at buffer edge
    # Boundary pixels have dist_to_other_side == 1 (neighbour is across).
    # Build a distance-from-boundary field valid only within ring.
    # Easier: distance from ~ring to ring — gives dist from nearest non-ring.
    dist_from_edge = distance_transform_edt(ring).astype(np.float32)
    max_dist = dist_from_edge[ring].max() if ring.any() else 1.0
    # Taper weight: peaks at center of ring (where boundary is) vs. outer edge.
    # Normalize dist_from_edge to [0,1] within ring.
    weight = np.clip(dist_from_edge / max(max_dist, 1.0), 0.0, 1.0) * taper
    # Zero outside ring
    weight[~ring] = 0.0

    sy_f = surface_y.astype(np.float32)
    for _pass in range(max(1, passes)):
        blurred = gaussian_filter(sy_f, sigma=sigma, mode='nearest')
        sy_f = weight * blurred + (1.0 - weight) * sy_f

    surface_y[:] = np.round(sy_f).astype(surface_y.dtype)


def decorate_surface(
    surface_y:    np.ndarray,   # (H, W) int16
    biome_grid:   np.ndarray,   # (H, W) object (str)
    erosion_tile: np.ndarray,   # (H, W) float32
    moisture_tile:np.ndarray,   # (H, W) float32
    height_tile:  np.ndarray,   # (H, W) float32
    river_meta:   np.ndarray,   # (H, W) uint8
    flow_tile:    np.ndarray,   # (H, W) float32
    noise_fields: dict,
    cfg:          dict,
    tile_x:       int,
    tile_y:       int,
    eco_grads=None,             # Optional EcoGradients from eco_gradients.py
    cliff_deg:    np.ndarray | None = None,  # (H,W) float32 degrees
    use_new_geology: bool = False,  # When True, skip legacy rock painting (geology column handles subsurface)
    use_new_surface_pipeline: bool = False,  # Phase 2.0: run new layer-based surface pipeline for temperate cliffs
    lithology_tile: np.ndarray | None = None,  # (H/8, W/8) uint8 lithology group IDs — upscaled internally
    clearing_field: np.ndarray | None = None,  # (H, W) float32 [0,1] — meadow clearing noise (S57 Phase 3a)
    biome_grid_padded: np.ndarray | None = None,  # (H+2*pad, W+2*pad) str — neighbour-tile biome halo (S58 Phase 3b)
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Compute surface and subsurface block arrays for a tile.

    When *eco_grads* is provided, ecological gradient conditions (eco_moist,
    eco_dry, eco_ridge, eco_basin, eco_shallow_soil, eco_deep_soil) are used
    alongside the legacy noise/erosion/moisture conditions.  When absent, falls
    back to pure noise-threshold behaviour.

    Returns:
        surface_blocks:    (H, W) str array  — block at surface_y
        subsurface_blocks: (H, W) str array  — block at surface_y - 1 and surface_y - 2
        ground_cover:      (H, W) str array  — block at surface_y + 1 ('' = air/none)
    """
    H, W = surface_y.shape
    bm_cfg   = cfg["block_mixing"]
    px_off   = tile_x * W
    py_off   = tile_y * H

    # S69: Flatten Gaea's raw dune bumps BEFORE the universal boundary
    # smoother.  Root-cause fix for the sand/biome seams that drove S68 to
    # crank the boundary smoother to sigma=16 × 6 passes (which then smeared
    # mountain ridges).  Touches gap_mask==8 pixels only; rest of world is
    # left for the boundary smoother at S67-gentle intensity.
    _df_cfg = cfg.get("dune_flatten", {})
    if _df_cfg.get("enabled", True) and eco_grads is not None \
            and hasattr(eco_grads, 'gap_mask'):
        _flatten_dune_regions(
            surface_y, eco_grads.gap_mask,
            sigma_baseline=float(_df_cfg.get("sigma_baseline", 30.0)),
            flatten_strength=float(_df_cfg.get("flatten_strength", 0.7)),
            local_smooth_sigma=float(_df_cfg.get("local_smooth_sigma", 8.0)),
            mask_dilation_blocks=int(_df_cfg.get("mask_dilation_blocks", 4)),
        )

    # S65/S66/S67: Y-smoothing at ALL biome boundaries — user wants the seam
    # fix to apply regardless of biome.  Previous per-biome target list was
    # missing pairs (e.g. SNOWY_BOREAL_TAIGA ↔ BOREAL_TAIGA).  Now: detect
    # EVERY biome boundary pixel via neighbour difference, apply Gaussian
    # smoothing in a wide ring centered on it.
    if cfg.get("sand_dune_smoothing", {}).get("enabled", True):
        _sds_cfg = cfg.get("sand_dune_smoothing", {})
        _smooth_all_biome_boundaries_y(
            surface_y, biome_grid,
            buffer_blocks=int(_sds_cfg.get("buffer_blocks", 24)),
            sigma=float(_sds_cfg.get("sigma", 8.0)),
            passes=int(_sds_cfg.get("passes", 3)),
        )
        # Ocean-coastline smoothing — treat underwater-threshold as the
        # "other side" of the boundary.  Smooths beach cliff cutoffs.
        if _sds_cfg.get("smooth_ocean_coastline", True):
            _smooth_ocean_coastline_y(
                surface_y,
                buffer_blocks=int(_sds_cfg.get("coast_buffer_blocks", 18)),
                sigma=float(_sds_cfg.get("coast_sigma", 6.0)),
                passes=int(_sds_cfg.get("coast_passes", 2)),
            )

    # --- Build noise arrays ------------------------------------------------
    den_cfg  = cfg["decoration_density_noise"]
    den_gen  = noise_fields["decoration_density"]
    noise_b  = _noise_tile(den_gen, H, W, px_off, py_off,
                           scale=den_cfg["scale"], octaves=den_cfg["octaves"])

    # Secondary noise layers — only needed for legacy palette path and slope zones.
    # When noise_layers_biome is active, skip generating noise_b2/b3/b4 for block
    # mixing (saves ~3 noise tile generations) but still generate for slope zones.
    has_noise_layers = bool(cfg.get("noise_layers_biome"))
    if not has_noise_layers:
        noise_b2 = _noise_tile(den_gen, H, W, px_off + 31337, py_off + 71193,
                               scale=bm_cfg["noise_scale"], octaves=2)
        noise_b3 = _noise_tile(den_gen, H, W, px_off + 99991, py_off + 13337,
                               scale=bm_cfg["noise_scale"], octaves=2)
        noise_b4 = _noise_tile(den_gen, H, W, px_off + 777771, py_off + 444443,
                               scale=bm_cfg["noise_scale"], octaves=2)
    else:
        # Lightweight: only generate noise_b2/b3 for slope zones (reuse noise_b for b4)
        noise_b2 = _noise_tile(den_gen, H, W, px_off + 31337, py_off + 71193,
                               scale=bm_cfg["noise_scale"], octaves=2)
        noise_b3 = _noise_tile(den_gen, H, W, px_off + 99991, py_off + 13337,
                               scale=bm_cfg["noise_scale"], octaves=2)
        noise_b4 = noise_b  # reuse primary noise for slope talus scatter

    # --- Precompute condition masks ----------------------------------------
    e_thr    = bm_cfg["erosion_threshold"]
    e2_thr   = bm_cfg.get("erosion2_threshold",   0.75)
    m_thr    = bm_cfg["moisture_threshold"]
    m2_thr   = bm_cfg.get("moisture2_threshold",  0.80)
    n_thr    = bm_cfg["noise_threshold"]
    n2_thr   = bm_cfg.get("noise2_threshold",     0.75)
    n3_thr   = bm_cfg.get("noise3_threshold",     0.82)
    n4_thr   = bm_cfg.get("noise4_threshold",     0.88)
    alt_thr  = bm_cfg.get("altitude_threshold",   0.72)

    cond: dict[str, np.ndarray] = {
        "erosion":   erosion_tile  >= e_thr,
        "erosion2":  erosion_tile  >= e2_thr,
        "moisture":  moisture_tile >= m_thr,
        "moisture2": moisture_tile >= m2_thr,
        "noise":     noise_b       >= n_thr,
        "noise2":    noise_b2      >= n2_thr,
        "noise3":    noise_b3      >= n3_thr,
        "noise4":    noise_b4      >= n4_thr,
        "altitude":  height_tile   >= alt_thr,
    }

    # --- Ecological gradient conditions ------------------------------------
    # When eco_grads is provided, build stochastic boolean masks from
    # continuous [0,1] gradient fields.  Noise modulates the sigmoid
    # probability so edges stay organic, not ruler-straight.
    if eco_grads is not None:
        from core.eco_gradients import eco_sigmoid

        eco_cfg = cfg.get("eco_vegetation", {})
        noise_amp = float(eco_cfg.get("noise_amplitude", 0.2))

        # Noise modulator: low-amplitude perturbation [1-amp, 1+amp]
        noise_mod = 1.0 + noise_amp * (noise_b - 0.5) * 2.0   # [1-amp, 1+amp]

        # Seeded random field for stochastic thresholding
        rng_eco = np.random.default_rng(
            tile_x * 73856093 ^ tile_y * 19349663 ^ 9999991)
        rand_eco = rng_eco.random((H, W)).astype(np.float32)

        # Build continuous probability fields, then threshold stochastically
        eco_probs = {
            "eco_moist":        eco_sigmoid(eco_grads.moisture_index, 0.50, 0.10) * noise_mod,
            "eco_dry":          eco_sigmoid(1.0 - eco_grads.moisture_index, 0.50, 0.10) * noise_mod,
            "eco_ridge":        eco_sigmoid(eco_grads.wind_exposure, 0.40, 0.08) * noise_mod,
            "eco_basin":        eco_sigmoid(eco_grads.concavity_norm, 0.60, 0.08) * noise_mod,
            "eco_shallow_soil": eco_sigmoid(1.0 - eco_grads.soil_depth, 0.50, 0.10) * noise_mod,
            "eco_deep_soil":    eco_sigmoid(eco_grads.soil_depth, 0.60, 0.10) * noise_mod,
        }
        for tag, prob in eco_probs.items():
            cond[tag] = np.clip(prob, 0.0, 1.0) > rand_eco

    # Per-biome sparse overrides on noise thresholds
    sparse_overrides: dict = bm_cfg.get("sparse_overrides", {})

    # --- Output arrays -------------------------------------------------------
    surface_blocks    = np.full((H, W), "stone",        dtype=object)
    subsurface_blocks = np.full((H, W), "stone",        dtype=object)
    ground_cover      = np.full((H, W), "",             dtype=object)

    # --- Block mixing ---------------------------------------------------------
    # Two paths: noise_layers_biome (from palette editor) or legacy palettes.
    noise_layers = cfg.get("noise_layers_biome")
    used_noise_layers = False

    if noise_layers:
        # ── PRIMARY: noise_layers_biome from thresholds.json ──────────────
        # This is the palette editor output — each biome has a layer stack
        # with noise type, coverage, scale, seed.  Layers applied bottom-
        # to-top, each overwriting where noise >= (1 - coverage).
        _apply_noise_layers(
            surface_blocks, subsurface_blocks,
            biome_grid, noise_layers,
            H, W, px_off, py_off,
        )
        used_noise_layers = True

        # S53 eco overlay DISABLED S57 — palette flattening.
        # This loop re-applied eco_* condition entries (eco_ridge, eco_moist,
        # eco_basin, etc.) from BIOME_BLOCK_PALETTES on top of noise layers.
        # eco_ridge traced contour lines → visible banding.  noise_layers_biome
        # already provides clean noise-only surfaces for all 26 biomes.
        # BIOME_BLOCK_PALETTES retained as reference for the legacy fallback path.

    if not used_noise_layers:
        # ── FALLBACK: legacy BIOME_BLOCK_PALETTES + condition tags ────────
        unique_biomes = np.unique(biome_grid)

        PRIORITY = [
            "base",
            "eco_dry", "eco_moist",
            "eco_shallow_soil", "eco_deep_soil",
            "eco_ridge", "eco_basin",
            "noise4", "noise3", "noise2", "noise",
            "moisture2", "moisture", "erosion2", "erosion", "altitude",
        ]

        for biome in unique_biomes:
            biome_str = str(biome)
            palette   = BIOME_BLOCK_PALETTES.get(biome_str)
            if palette is None:
                continue

            mask = (biome_grid == biome)

            bio_cond = dict(cond)
            if biome_str in sparse_overrides:
                ov = sparse_overrides[biome_str]
                if "noise2_threshold" in ov:
                    bio_cond["noise2"] = noise_b2 >= ov["noise2_threshold"]
                if "noise3_threshold" in ov:
                    bio_cond["noise3"] = noise_b3 >= ov["noise3_threshold"]
                if "noise4_threshold" in ov:
                    bio_cond["noise4"] = noise_b4 >= ov["noise4_threshold"]

            by_tag: dict[str, tuple[str, str]] = {}
            for surf, sub, tag in palette:
                by_tag[tag] = (surf, sub)

            for tag in PRIORITY:
                if tag not in by_tag:
                    continue
                surf, sub = by_tag[tag]
                if tag == "base":
                    apply = mask
                else:
                    apply = mask & bio_cond.get(tag, np.zeros((H, W), dtype=bool))
                surface_blocks[apply]    = surf
                subsurface_blocks[apply] = sub

    # S52: legacy _apply_slope_zones() REMOVED.  This was a second slope
    # system (45°/65° thresholds) that wrote stone/gravel/cobblestone on
    # gap==0 steep pixels — redundant with the surface pipeline layers:
    #   TemperateCliffFace (35°+), TemperateTalusApron (18-35°),
    #   GrassTerrace (8-18°).  Keeping the function definition below for
    # reference but no longer calling it.

    # --- Grass cutoff at elevation (S57) ----------------------------------------
    # Biome-agnostic: grass_block fades out above Y 325, fully gone by Y 350.
    # Inverse of the snow fade-in (Y 250→275) in eco_gradients.  Prevents
    # grass surfaces at alpine/mountaintop elevations where only stone/dirt
    # should be visible.  Replacement block = subsurface of the biome's base
    # palette entry (usually dirt/coarse_dirt).
    # S85: scaled for 768-height world (was 325/350 for 448-height era).
    GRASS_Y_FLOOR = 460.0
    GRASS_Y_CEIL  = 500.0
    _grass_eligible = (surface_blocks == "grass_block") & (surface_y >= GRASS_Y_FLOOR)
    if _grass_eligible.any():
        _sy_f = surface_y.astype(np.float32)
        _grass_fade = np.clip(
            (_sy_f - GRASS_Y_FLOOR) / (GRASS_Y_CEIL - GRASS_Y_FLOOR), 0.0, 1.0
        )
        _grass_rng = np.random.default_rng(
            tile_x * 73856093 ^ tile_y * 19349663 ^ 0xC0FF)
        _grass_coin = _grass_rng.random((H, W)).astype(np.float32)
        _grass_kill = _grass_eligible & (_grass_coin < _grass_fade)
        if _grass_kill.any():
            surface_blocks[_grass_kill] = "coarse_dirt"
        del _sy_f, _grass_fade, _grass_rng, _grass_coin, _grass_kill
    del _grass_eligible

    # --- Meadow clearing surface block override (S57 Phase 3a, rewritten) -----
    # Gradient salt-and-pepper pattern (modeled on _apply_river_banks + S55
    # beach plateau).  Replaces forest-floor blocks with a balanced 5-block
    # mix inside clearings; uses per-pixel decision coin across a probability
    # ramp so the clearing/forest edge reads as salt-and-pepper, not a hard
    # line.  Runs BEFORE gap==1/==4 overrides so hydrology meadows still win;
    # clearings stomp forest surface but yield to floodplain/meadow.
    _CLEARING_BIOMES = frozenset({
        "TEMPERATE_RAINFOREST", "TEMPERATE_DECIDUOUS", "BOREAL_TAIGA",
        "MIXED_FOREST", "BIRCH_FOREST", "RIPARIAN_WOODLAND",
    })
    if clearing_field is not None:
        from core.meadow_clearing_field import (
            CLEARING_INTERIOR_THRESHOLD as _CF_THR,
            CLEARING_EDGE_BAND as _CF_HALF,
        )
        _clearing_biome = np.zeros((H, W), dtype=bool)
        for _cb in _CLEARING_BIOMES:
            _clearing_biome |= (biome_grid == _cb)
        if _clearing_biome.any():
            # Step A: Gradient clearing probability.  Ramps from 1.0 (deep
            # clearing, field <= THR - HALF) to 0.0 (deep forest, field >= THR + HALF).
            # S65: apply per-pixel field jitter BEFORE the threshold so the
            # boundary has more single-block noise where two clearings (or a
            # clearing and a forest) meet.  Amplitude = 30% of the edge band.
            _cf_jitter_rng = np.random.default_rng(
                tile_x * 11971 ^ tile_y * 59359 ^ 0xCBD17)
            _cf_jitter = (_cf_jitter_rng.random((H, W)).astype(np.float32) - 0.5) * _CF_HALF * 0.6
            _cf_prob = np.clip(
                ((_CF_THR + _CF_HALF) - (clearing_field + _cf_jitter)) / (2.0 * _CF_HALF),
                0.0, 1.0,
            ).astype(np.float32)

            # Step B: Per-pixel decision coin (uniform RNG, NO gaussian filter).
            # Same seed 0xC1EA5F used in _apply_ground_cover clearing section
            # so surface block and ground cover decisions align pixel-by-pixel.
            _cp_rng = np.random.default_rng(
                tile_x * 48271 ^ tile_y * 31337 ^ 0xC1EA5F)
            _cp_decision = _cp_rng.random((H, W)).astype(np.float32)
            _is_clearing_px = _clearing_biome & (_cp_decision < _cf_prob)

            # Paint over all forest-floor blocks INCLUDING grass_block.
            # Inside a clearing, all forest surface blocks become grass_block
            # (matches floodplain gap==1/==4 treatment).  The simplex blob
            # SHAPE of the clearing provides the organic outline; the
            # gradient probability at the edge provides per-pixel salt-and-
            # pepper softening at the forest/clearing boundary.
            _FOREST_FLOOR = ("grass_block", "podzol", "dirt", "coarse_dirt", "moss_block", "rooted_dirt")
            _ff_mask = np.zeros((H, W), dtype=bool)
            for _b in _FOREST_FLOOR:
                _ff_mask |= (surface_blocks == _b)
            _paint_px = _is_clearing_px & _ff_mask
            if _paint_px.any():
                # Solid grass_block conversion — matches floodplain pattern.
                surface_blocks[_paint_px] = "grass_block"

            del _cp_rng, _cp_decision, _cf_prob, _is_clearing_px, _ff_mask, _paint_px
        del _clearing_biome

    # --- Floodplain (gap==4) edge softening (S57 Phase 3a) --------------------
    # gap==4 is a hard hydrology mask — without softening the floodplain
    # reads as a sharp grass-block-bordered corridor against forest.  Apply
    # the same gradient salt-and-pepper pattern using EDT distance from the
    # gap==4 boundary as the probability ramp.
    if eco_grads is not None and hasattr(eco_grads, 'gap_mask'):
        _gap_fp = eco_grads.gap_mask == 4
        if _gap_fp.any():
            from scipy.ndimage import distance_transform_edt as _edt_fp
            # Distance from gap==4 pixels, measured OUTSIDE the gap.
            # fp_dist = 0 at gap boundary, increasing outward into forest.
            _fp_dist = _edt_fp(~_gap_fp).astype(np.float32)
            # Softening zone: 8 blocks outside the gap==4 boundary.
            FP_SOFT_WIDTH = 8.0
            # Probability ramp: 1.0 at boundary → 0.0 at FP_SOFT_WIDTH.
            # Only apply softening in the outside-gap zone (decision was already
            # made for inside-gap pixels by the existing gap==4 override below).
            _fp_prob = np.clip(
                1.0 - (_fp_dist / FP_SOFT_WIDTH), 0.0, 1.0,
            ).astype(np.float32)
            # Only soften where we're NEAR the floodplain AND outside it.
            _fp_soft_zone = (~_gap_fp) & (_fp_dist > 0) & (_fp_dist <= FP_SOFT_WIDTH)
            if _fp_soft_zone.any():
                _fp_rng = np.random.default_rng(
                    tile_x * 48271 ^ tile_y * 31337 ^ 0xF10D50)
                _fp_decision = _fp_rng.random((H, W)).astype(np.float32)
                _fp_paint_decision = _fp_soft_zone & (_fp_decision < _fp_prob)

                # Only paint forest-floor blocks INCLUDING grass_block.
                _FP_FLOOR = ("grass_block", "podzol", "dirt", "coarse_dirt", "moss_block", "rooted_dirt")
                _fp_ff = np.zeros((H, W), dtype=bool)
                for _b in _FP_FLOOR:
                    _fp_ff |= (surface_blocks == _b)
                _fp_paint = _fp_paint_decision & _fp_ff
                if _fp_paint.any():
                    # Solid grass_block conversion — matches floodplain interior.
                    surface_blocks[_fp_paint] = "grass_block"
                del _fp_rng, _fp_decision, _fp_paint_decision, _fp_ff, _fp_paint
            del _fp_dist, _fp_prob, _fp_soft_zone
        del _gap_fp

    # --- Gap surface block ratio shift ----------------------------------------
    # Inside clearings, nudge surface block ratios WITHOUT changing noise type.
    # Meadow: some podzol/coarse_dirt → grass_block (more grass in clearings)
    # Bare: some grass_block → gravel or stone (exposed substrate)
    # Windthrow: leave as-is (fallen trees decompose on existing soil)
    if eco_grads is not None and hasattr(eco_grads, 'gap_mask'):
        _gap = eco_grads.gap_mask
        if (_gap > 0).any():
            _gap_rng = np.random.default_rng(
                tile_x * 48271 ^ tile_y * 31337 ^ 0x6A9)
            _gap_rand = _gap_rng.random((H, W)).astype(np.float32)

            # Meadow: no fallen forest groundcover — almost all grass_block.
            # Podzol is forest-floor decomposition, doesn't belong in open meadow.
            # Coarse_dirt only as very sparse patches (compacted game trails, etc.)
            meadow_px = _gap == 1
            meadow_podzol = meadow_px & (surface_blocks == "podzol")
            surface_blocks[meadow_podzol] = "grass_block"
            meadow_dirt = meadow_px & (surface_blocks == "dirt")
            surface_blocks[meadow_dirt] = "grass_block"
            meadow_coarse = meadow_px & (surface_blocks == "coarse_dirt")
            # Keep only ~10% of coarse_dirt, rest → grass_block
            surface_blocks[meadow_coarse & (_gap_rand >= 0.10)] = "grass_block"

            # Windthrow: almost all grass_block with very sparse coarse_dirt.
            wt_px = _gap == 2
            if wt_px.any():
                surface_blocks[wt_px] = "grass_block"
                surface_blocks[wt_px & (_gap_rand < 0.03)] = "coarse_dirt"

            # Floodplain: treat like meadow for surface blocks — grass_block
            # dominant, no forest-floor materials.  No mud outside the
            # existing riparian bank zone (that's handled separately).
            flood_px = _gap == 4
            if flood_px.any():
                flood_podzol = flood_px & (surface_blocks == "podzol")
                surface_blocks[flood_podzol] = "grass_block"
                flood_dirt = flood_px & (surface_blocks == "dirt")
                surface_blocks[flood_dirt] = "grass_block"
                flood_coarse = flood_px & (surface_blocks == "coarse_dirt")
                surface_blocks[flood_coarse & (_gap_rand >= 0.08)] = "grass_block"

            # Rock + Alpine meadow: use raw gradient for probabilistic
            # surface block mixing.  This breaks up the contour-line
            # staircase at the rock/meadow boundary by dithering stone
            # and grass pixels based on the continuous gradient value.
            #
            # EXCEPTION: This boundary is intentionally softer than
            # standard biome transitions.  The adjoining biome's grass
            # encroaches further up into the rock zone than other ecotone
            # boundaries, mimicking real alpine treeline ecology where
            # grass/krummholz fingers extend well above the general
            # ── gap==6 (alpine_meadow) RETIRED S56 — Gaea slope mask replaces ──

            # ── Rock (gap==5): surface block selection via lithology palette ──
            # S71-3: PAINTED lithology mask is source of truth.  Per-pixel
            # group ID from `lithology_tile` selects the palette; only unpainted
            # cells (id=0) fall back to the biome-based `zone_to_group` path.
            # Previously this was per-biome, which ignored the user's painted
            # lithology overlay (e.g. arid_basaltic continents rendered as
            # whatever biome's hardcoded mapping said).
            rock_px = _gap == 5
            if rock_px.any():
                _rng = np.random.default_rng(tile_x * 48271 ^ tile_y * 31337 ^ 0xB10C)
                _scatter = _rng.random((H, W)).astype(np.float32)

                _litho_cfg = cfg.get("lithology", {}) if isinstance(cfg, dict) else {}
                _zone_to_group = _litho_cfg.get("zone_to_group", {})
                _groups = _litho_cfg.get("groups", {})
                _DEFAULT_PAL = ["stone", "andesite", "granite", "diorite"]

                # Build group-id → palette LUT from config.
                _gid_to_pal: dict[int, list] = {}
                for _gname, _gdata in _groups.items():
                    _gid = int(_gdata.get("id", 0))
                    _pal_g = _gdata.get("palette") or _DEFAULT_PAL
                    _gid_to_pal[_gid] = _pal_g

                # Upscale lithology to tile resolution if needed.
                _litho_at_res = None
                if lithology_tile is not None:
                    if lithology_tile.shape != (H, W):
                        from scipy.ndimage import zoom as _sd_zoom
                        _zh = H / lithology_tile.shape[0]
                        _zw = W / lithology_tile.shape[1]
                        _litho_at_res = _sd_zoom(lithology_tile, (_zh, _zw), order=0)
                    else:
                        _litho_at_res = lithology_tile

                def _palette_for_biome(biome_name: str) -> list:
                    g = _zone_to_group.get(biome_name)
                    if g and g in _groups:
                        p = _groups[g].get("palette")
                        if p:
                            return p
                    return _DEFAULT_PAL

                if _litho_at_res is not None:
                    # PAINTED-FIRST path: iterate over unique lithology IDs
                    _unique_gids = np.unique(_litho_at_res[rock_px])
                    for _gid in _unique_gids:
                        _gid = int(_gid)
                        _bm_lith = rock_px & (_litho_at_res == _gid)
                        if not _bm_lith.any():
                            continue
                        if _gid == 0:
                            # Unpainted cells — fall back to per-biome
                            for _bname in np.unique(biome_grid[_bm_lith]):
                                _bm_fb = _bm_lith & (biome_grid == _bname)
                                if not _bm_fb.any():
                                    continue
                                _pal = _palette_for_biome(str(_bname))
                                _n = len(_pal)
                                for _i, _blk in enumerate(_pal):
                                    _lo = _i / _n; _hi = (_i + 1) / _n
                                    _band = _bm_fb & (_scatter >= _lo) & (_scatter < _hi)
                                    if _band.any():
                                        surface_blocks[_band] = _blk
                                subsurface_blocks[_bm_fb] = _pal[0]
                        else:
                            _pal = _gid_to_pal.get(_gid, _DEFAULT_PAL)
                            _n = len(_pal)
                            for _i, _blk in enumerate(_pal):
                                _lo = _i / _n; _hi = (_i + 1) / _n
                                _band = _bm_lith & (_scatter >= _lo) & (_scatter < _hi)
                                if _band.any():
                                    surface_blocks[_band] = _blk
                            subsurface_blocks[_bm_lith] = _pal[0]
                else:
                    # No lithology mask available — pure biome fallback (legacy)
                    for _bname in np.unique(biome_grid[rock_px]):
                        _bm = rock_px & (biome_grid == _bname)
                        if not _bm.any():
                            continue
                        _pal = _palette_for_biome(str(_bname))
                        _n = len(_pal)
                        for _i, _blk in enumerate(_pal):
                            _lo = _i / _n; _hi = (_i + 1) / _n
                            _band = _bm & (_scatter >= _lo) & (_scatter < _hi)
                            if _band.any():
                                surface_blocks[_band] = _blk
                        subsurface_blocks[_bm] = _pal[0]

                # Flow-driven wash channels — per-lithology-group wash palette.
                # S85: was hardcoded sand+sandstone universally; now each lithology
                # group has its own `wash_palette` in config.
                # S86 Item 1B: intensified per user feedback.
                #   - Lower flow threshold (0.005 -> configurable, default 0.002):
                #     wider trigger zone, more wash visible.
                #   - Dilate wash zone (default 2 blocks): wider channels.
                #   - Write to subsurface_blocks too: 2-block visible depth
                #     instead of single block on top of underlying rock.
                #   - 5-block linear fade at outer dilation edge so washes
                #     blend into surrounding terrain instead of stopping at
                #     a hard ring.
                # Knobs in config.washes (defaults below if missing).
                if flow_tile is not None:
                    _wcfg = cfg.get("washes", {}) if isinstance(cfg, dict) else {}
                    _wash_min_flow = float(_wcfg.get("min_flow", 0.002))
                    _wash_dilation = int(_wcfg.get("dilation", 2))
                    _wash_fade_blocks = int(_wcfg.get("fade_blocks", 5))
                    _wash_zone_core = rock_px & (flow_tile > _wash_min_flow)
                    if _wash_dilation > 0 and _wash_zone_core.any():
                        from scipy.ndimage import binary_dilation as _bd
                        _wash_zone = _bd(_wash_zone_core, iterations=_wash_dilation) & rock_px
                    else:
                        _wash_zone = _wash_zone_core
                    # Fade: probability ramps from 1 at the core down to 0 over
                    # _wash_fade_blocks at the outer rim of the dilation.
                    _wash_fade_prob = None
                    if _wash_fade_blocks > 0 and _wash_zone.any() and _wash_zone_core.any():
                        from scipy.ndimage import distance_transform_edt as _dt
                        # dist from edge of core: pixels in core have dist 0 inside core.
                        # Actually we want: at core pixels prob=1, at outer edge prob=0.
                        _dist_from_core = _dt(~_wash_zone_core).astype(np.float32)
                        _wash_fade_prob = np.clip(
                            1.0 - _dist_from_core / float(_wash_fade_blocks),
                            0.0, 1.0,
                        )
                    # Apply fade by dropping pixels via coin vs fade probability.
                    # Result: core stays solid, outer rim thins out gradually.
                    if _wash_fade_prob is not None:
                        _fade_rng = np.random.default_rng(
                            tile_x * 17 ^ tile_y * 31 ^ 0xFADE)
                        _fade_coin = _fade_rng.random((H, W)).astype(np.float32)
                        _wash_zone = _wash_zone & (_fade_coin < _wash_fade_prob)
                    if _wash_zone.any():
                        _DEFAULT_WASH_PAL = ["gravel", "coarse_dirt", "sand"]
                        _wash_rng = np.random.default_rng(
                            tile_x * 48271 ^ tile_y * 31337 ^ 0x4A5E)
                        # Build group-id -> wash_palette LUT from config.
                        _gid_to_wash: dict[int, list] = {}
                        for _gname, _gdata in _groups.items():
                            _gid = int(_gdata.get("id", 0))
                            _wp = _gdata.get("wash_palette") or _DEFAULT_WASH_PAL
                            _gid_to_wash[_gid] = _wp

                        def _wash_palette_for_biome(biome_name: str) -> list:
                            g = _zone_to_group.get(biome_name)
                            if g and g in _groups:
                                wp = _groups[g].get("wash_palette")
                                if wp:
                                    return wp
                            return _DEFAULT_WASH_PAL

                        # Per-pixel wash group lookup: lithology mask first,
                        # biome fallback for unpainted pixels (matches the rock
                        # surface palette path above).
                        # S86 Item 1B: ALSO write to subsurface_blocks so washes
                        # appear 2 blocks deep instead of 1 (user feedback: single
                        # block over each layer reveals rock on slopes).
                        if _litho_at_res is not None:
                            for _gid in np.unique(_litho_at_res[_wash_zone]):
                                _gid = int(_gid)
                                _bm_w = _wash_zone & (_litho_at_res == _gid)
                                if not _bm_w.any():
                                    continue
                                if _gid == 0:
                                    # Unpainted - per-biome
                                    for _bname in np.unique(biome_grid[_bm_w]):
                                        _bm_wfb = _bm_w & (biome_grid == _bname)
                                        if not _bm_wfb.any():
                                            continue
                                        _wp = _wash_palette_for_biome(str(_bname))
                                        _n_pix = int(_bm_wfb.sum())
                                        _wp_arr = np.asarray(_wp, dtype=object)
                                        _idx = _wash_rng.integers(0, len(_wp), size=_n_pix)
                                        surface_blocks[_bm_wfb] = _wp_arr[_idx]
                                        # Subsurface: independently sampled (less
                                        # repetition on slopes) but same palette.
                                        _idx_sub = _wash_rng.integers(0, len(_wp), size=_n_pix)
                                        subsurface_blocks[_bm_wfb] = _wp_arr[_idx_sub]
                                else:
                                    _wp = _gid_to_wash.get(_gid, _DEFAULT_WASH_PAL)
                                    _n_pix = int(_bm_w.sum())
                                    _wp_arr = np.asarray(_wp, dtype=object)
                                    _idx = _wash_rng.integers(0, len(_wp), size=_n_pix)
                                    surface_blocks[_bm_w] = _wp_arr[_idx]
                                    _idx_sub = _wash_rng.integers(0, len(_wp), size=_n_pix)
                                    subsurface_blocks[_bm_w] = _wp_arr[_idx_sub]
                        else:
                            # No lithology mask - per-biome only
                            for _bname in np.unique(biome_grid[_wash_zone]):
                                _bm_w = _wash_zone & (biome_grid == _bname)
                                if not _bm_w.any():
                                    continue
                                _wp = _wash_palette_for_biome(str(_bname))
                                _n_pix = int(_bm_w.sum())
                                _wp_arr = np.asarray(_wp, dtype=object)
                                _idx = _wash_rng.integers(0, len(_wp), size=_n_pix)
                                surface_blocks[_bm_w] = _wp_arr[_idx]
                                _idx_sub = _wash_rng.integers(0, len(_wp), size=_n_pix)
                                subsurface_blocks[_bm_w] = _wp_arr[_idx_sub]
                        del _wash_rng
                    del _wash_zone

                del _rng, _scatter

            # ── Snow caps (gap==7): snow_block replacement (S56 simplified) ──
            # Gaea dusting mask drives gap==7. All snow pixels get snow_block.
            # Dither edges are baked into the mask at 50k by rebuild_gaea_gaps.py
            # (blue-noise dither), so no per-pixel probability ramp needed here.
            # S85: SNOWY_BOREAL_TAIGA + FROZEN_FLATS exempted — SBT's native podzol
            # surface supports foliage and snow_carpet (vanilla snow[layers=1])
            # provides the snowy visual on top.  FF is the Tundra Valley design
            # (grass_block + scattered snow_carpet); forcing snow_block here would
            # break the Tundra Valley palette + nuke its rich GC palette.
            snow_px = _gap == 7
            if snow_px.any():
                _snow_exempt = (
                    (biome_grid == "SNOWY_BOREAL_TAIGA") |
                    (biome_grid == "FROZEN_FLATS")
                )
                snow_px = snow_px & ~_snow_exempt
            if snow_px.any():
                surface_blocks[snow_px] = "snow_block"
                # S84: powder_snow generation removed — was placed in concavities
                # but powder_snow is a hazard ("pest block") that traps players.
                # All snow now renders as snow_block.

            # ── Sand dunes (gap==8): pure sand ───────────────────────────
            sand_px = _gap == 8
            if sand_px.any():
                surface_blocks[sand_px] = "sand"
                if not use_new_geology:
                    subsurface_blocks[sand_px] = "sandstone"

            # ── Sand flows on rock (proximity-based dither) ─────────────
            # Where rock/alpine pixels border sand dunes, paint sand patches
            # on the rock surface — wind-deposited sand fingers licking up
            # the rocky margins. Distance-based falloff like the lake fringe.
            if sand_px.any() and rock_px.any():
                from scipy.ndimage import distance_transform_edt as _edt_sand
                # Distance from sand dune (in pixels)
                _dist_sand = _edt_sand(~sand_px).astype(np.float32)
                # Sand-flow zone: rock/alpine pixels within 4 px of dunes (subtle)
                FLOW_DIST = 4.0
                _flow_zone = rock_px & (_dist_sand > 0) & (_dist_sand <= FLOW_DIST)
                if _flow_zone.any():
                    # Probability: 35% at d=1, 0% at d=4 (subtle fingers)
                    _flow_prob = np.clip(0.35 * (1.0 - (_dist_sand - 1.0) / (FLOW_DIST - 1.0)), 0.0, 0.35)
                    _sf_rng = np.random.default_rng(
                        tile_x * 48271 ^ tile_y * 31337 ^ 0x5A4F)
                    _sf_rand = _sf_rng.random((H, W)).astype(np.float32)
                    _sand_flow = _flow_zone & (_sf_rand < _flow_prob)
                    surface_blocks[_sand_flow] = "sand"
                    del _sf_rng, _sf_rand, _flow_prob, _sand_flow
                del _dist_sand, _flow_zone

            # ── Beach (gap==9): sand only — S51 rework ─────────────────────
            beach_px = _gap == 9
            if beach_px.any():
                surface_blocks[beach_px] = "sand"
                if not use_new_geology:
                    subsurface_blocks[beach_px] = "sandstone"

            # ── Beach edge (S55 v7): no overwrite.
            # The gap==9 placement in eco_gradients already creates the
            # gradient (sand probability 1.0 → 0.0 across the dither zone).
            # Pixels that don't get sand simply retain whatever the biome
            # palette painted — moss_block, mud, coarse_dirt, podzol, etc.
            # This preserves biome character and lets ground cover plant
            # vegetation through the transition band normally.  No plain-
            # grass-block buffer between sand and forest.

    # --- Gap edge probabilistic dither ----------------------------------------
    # At meadow/windthrow/floodplain edges, blend the gap's grass_block surface
    # with the surrounding biome's surface block.  Uses distance-from-gap-interior
    # to create a probability gradient: deep inside gap = 100% gap block, at
    # edge = ~50/50, outside = 0%.  This replaces the hard gap boundary with
    # an organic, dithered transition matching the rock/alpine approach.
    #
    # Standard width (not extended like rock/alpine — see that section's comment
    # for why alpine is deliberately softer).
    if eco_grads is not None and hasattr(eco_grads, 'gap_mask'):
        from scipy.ndimage import distance_transform_edt as _edt_gap_edge
        _gap = eco_grads.gap_mask
        _DITHER_WIDTH = 6  # pixels of transition band at gap edges

        # Save the biome's original surface blocks BEFORE gap overrides
        # were applied — we need these as the "forest side" of the blend.
        # Since gap overrides already ran above, the current surface_blocks
        # inside gaps are gap-blocks (grass_block etc).  The biome blocks
        # are still present OUTSIDE the gap.  For each edge pixel, we
        # find the nearest non-gap surface block via the original biome
        # palette (already in surface_blocks outside the gap).

        for gap_val, gap_label in [(1, "meadow"), (2, "windthrow"), (4, "floodplain")]:
            _this_gap = _gap == gap_val
            if not _this_gap.any():
                continue

            # Distance from gap interior (positive inside, 0 at boundary)
            _dist_inside = _edt_gap_edge(_this_gap).astype(np.float32)
            # Distance from gap exterior (positive outside, 0 at boundary)
            _dist_outside = _edt_gap_edge(~_this_gap).astype(np.float32)

            # Transition band: pixels within DITHER_WIDTH of the gap boundary
            # on EITHER side (inside gap approaching edge, or outside gap near edge)
            _inner_edge = _this_gap & (_dist_inside <= _DITHER_WIDTH)
            _outer_edge = ~_this_gap & (_dist_outside <= _DITHER_WIDTH) & (surface_y >= 63)
            # Don't dither into water or other gap types
            _water_mask = (river_meta > 0) if river_meta is not None else np.zeros((H, W), dtype=bool)
            _outer_edge = _outer_edge & ~_water_mask & (_gap == 0)

            _edge_band = _inner_edge | _outer_edge
            if not _edge_band.any():
                continue

            # Probability of keeping gap block (grass): high deep inside, low at outer edge
            # Inner edge: prob = dist_inside / DITHER_WIDTH (1.0 deep inside → 0.0 at boundary)
            # Outer edge: prob = 0 (always keep biome block, but with some gap-block scatter)
            _keep_gap_prob = np.zeros((H, W), dtype=np.float32)
            _keep_gap_prob[_inner_edge] = np.clip(
                _dist_inside[_inner_edge] / _DITHER_WIDTH, 0.0, 1.0)
            # Outer edge: small probability of gap block bleeding out (scattered grass in forest)
            _keep_gap_prob[_outer_edge] = np.clip(
                0.3 * (1.0 - _dist_outside[_outer_edge] / _DITHER_WIDTH), 0.0, 0.3)

            # Use the existing _gap_rand for deterministic dither
            _swap_to_biome = _inner_edge & (_gap_rand >= _keep_gap_prob)
            _swap_to_gap = _outer_edge & (_gap_rand >= (1.0 - _keep_gap_prob))

            # For inner→biome swaps: these pixels are currently grass_block (gap block).
            # We want to replace with whatever the biome palette would have given.
            # The nearest non-gap pixel's surface block is a good proxy.
            # Simple approach: copy from the nearest non-gap neighbor via shift.
            if _swap_to_biome.any():
                # Find nearest biome surface block by shifting in 4 directions
                _biome_source = np.zeros((H, W), dtype=bool)
                _biome_source = (~_this_gap) & (_gap == 0) & ~_water_mask
                if _biome_source.any():
                    # Use the surface block of the nearest biome pixel
                    # For each swap pixel, find nearest biome pixel's block
                    from scipy.ndimage import distance_transform_edt as _edt2
                    _dist_biome, _idx_biome = _edt2(~_biome_source, return_indices=True)
                    _nearest_rows = _idx_biome[0][_swap_to_biome]
                    _nearest_cols = _idx_biome[1][_swap_to_biome]
                    surface_blocks[_swap_to_biome] = surface_blocks[_nearest_rows, _nearest_cols]
                    del _dist_biome, _idx_biome

            # For outer→gap swaps: these pixels currently have biome blocks.
            # Replace with gap block (grass_block) to scatter grass into forest edge.
            if _swap_to_gap.any():
                surface_blocks[_swap_to_gap] = "grass_block"

            del _dist_inside, _dist_outside, _inner_edge, _outer_edge, _edge_band
            del _keep_gap_prob, _swap_to_biome, _swap_to_gap

    # --- River bank features -------------------------------------------------
    _apply_river_banks(
        surface_blocks, ground_cover,
        river_meta, flow_tile, biome_grid, moisture_tile,
        noise_b, cfg,
        eco_grads=eco_grads,
        noise_fields=noise_fields,
        tile_x=tile_x, tile_y=tile_y,
    )

    # --- Biome boundary ecotone dither (AFTER banks so it blends edges) ------
    _gap_for_ecotone = eco_grads.gap_mask if (eco_grads is not None and hasattr(eco_grads, 'gap_mask')) else None

    # S58 Phase 3b — cross-tile seam softening.
    # When the orchestrator provides a padded biome_grid (inner H,W surrounded
    # by a halo of neighbour-tile biomes), run the ecotone dither on a padded
    # surface_blocks/subsurface_blocks array so transitions that fall exactly
    # on tile seams get the same gradient-probability softening as within-tile
    # transitions. Each tile only writes to its own inner pixels; the halo is
    # read-only reference data, so no cross-worker coordination is needed.
    _use_padded_ecotone = (
        biome_grid_padded is not None
        and biome_grid_padded.ndim == 2
        and biome_grid_padded.shape[0] > H
        and biome_grid_padded.shape[1] > W
        and (biome_grid_padded.shape[0] - H) == (biome_grid_padded.shape[1] - W)
        and (biome_grid_padded.shape[0] - H) % 2 == 0
    )

    if _use_padded_ecotone:
        _pad_px = (biome_grid_padded.shape[0] - H) // 2
        Hpad = H + 2 * _pad_px
        Wpad = W + 2 * _pad_px
        _inner = (slice(_pad_px, _pad_px + H), slice(_pad_px, _pad_px + W))

        # Allocate padded surface/subsurface arrays. Seed with "stone" so any
        # halo pixel that no noise-layer entry covers still reads as valid
        # block (matches decorate_surface's own default seed at line 881).
        surface_blocks_padded    = np.full((Hpad, Wpad), "stone", dtype=object)
        subsurface_blocks_padded = np.full((Hpad, Wpad), "stone", dtype=object)

        # Paint halo using the same per-biome noise-layer stacks that painted
        # the inner tile. The function iterates np.unique(biome_grid_padded)
        # so biomes only present in the halo get painted too. We pass the
        # padded-grid world-space offset (tile origin minus pad_px) so noise
        # fields stay spatially consistent with the inner tile's painting.
        _noise_layers_cfg = cfg.get("noise_layers_biome") if isinstance(cfg, dict) else None
        if _noise_layers_cfg:
            _apply_noise_layers(
                surface_blocks_padded, subsurface_blocks_padded,
                biome_grid_padded, _noise_layers_cfg,
                Hpad, Wpad,
                tile_x * W - _pad_px, tile_y * H - _pad_px,
            )

        # Overwrite the inner region with the authoritative inner arrays
        # (which include river banks, gap painting, clearings — all the layers
        # applied before this point in decorate_surface).
        surface_blocks_padded[_inner]    = surface_blocks
        subsurface_blocks_padded[_inner] = subsurface_blocks

        # Build padded noise_b using the SAME OpenSimplex fBm generator as
        # the inner-tile noise_b (line 803). World-space coords shifted by
        # -_pad_px on each axis so the padded field is continuous with the
        # inner field (inner 512x512 of padded matches the inner noise_b
        # byte-for-byte — it's the same deterministic function of world
        # coord). _apply_ecotone_dither reads this at line 1765 for ±20%
        # organic-edge modulation.
        _den_cfg = cfg["decoration_density_noise"]
        _den_gen = noise_fields["decoration_density"]
        noise_b_padded = _noise_tile(
            _den_gen, Hpad, Wpad,
            tile_x * W - _pad_px, tile_y * H - _pad_px,
            scale=_den_cfg["scale"], octaves=_den_cfg["octaves"],
        )

        # Build padded gap_mask — inner from eco_grads.gap_mask, halo=0 (no
        # gap protection outside inner, but halo pixels aren't swap candidates
        # anyway since the dither's swap writes land within its own bookkeeping
        # and we slice to inner after).
        if _gap_for_ecotone is not None:
            gap_mask_padded = np.zeros((Hpad, Wpad), dtype=_gap_for_ecotone.dtype)
            gap_mask_padded[_inner] = _gap_for_ecotone
        else:
            gap_mask_padded = None

        # Run ecotone dither on padded arrays. Writes land into both inner
        # and halo regions; we then extract the inner slice back into the
        # authoritative surface/subsurface arrays used by the rest of
        # decorate_surface.
        _apply_ecotone_dither(
            surface_blocks_padded, subsurface_blocks_padded,
            biome_grid_padded, noise_b_padded, cfg,
            tile_x, tile_y,
            gap_mask=gap_mask_padded,
            use_new_geology=use_new_geology,
        )

        # Slice inner back — this is the Phase 3b output.
        surface_blocks[...]    = surface_blocks_padded[_inner]
        subsurface_blocks[...] = subsurface_blocks_padded[_inner]

        # Free the padded scratch arrays; they aren't needed downstream.
        del surface_blocks_padded, subsurface_blocks_padded
        del noise_b_padded
        if gap_mask_padded is not None:
            del gap_mask_padded
    else:
        _apply_ecotone_dither(
            surface_blocks, subsurface_blocks,
            biome_grid, noise_b, cfg,
            tile_x, tile_y,
            gap_mask=_gap_for_ecotone,
            use_new_geology=use_new_geology,
        )

    # --- S60 high-elevation stone palette fade ------------------------------
    # Ramp grass/dirt-family surface blocks toward each biome's lithology
    # stone palette as `surface_y` climbs. Complements the gap==5 rock logic
    # (which fires on slope) by handling the flat-but-high case — alpine
    # plateaus shouldn't read as grass even if slope is modest. Also kills
    # ground cover above the full-fade band so bare rock stays bare.
    # S84: bumped from 230-280 for 768-height world. Y 480-580 matches
    # real-world bare-rock altitude (~3500-4500m) in our compressed scale.
    # S85: nudged start 480 -> 500 per user direction — keeps mid-mountain forests
    # green longer, only bleaches into stone at true alpine zone.
    _FADE_Y_START = 500   # 0% fade below this
    _FADE_Y_FULL  = 580   # 100% fade above this
    _GRASS_FAMILY = frozenset({
        "grass_block", "podzol", "coarse_dirt", "packed_mud",
        "rooted_dirt", "moss_block", "dirt", "mycelium",
    })
    # S71-3 swap: AT KEEPS its high-elevation stone-fade EXEMPTION so that
    # coarse_dirt cells stay coarse_dirt at altitude → GC palette can fire on
    # them.  Snowgap (now applied to AT again per user direction) provides the
    # peak-snow visual where altitude/slope warrant it.
    _above = (surface_y >= _FADE_Y_START) & (biome_grid != "ARCTIC_TUNDRA")
    if _above.any():
        _fade_prob = np.clip(
            (surface_y.astype(np.float32) - _FADE_Y_START) /
            max(1.0, float(_FADE_Y_FULL - _FADE_Y_START)),
            0.0, 1.0,
        )
        _rng_fade = np.random.default_rng(tile_x * 1234567 ^ tile_y * 8765432 ^ 0xFADE)
        _coin_fade = _rng_fade.random((H, W)).astype(np.float32)
        _scatter_pal = _rng_fade.random((H, W)).astype(np.float32)
        _grass_mask = np.isin(surface_blocks, list(_GRASS_FAMILY))
        _fade_mask = _above & _grass_mask & (_coin_fade < _fade_prob)
        if _fade_mask.any():
            _litho_cfg = cfg.get("lithology", {}) if isinstance(cfg, dict) else {}
            _zone_to_group = _litho_cfg.get("zone_to_group", {})
            _groups = _litho_cfg.get("groups", {})
            _DEFAULT_STONE_PAL = ["stone", "andesite", "granite", "diorite"]

            # S71-3: PAINTED lithology mask is source of truth for fade palette.
            # Only fall back to biome-based zone_to_group on unpainted cells.
            _gid_to_pal_fade: dict[int, list] = {}
            for _gname, _gdata in _groups.items():
                _gid_f = int(_gdata.get("id", 0))
                _gid_to_pal_fade[_gid_f] = _gdata.get("palette") or _DEFAULT_STONE_PAL

            _litho_at_res_fade = None
            if lithology_tile is not None:
                if lithology_tile.shape != (H, W):
                    from scipy.ndimage import zoom as _sd_zoom_fade
                    _zh_f = H / lithology_tile.shape[0]
                    _zw_f = W / lithology_tile.shape[1]
                    _litho_at_res_fade = _sd_zoom_fade(lithology_tile, (_zh_f, _zw_f), order=0)
                else:
                    _litho_at_res_fade = lithology_tile

            def _fade_palette_for_biome(biome_name: str) -> list:
                _gname = _zone_to_group.get(biome_name)
                _pal_b = (_groups.get(_gname, {}).get("palette")
                          if _gname else None) or _DEFAULT_STONE_PAL
                return _pal_b

            if _litho_at_res_fade is not None:
                for _gid_v in np.unique(_litho_at_res_fade[_fade_mask]):
                    _gid_v = int(_gid_v)
                    _bm_l = _fade_mask & (_litho_at_res_fade == _gid_v)
                    if not _bm_l.any():
                        continue
                    if _gid_v == 0:
                        # Unpainted — biome fallback
                        for _bname in np.unique(biome_grid[_bm_l]):
                            _bm_fb = _bm_l & (biome_grid == _bname)
                            if not _bm_fb.any():
                                continue
                            _pal = _fade_palette_for_biome(str(_bname))
                            _n = len(_pal)
                            for _i, _blk in enumerate(_pal):
                                _lo = _i / _n; _hi = (_i + 1) / _n
                                _band = _bm_fb & (_scatter_pal >= _lo) & (_scatter_pal < _hi)
                                if _band.any():
                                    surface_blocks[_band] = _blk
                            subsurface_blocks[_bm_fb] = _pal[0]
                    else:
                        _pal = _gid_to_pal_fade.get(_gid_v, _DEFAULT_STONE_PAL)
                        _n = len(_pal)
                        for _i, _blk in enumerate(_pal):
                            _lo = _i / _n; _hi = (_i + 1) / _n
                            _band = _bm_l & (_scatter_pal >= _lo) & (_scatter_pal < _hi)
                            if _band.any():
                                surface_blocks[_band] = _blk
                        subsurface_blocks[_bm_l] = _pal[0]
            else:
                # No lithology — pure biome fallback (legacy)
                for _bname in np.unique(biome_grid[_fade_mask]):
                    _bm = _fade_mask & (biome_grid == _bname)
                    if not _bm.any():
                        continue
                    _pal = _fade_palette_for_biome(str(_bname))
                    _n = len(_pal)
                    for _i, _blk in enumerate(_pal):
                        _lo = _i / _n; _hi = (_i + 1) / _n
                        _band = _bm & (_scatter_pal >= _lo) & (_scatter_pal < _hi)
                        if _band.any():
                            surface_blocks[_band] = _blk
                    subsurface_blocks[_bm] = _pal[0]

    # --- Ground cover --------------------------------------------------------
    # NOTE: ground cover is applied here provisionally; if the surface pipeline
    # runs below (use_new_surface_pipeline), a post-pipeline gating pass will
    # clear ground cover on pixels where non-plantable blocks were placed.
    _apply_ground_cover(
        ground_cover, surface_blocks,
        biome_grid, river_meta,
        noise_b, den_cfg,
        tile_x, tile_y,
        eco_grads=eco_grads,
        cliff_deg=cliff_deg,
        clearing_field=clearing_field,
    )

    # S59: Ecotone dither for ground cover — symmetric with surface/subsurface
    # dither above, same 30-block linear ramp + per-pixel salt-and-pepper, but
    # independent coin (different RNG seed). Must run before water cleanup so
    # swapped GC near water still gets cleared.
    _apply_ecotone_dither_ground_cover(
        ground_cover, biome_grid, noise_b, cfg,
        tile_x, tile_y,
        gap_mask=_gap_for_ecotone,
    )

    # S60: clear ground cover on bare rock. When the high-elevation height
    # fade converts a grass/dirt surface to stone-family blocks, flowers and
    # grass can't still sit on top. Also catches gap==5 rock pixels where
    # ground cover leaked through.
    _STONE_FAMILY = frozenset({
        "stone", "andesite", "granite", "diorite", "cobblestone", "tuff",
        "deepslate", "cobbled_deepslate", "calcite", "dripstone_block",
        "basalt", "smooth_basalt", "blackstone", "coal_block",
        "mossy_cobblestone", "dead_fire_coral_block", "dead_horn_coral_block",
        "suspicious_gravel", "suspicious_sand", "polished_granite",
        "polished_andesite", "polished_diorite", "smooth_stone",
        "sandstone", "red_sandstone", "smooth_sandstone", "orange_terracotta",
        "terracotta",
    })
    _on_stone = np.isin(surface_blocks, list(_STONE_FAMILY))
    if _on_stone.any():
        ground_cover[_on_stone] = ""

    # --- Clear floating vegetation near water --------------------------------
    # Ground_cover on river_meta > 0 pixels (bank/water) and adjacent pixels
    # appears to float over the water surface.  Clear it all.
    from scipy.ndimage import binary_dilation as _bd_cleanup
    water_px = river_meta > 0
    if water_px.any():
        near_water = _bd_cleanup(water_px, iterations=1)  # water + 1px margin
        ground_cover[near_water] = ""

    # --- FINAL meadow/floodplain override — absolute last pass ---------------
    # Meadow and floodplain clearings = grass_block, period. Overwrites
    # everything — slope zones, eco conditions, ecotone dither, river banks.
    # Dilate by 2px to cover contour-line gaps that the gap_mask missed.
    if eco_grads is not None and hasattr(eco_grads, 'gap_mask'):
        from scipy.ndimage import binary_dilation as _bd_meadow_final
        _raw_meadow = (eco_grads.gap_mask == 1) | (eco_grads.gap_mask == 4)
        # S70: skip RIPARIAN_WOODLAND + FRESHWATER_FEN + LUSH_RAINFOREST_COAST
        # + SAND_DUNE_DESERT.  Those biomes had floodplain (gap=4) suppressed
        # in eco_gradients (Item C), but if any meadow (gap=1) survives the
        # dilation we don't want it to force grass over what the user wants
        # there (trees / sand surface).  S70-f2 added LUSH; S70-f4 added DUNE.
        if biome_grid is not None:
            _raw_meadow = _raw_meadow & ~(
                (biome_grid == "RIPARIAN_WOODLAND")
                | (biome_grid == "FRESHWATER_FEN")
                | (biome_grid == "LUSH_RAINFOREST_COAST")
                | (biome_grid == "SAND_DUNE_DESERT"))
        if _raw_meadow.any():
            _final_meadow = _bd_meadow_final(_raw_meadow, iterations=2)
            # Don't expand into water, rock, snow, alpine meadow, sand dune, or beach
            _water = (river_meta > 0) if river_meta is not None else np.zeros((H, W), dtype=bool)
            _protected_gaps = (
                (eco_grads.gap_mask == 5) |  # rock
                (eco_grads.gap_mask == 6) |  # alpine_meadow
                (eco_grads.gap_mask == 7) |  # snow
                (eco_grads.gap_mask == 8) |  # sand_dune
                (eco_grads.gap_mask == 9)    # beach
            )
            _final_meadow = _final_meadow & ~_water & ~_protected_gaps & (surface_y >= 63)
            _fm_rng = np.random.default_rng(
                tile_x * 48271 ^ tile_y * 31337 ^ 0xF14A1)
            _fm_rand = _fm_rng.random((H, W)).astype(np.float32)
            # Everything → grass_block except ~5% coarse_dirt
            _fm_coarse = _final_meadow & (_fm_rand < 0.05)
            surface_blocks[_final_meadow & ~_fm_coarse] = "grass_block"
            surface_blocks[_fm_coarse] = "coarse_dirt"

    # ------------------------------------------------------------------
    # Phase 0.75 shadow-mode hookup (S45)
    # ------------------------------------------------------------------
    # Run `core/surface_pipeline.run_passes()` against a SurfaceContext built
    # from the finished production output, with an EMPTY layer list. This
    # smoke-tests the SurfaceContext construction + protocol invariants on
    # real tile data with zero impact — an empty layer list is structurally
    # guaranteed not to mutate `prior_surface` (see run_passes source; the
    # outer `for layers in passes:` loop iterates zero times). Phase 2 will
    # replace the empty list with real temperate-mountain layers.
    #
    # Gate: cfg['surface_pipeline']['shadow_mode'] OR env VANDIR_SHADOW=1.
    # Failure policy: any exception is logged to stdout and SWALLOWED — the
    # production tuple below is never touched by this block. The try/except
    # exists to contain bugs in context construction so a developer bug in
    # the shadow path cannot regress production tiles.
    _sp_cfg = cfg.get("surface_pipeline", {}) if isinstance(cfg, dict) else {}
    _shadow_flag_on = bool(_sp_cfg.get("shadow_mode", False))
    import os as _shadow_os
    _shadow_env_on = _shadow_os.environ.get("VANDIR_SHADOW", "") == "1"
    if _shadow_flag_on or _shadow_env_on:
        try:
            from core.surface_pipeline import run_passes as _shadow_run_passes
            from core.layers.protocol import SurfaceContext as _ShadowCtx
            _shadow_eco: dict = {}
            if eco_grads is not None:
                for _attr in (
                    "moisture_index", "wind_exposure", "concavity_norm",
                    "soil_depth", "gap_mask", "aspect",
                    "riparian_proximity", "lake_fringe",
                    "rock_exposure_gradient", "rock_tight_gradient",
                    "snow_caps_gradient",
                ):
                    _v = getattr(eco_grads, _attr, None)
                    if _v is not None:
                        _shadow_eco[_attr] = _v
            _shadow_ctx = _ShadowCtx(
                tile_x=int(tile_x),
                tile_z=int(tile_y),
                biome_grid=biome_grid,
                lithology_grid=None,  # Phase 0.5 flag still OFF
                eco_grads=_shadow_eco,
                column_output={"surface_y": surface_y},
                prior_surface=surface_blocks.copy(),
                prior_ownership=np.zeros((H, W), dtype=np.uint16),
                overlay_touched=np.zeros((H, W), dtype=np.uint8),
            )
            _shadow_result = _shadow_run_passes([], _shadow_ctx, strict=True)
            # Empty layer list guarantees _shadow_result.surface ==
            # _shadow_ctx.prior_surface element-wise. Discarded on purpose.
            del _shadow_result
        except Exception as _shadow_exc:  # noqa: BLE001
            print(
                f"[shadow] ERROR tile=({tile_x},{tile_y}): "
                f"{type(_shadow_exc).__name__}: {_shadow_exc}"
            )

    # -----------------------------------------------------------------------
    # Phase 2.0 — New surface pipeline for temperate cliff/talus layers.
    # When use_new_surface_pipeline=True, run the layer-based pipeline on
    # temperate cliff pixels and merge results into surface_blocks. Legacy
    # path already ran above — pipeline overwrites only claimed pixels.
    # Subsurface is NOT touched (geology column owns subsurface).
    # -----------------------------------------------------------------------
    if use_new_surface_pipeline:
        try:
            from core.surface_pipeline import run_pass as _sp_run_pass
            from core.layers.protocol import SurfaceContext as _SPCtx
            from core.layers.pass2_surface import (
                WeatheredTop,
                RiverBar,
                DesertPavement,
            )

            # Build lithology grid at tile resolution (512×512).
            _litho_512: np.ndarray | None = None
            if lithology_tile is not None:
                from scipy.ndimage import zoom as _sp_zoom
                _zh = H / lithology_tile.shape[0]
                _zw = W / lithology_tile.shape[1]
                _litho_512 = _sp_zoom(lithology_tile, (_zh, _zw), order=0)

            # Build eco_grads dict for SurfaceContext.
            _pipe_eco: dict = {}
            if eco_grads is not None:
                for _attr in (
                    "moisture_index", "wind_exposure", "concavity_norm",
                    "soil_depth", "gap_mask", "aspect", "north_factor",
                    "riparian_proximity", "lake_fringe",
                    "rock_exposure_gradient", "rock_tight_gradient",
                    "snow_caps_gradient", "sand_dunes_gradient",
                ):
                    _v = getattr(eco_grads, _attr, None)
                    if _v is not None:
                        _pipe_eco[_attr] = _v
            if cliff_deg is not None:
                _pipe_eco["cliff_deg"] = cliff_deg
            _pipe_eco["surface_y"] = surface_y
            _pipe_eco["noise_b"] = noise_b

            # Instantiate layers.
            # Note: ForestSurface removed in S50 — legacy decorate_surface()
            # handles forest floor blocks correctly via per-biome logic + gap_mask.
            _litho_cfg = cfg.get("lithology", {}) if isinstance(cfg, dict) else {}
            _layers = [
                # TemperateCliffFace, TemperateTalusApron, VerticalFluting
                # retired S56 — Gaea slope mask drives rock gap directly.
                # GrassTerrace() — disabled S56 to confirm banding culprit
                WeatheredTop(),
                RiverBar(),
                DesertPavement(),
            ]

            _sp_ctx = _SPCtx(
                tile_x=int(tile_x),
                tile_z=int(tile_y),
                biome_grid=biome_grid,
                lithology_grid=_litho_512,
                eco_grads=_pipe_eco,
                column_output={"surface_y": surface_y},
                prior_surface=surface_blocks.copy(),
                prior_ownership=np.zeros((H, W), dtype=np.uint16),
                overlay_touched=np.zeros((H, W), dtype=np.uint8),
            )

            _sp_result = _sp_run_pass(_layers, _sp_ctx, strict=True)

            # Merge: overwrite surface_blocks where pipeline claimed pixels.
            _claimed = _sp_result.ownership > 0
            _overlaid = _sp_result.overlay_touched > 0
            _changed = _claimed | _overlaid
            if _changed.any():
                surface_blocks[_changed] = _sp_result.surface[_changed]

        except Exception as _sp_exc:  # noqa: BLE001
            print(
                f"[surface_pipeline] ERROR tile=({tile_x},{tile_y}): "
                f"{type(_sp_exc).__name__}: {_sp_exc}"
            )

        # ---- Post-pipeline ground cover gating (Phase 3 foundation) ----------
        # Clear ground cover on pixels where the surface pipeline placed
        # non-plantable blocks (stone, gravel, cobblestone, etc.).  These
        # blocks can't physically support grass/fern growth — ground cover
        # on cliff faces and talus aprons looks wrong.
        _NON_PLANTABLE = frozenset({
            "stone", "andesite", "granite", "diorite", "deepslate",
            "cobblestone", "mossy_cobblestone", "cobbled_deepslate",
            "gravel", "calcite", "tuff", "dripstone_block",
            "basalt", "smooth_basalt", "blackstone",
            "sandstone", "red_sandstone", "smooth_sandstone",
            "terracotta", "orange_terracotta", "brown_terracotta",
            "white_terracotta", "yellow_terracotta", "red_terracotta",
            "light_gray_terracotta",
            "packed_mud",
            # S70-f5: REMOVED sand, red_sand, suspicious_sand — these DO
            # support cactus, dead_bush, short_dry_grass, tall_dry_grass
            # in MC 1.21+.  Keeping them in the list nuked ALL vegetation
            # on SAND_DUNE_DESERT (sand surface), which was the bug user
            # reported as "literal zero vegetation in dunes".
            "snow_block", "powder_snow", "ice", "packed_ice", "blue_ice",
        })
        _np_mask = np.zeros((H, W), dtype=bool)
        for _blk in _NON_PLANTABLE:
            _np_mask |= (surface_blocks == _blk)
        _gc_cleared = _np_mask & (ground_cover != "")
        if _gc_cleared.any():
            ground_cover[_gc_cleared] = ""

    # ──────────────────────────────────────────────────────────────────
    # S68: SBT MOUNTAINCAP REMAP — unified decision before snow carpet.
    # Where SNOWY_BOREAL_TAIGA is below altitude threshold OR in dither band
    # with noise failing, remap biome_grid to BOREAL_ALPINE.  This:
    #   (1) prevents snow_carpet from placing there (not in snowy list)
    #   (2) makes chunk_writer emit plains MC tag (no weather snow)
    # Single source of truth: if biome_grid stays SBT, both carpet + snowy_taiga
    # tag apply.  If remapped to BA, neither applies.
    _apply_sbt_mountaincap_remap(biome_grid, surface_y, tile_x, tile_y, cfg)

    # S85: ARCTIC_TUNDRA → SBT below Y 500 remap REMOVED.  User-painted ARC_TUN
    # in override.tif now appears as ARC_TUN regardless of altitude.  Painted
    # intent is canonical (same philosophy as BIOME_ALTITUDE_REMAPS removal).

    # ──────────────────────────────────────────────────────────────────
    # S64: SNOW CARPET PASS
    # Places `snow[layers=1]` on snowy biomes (SBT, ARCTIC_TUNDRA, FROZEN_FLATS,
    # BOREAL_ALPINE) via ground_cover.  Boundary dither matches surface-block
    # dither aesthetic.  Runs before the ocean pass so ocean decoration
    # can still overwrite ground_cover on ocean pixels.
    # ──────────────────────────────────────────────────────────────────
    _apply_snow_carpet(
        surface_blocks, ground_cover, biome_grid, cfg, tile_x, tile_y,
    )

    # ──────────────────────────────────────────────────────────────────
    # S62: OCEAN DECORATION PASS
    # Runs LAST.  Only modifies pixels where biome is _OCEAN/_DEFAULT AND
    # surface_y < sea_level.  Land pixels are physically unreachable by
    # this call via the `is_ocean` guard inside decorate_ocean().
    # Feature-flagged via cfg["ocean"]["enabled"].
    # ──────────────────────────────────────────────────────────────────
    if cfg.get("ocean", {}).get("enabled", False):
        try:
            from core.ocean_decorator import decorate_ocean
            # Tile world origin: tile_x/tile_y are tile indices; multiply by
            # tile width (512) to get block coords for simplex seeding.
            _tile_wx = int(tile_x) * surface_y.shape[1]
            _tile_wz = int(tile_y) * surface_y.shape[0]
            decorate_ocean(
                surface_y=surface_y,
                surface_blk=surface_blocks,
                sub_blk=subsurface_blocks,
                biome_grid=biome_grid,
                cfg=cfg,
                tile_world_x=_tile_wx,
                tile_world_z=_tile_wz,
                ground_cover=ground_cover,  # S63: pass for underwater vegetation
            )
        except Exception as _e:
            # Ocean decoration is best-effort — a crash here should NOT
            # break tile rendering.  Log + skip.
            print(f"[ocean_decorator] SKIPPED (tile {tile_x},{tile_y}): {_e}",
                  flush=True)

    return surface_blocks, subsurface_blocks, ground_cover


# ---------------------------------------------------------------------------
# BIOME BOUNDARY ECOTONE DITHER
# ---------------------------------------------------------------------------

def _apply_ecotone_dither(
    surface_blocks:    np.ndarray,   # (H, W) object — modified in-place
    subsurface_blocks: np.ndarray,   # (H, W) object — modified in-place
    biome_grid:        np.ndarray,   # (H, W) object (str)
    noise_b:           np.ndarray,   # (H, W) float32 [0, 1] decoration noise
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
    gap_mask:          np.ndarray | None = None,  # skip rock/alpine pixels
    use_new_geology:   bool = False,
) -> None:
    """Dither biome boundaries by probabilistically mixing blocks from
    adjacent biomes within a transition band.

    For each pixel within *width_px* of a biome boundary, compute the
    probability of keeping the current biome's block vs adopting the
    nearest neighbour biome's block.  Probability is sigmoid-shaped
    (smooth at center, saturated at edges) and modulated by noise for
    organic, ragged transitions.

    Skips ocean pixels and single-biome tiles.
    """
    from scipy.ndimage import distance_transform_edt, gaussian_filter

    H, W = biome_grid.shape
    eco_cfg   = cfg.get("eco_ground_cover", {})
    width_px  = int(eco_cfg.get("ecotone_width_px", 30))   # S58: 48 → 30. Linear ramp now used; 30 = visible fade extent on each side of boundary.
    sharpness = float(eco_cfg.get("ecotone_sigmoid_sharpness", 4.0))  # legacy, only used if cfg sets ecotone_use_linear=False

    unique_biomes = [b for b in np.unique(biome_grid) if not str(b).startswith("_")]
    if len(unique_biomes) < 2:
        return  # single-biome tile — nothing to blend
        # NOTE: this also skips biome boundaries that straddle tile seams
        # (different biome on this tile vs. the neighbour).  Cross-tile
        # ecotone awareness is a deferred carry-forward.

    # Seeded RNG for deterministic dithering. S58: reverted from S55 v2
    # gaussian-filtered (sigma=3 → §3 coherent 3-10 block lobes) back to
    # true per-pixel uniform random — NOISE_PATTERNS §1 salt-and-pepper
    # to match the §4 gradient+decision spec the dither implements.
    # Lobed decision coin produced visible "fingers" perpendicular to the
    # boundary; per-pixel coin gives the soft 1-block-scale fade the user
    # asked for.
    rng = np.random.default_rng(tile_x * 48271 ^ tile_y * 31337 ^ 0xEC0D17E)
    rand_field = rng.random((H, W)).astype(np.float32)

    # For each biome, compute distance-to-boundary (signed: positive = inside)
    # and find the nearest neighbour biome at each pixel.
    # Work at full resolution — EDT on 512×512 bool is ~3ms each.
    dist_maps: dict[str, np.ndarray] = {}
    for biome in unique_biomes:
        bname = str(biome)
        mask = biome_grid == biome
        if not mask.any():
            continue
        # Distance from outside this biome to its boundary
        dist_inside = distance_transform_edt(mask).astype(np.float32)
        dist_maps[bname] = dist_inside

    # For each pixel near a boundary, find which other biome is closest
    # Build a "nearest other biome" grid by finding the biome with the
    # smallest distance_transform value (excluding current biome)
    near_boundary = np.zeros((H, W), dtype=bool)
    for bname, dist in dist_maps.items():
        near_boundary |= (dist > 0) & (dist <= width_px)

    if not near_boundary.any():
        return

    # For boundary pixels: find the nearest OTHER biome
    # Strategy: for each boundary pixel, check which other biome's distance
    # map has the smallest value (= that biome's boundary is closest)
    biome_names = list(dist_maps.keys())
    if len(biome_names) < 2:
        return

    # Stack all distance maps into (N, H, W) and find per-pixel assignments
    # Current biome distance = how far inside we are
    # Other biome distances = how far from their boundary we are (via EDT of ~mask)
    other_dist_maps: dict[str, np.ndarray] = {}
    for bname in biome_names:
        mask = biome_grid == bname
        # Distance from this biome's boundary (for pixels OUTSIDE this biome)
        dist_outside = distance_transform_edt(~mask).astype(np.float32)
        other_dist_maps[bname] = dist_outside

    # For each boundary pixel, find nearest neighbour biome
    neighbour_biome = np.empty((H, W), dtype=object)
    neighbour_biome[:] = ""
    min_other_dist = np.full((H, W), np.inf, dtype=np.float32)

    if not near_boundary.any():
        return

    for bname in biome_names:
        d = other_dist_maps[bname]
        # Only consider this as neighbour where it's NOT the current biome
        is_other = (biome_grid != bname) & near_boundary
        closer = is_other & (d < min_other_dist)
        if closer.any():
            neighbour_biome[closer] = bname
            min_other_dist[closer] = d[closer]

    # Now apply the dither: for each boundary pixel with a valid neighbour,
    # compute blend probability and potentially swap blocks.
    # Skip rock exposure and alpine meadow pixels — their surface blocks
    # are set by the rock_exposure gradient, not by biome palette.
    has_neighbour = near_boundary & (neighbour_biome != "")
    if gap_mask is not None:
        # Don't ecotone-dither rock (5) or alpine meadow (6) — their blocks
        # are gradient-driven, not biome-palette-driven
        has_neighbour = has_neighbour & (gap_mask != 5) & (gap_mask != 6) & (gap_mask != 7) & (gap_mask != 8) & (gap_mask != 9)
    if not has_neighbour.any():
        return

    # Get current biome's distance-inside for blend computation
    dist_inside = np.zeros((H, W), dtype=np.float32)
    for bname, d in dist_maps.items():
        mask = biome_grid == bname
        dist_inside[mask] = d[mask]

    # S58: Boundary meander disabled (default amp=0). Was used when the
    # biome boundary itself was a straight EDT line; now soften_biome_boundaries
    # produces a wide organic boundary at the assignment level, so additional
    # distance perturbation is redundant and noisy.
    _meander_cfg = cfg.get("ecotone_meander", {}) if isinstance(cfg, dict) else {}
    _meander_amp_blocks = float(_meander_cfg.get("amplitude_px", 0.0))
    if _meander_amp_blocks > 0.0:
        _meander_scale = float(_meander_cfg.get("scale", 40.0))
        _meander_octaves = int(_meander_cfg.get("octaves", 2))
        _meander_noise = _noise_tile(
            _ecotone_meander_gen(cfg), H, W, tile_x * W, tile_y * H,
            scale=_meander_scale, octaves=_meander_octaves,
        )
        dist_inside_effective = dist_inside + ((_meander_noise - 0.5) * 2.0 * _meander_amp_blocks).astype(np.float32)
    else:
        dist_inside_effective = dist_inside

    # S58: Linear ramp + per-pixel salt-and-pepper, NOISE_PATTERNS §4 spec.
    # Was sigmoid + cap, which produced a flat-plateau-then-drop pattern that
    # didn't read as a true gradient. Linear ramp gives a clean visible
    # 30-block fade from max-swap at the boundary to 0 at width_px deep.
    # Cap controls max swap rate at the boundary (0.5 = up to 50% of
    # right-at-boundary pixels get swapped to neighbour blocks).
    _swap_cap = float(cfg.get("eco_ground_cover", {}).get("ecotone_swap_cap", 0.5))
    if _swap_cap <= 0.0:
        return  # dither disabled
    t = dist_inside_effective[has_neighbour] / width_px  # 0=boundary, 1=width_px deep
    # S85: align with NOISE_PATTERNS.md §4 plateau-clamp pattern (S71 fix that was
    # only applied to _compute_ecotone_swap_fields, not here).  Clip floor at 0.15
    # so deep-inside cells still get 15% swap chance — gives visible salt-and-pepper
    # across the FULL width_px transition zone instead of a near-hard cutoff where
    # the linear ramp approaches zero.  User: surface block boundaries still looked
    # harsh after the width 40 -> 100 / swap_cap 0.75 -> 0.85 widening because the
    # ramp was dropping to 0 at 100 blocks deep, not staying at the cap.
    swap_prob = np.clip(_swap_cap * (1.0 - t), 0.15, _swap_cap)
    # S85: REMOVED ±20% noise_b modulation. `noise_b` is a simplex/fBm noise
    # field (spatially correlated, scale ~10-50 blocks). Multiplying swap_prob
    # by it created visible GAUSSIAN BLOBS at the boundary — high-swap and
    # low-swap regions clustered into ~20-block blobs instead of clean
    # per-pixel salt-and-pepper. Anti-pattern called out in CLAUDE.md hard
    # rules. User noticed blobs in 33,6 at width=100. Pure plateau-clamp +
    # per-pixel rand_field decision is the NOISE_PATTERNS.md §4 spec.

    # Stochastic swap: if rand < swap_prob, adopt neighbour's block
    do_swap = rand_field[has_neighbour] < swap_prob

    if not do_swap.any():
        return

    # Get the block that the neighbour biome has at each swap pixel
    # We read from the already-computed surface_blocks/subsurface_blocks
    # but need the neighbour biome's version. Since blocks are already placed,
    # we can look up what the neighbour biome's nearest pixel has.
    # For each swap pixel, sample a block from the neighbour biome
    swap_idx = np.where(has_neighbour)
    swap_r = swap_idx[0][do_swap]
    swap_c = swap_idx[1][do_swap]

    if len(swap_r) == 0:
        return

    # S86 Item 1F (full): ecotone block-swap uses NEAREST-PIXEL sampling
    # from the neighbor biome's real painted blocks.
    #
    # Evolution:
    #   S85 Option A: lookup from noise_layers_biome config — used PRE-S55
    #     artifact tags, produced "weird giant sand blobs".
    #   S86 1F-lite: random sample from neighbor biome's painted pixels —
    #     colors correct but lost spatial structure (sand blobs in desert
    #     no longer cluster as blobs at the boundary).
    #   S86 1F (this): nearest-pixel sample via distance_transform_edt. For
    #     each swap pixel, find the closest pixel of the neighbor biome and
    #     copy its block. Colors AND blob structure preserved — if you're
    #     near a sand patch in the desert, you get sand; near a stone patch,
    #     you get stone; the boundary blends through whatever the neighbor
    #     biome was actually rendering at that locale.
    from scipy.ndimage import distance_transform_edt as _ec_dt
    nb_at_swap = neighbour_biome[swap_r, swap_c]
    for bname in biome_names:
        bname_mask = nb_at_swap == bname
        if not bname_mask.any():
            continue

        target_r = swap_r[bname_mask]
        target_c = swap_c[bname_mask]

        # For pixels OUTSIDE the neighbor biome, find the index of the nearest
        # pixel INSIDE the neighbor biome.  Returned `(iy, ix)` are (H, W)
        # arrays where each cell holds the (row, col) of the closest True
        # cell of `_biome_mask`.
        _biome_mask = biome_grid == bname
        if not _biome_mask.any():
            continue  # neighbor biome not present in this tile padded region
        _, (iy, ix) = _ec_dt(~_biome_mask, return_indices=True)

        sampled_r = iy[target_r, target_c]
        sampled_c = ix[target_r, target_c]
        surface_blocks[target_r, target_c] = surface_blocks[sampled_r, sampled_c]
        if not use_new_geology:
            subsurface_blocks[target_r, target_c] = subsurface_blocks[sampled_r, sampled_c]


def _compute_ecotone_swap_fields(
    biome_grid: np.ndarray,   # (H, W) object str
    cfg:        dict,
    gap_mask:   np.ndarray | None = None,
    noise_b:    np.ndarray | None = None,   # (H, W) float32 [0, 1]; None = no modulation
) -> tuple | None:
    """S59: Shared ecotone-swap geometry for GC and schematic seam dither.

    Returns ``(has_neighbour, neighbour_biome, swap_prob_grid, biome_names,
    width_px, swap_cap)`` or ``None`` when the dither is disabled or there is
    no multi-biome boundary to soften.

    ``swap_prob_grid`` is an ``(H, W)`` float32 field — 0 outside the ramp,
    ``cap * (1 - dist/width)`` inside, optionally modulated by
    ``0.8 + 0.4 * noise_b`` for ±20% organic-edge variation. Callers roll
    their own per-pixel coin and compare to this grid.

    Note: no padding — runs on the caller's biome grid as-is.
    """
    from scipy.ndimage import distance_transform_edt

    H, W = biome_grid.shape
    eco_cfg  = cfg.get("eco_ground_cover", {})
    width_px = int(eco_cfg.get("ecotone_width_px", 30))
    swap_cap = float(eco_cfg.get("ecotone_swap_cap", 0.5))
    if swap_cap <= 0.0:
        return None

    unique_biomes = [b for b in np.unique(biome_grid) if not str(b).startswith("_")]
    if len(unique_biomes) < 2:
        return None

    dist_maps: dict[str, np.ndarray] = {}
    other_dist_maps: dict[str, np.ndarray] = {}
    for biome in unique_biomes:
        bname = str(biome)
        mask = biome_grid == biome
        if not mask.any():
            continue
        dist_maps[bname]       = distance_transform_edt(mask).astype(np.float32)
        other_dist_maps[bname] = distance_transform_edt(~mask).astype(np.float32)
    biome_names = list(dist_maps.keys())
    if len(biome_names) < 2:
        return None

    near_boundary = np.zeros((H, W), dtype=bool)
    for d in dist_maps.values():
        near_boundary |= (d > 0) & (d <= width_px)
    if not near_boundary.any():
        return None

    neighbour_biome = np.empty((H, W), dtype=object)
    neighbour_biome[:] = ""
    min_other_dist = np.full((H, W), np.inf, dtype=np.float32)
    for bname in biome_names:
        d = other_dist_maps[bname]
        is_other = (biome_grid != bname) & near_boundary
        closer = is_other & (d < min_other_dist)
        if closer.any():
            neighbour_biome[closer] = bname
            min_other_dist[closer] = d[closer]

    has_neighbour = near_boundary & (neighbour_biome != "")
    if gap_mask is not None:
        has_neighbour = has_neighbour & (gap_mask != 5) & (gap_mask != 6) & (gap_mask != 7) & (gap_mask != 8) & (gap_mask != 9)
    if not has_neighbour.any():
        return None

    # (H, W) dist_inside grid
    dist_inside = np.zeros((H, W), dtype=np.float32)
    for bname, d in dist_maps.items():
        mask = biome_grid == bname
        dist_inside[mask] = d[mask]

    # NOISE_PATTERNS.md §4 — Gradient probability ramp + decision coin with
    # PLATEAU CLAMP.  S71-3 fix: previous formula `swap_cap * (1 - t)` ramped
    # from cap down to 0, which gave near-zero swap probability deep on the
    # inside of the seam and at the outer edge — boundaries still read as
    # nearly hard lines.  Per the doc: "clip probability to [0.15, 0.85]
    # within the seam zone — guarantees visible salt-and-pepper on BOTH
    # sides of the boundary instead of mostly-inside inner / mostly-outside
    # outer sub-bands."  Floor at 0.15 means even far-inside cells still
    # have a 15% swap chance; ceiling at swap_cap (0.75) keeps boundary
    # pixels from being 100% swapped.
    t = dist_inside / width_px
    swap_prob_grid = np.clip(swap_cap * (1.0 - t), 0.15, swap_cap).astype(np.float32)
    # S85: REMOVED ±20% noise_b multiplicative modulation. noise_b is a simplex/fBm
    # field (spatially correlated, scale ~10-50 blocks). Multiplying swap_prob by it
    # created visible gaussian blobs at boundaries — same anti-pattern as the
    # gaussian-coin removal in S58. Anti-pattern called out in CLAUDE.md hard rules.
    # NOISE_PATTERNS.md §4 spec is pure plateau-clamp + per-pixel rand decision.
    # Outside the has_neighbour zone (no nearby boundary), zero out so
    # interior pixels stay at biome's own palette.
    swap_prob_grid = np.where(has_neighbour, swap_prob_grid, 0.0).astype(np.float32)

    # S71: RIPARIAN_BIOMES dither boost — user reported missing visible dither
    # at FRESHWATER_FEN boundaries on (8,73).  Bump swap_prob 1.6x for boundary
    # pixels in any riparian biome so the cross-biome veg + block scatter is
    # visible against neighbouring rainforest/coast palettes (which have similar
    # wet-palette blocks → low natural contrast).
    _RIPARIAN_BIOMES = ("RIPARIAN_WOODLAND", "FRESHWATER_FEN",
                        "MANGROVE_COAST", "TIDAL_JUNGLE_FRINGE",
                        "LUSH_RAINFOREST_COAST", "RAINFOREST_COAST")
    rip_mask = np.zeros((H, W), dtype=bool)
    for _rb in _RIPARIAN_BIOMES:
        rip_mask |= (biome_grid == _rb)
    if rip_mask.any():
        swap_prob_grid = np.where(rip_mask, np.clip(swap_prob_grid * 1.6, 0.0, 0.85),
                                  swap_prob_grid).astype(np.float32)

    return has_neighbour, neighbour_biome, swap_prob_grid, biome_names, width_px, swap_cap


def _apply_ecotone_dither_ground_cover(
    ground_cover: np.ndarray,   # (H, W) object — modified in-place
    biome_grid:   np.ndarray,   # (H, W) object (str)
    noise_b:      np.ndarray,   # (H, W) float32 [0, 1] — for ±20% ramp modulation
    cfg:          dict,
    tile_x:       int,
    tile_y:       int,
    gap_mask:     np.ndarray | None = None,
) -> None:
    """S59: Ground cover counterpart to ``_apply_ecotone_dither``.

    Same shape (30-block linear ramp, 0.5 cap, per-pixel uniform salt-and-pepper,
    ±20% noise_b modulation) as the surface/subsurface dither but with an
    independent per-pixel coin (different RNG seed) — matches user's explicit
    "same shape as block dither" (not "shared coin") choice in S59.

    At swap pixels, sample a random pixel of the neighbour biome's area within
    this tile and copy its ground_cover value (captures the full GC palette
    diversity including "" air/none pixels).

    No padding — runs on inner tile only. Cross-tile seam asymmetry for GC at
    the 1-pixel tile boundary is accepted as a cosmetic carry-forward (noted
    in CLAUDE.md and §18).
    """
    fields = _compute_ecotone_swap_fields(biome_grid, cfg, gap_mask, noise_b)
    if fields is None:
        return
    has_neighbour, neighbour_biome, swap_prob_grid, biome_names, _, _ = fields

    H, W = biome_grid.shape
    # Independent per-pixel coin — different seed from surface/sub (0xEC0D17E).
    # 0x9C0DEC0 = "ground cover ecotone" tag seed, distinct.
    rng = np.random.default_rng(tile_x * 48271 ^ tile_y * 31337 ^ 0x9C0DEC0)
    rand_field = rng.random((H, W)).astype(np.float32)

    do_swap_grid = rand_field < swap_prob_grid
    if not do_swap_grid.any():
        return

    swap_r, swap_c = np.where(do_swap_grid)
    if len(swap_r) == 0:
        return

    # S86 Item 1F (full): same nearest-pixel sampling as the surface ecotone
    # path. Preserves spatial structure of neighbor biome's GC blobs across
    # the boundary instead of random-sampling from anywhere.
    from scipy.ndimage import distance_transform_edt as _gc_dt
    nb_at_swap = neighbour_biome[swap_r, swap_c]
    for bname in biome_names:
        bname_mask = nb_at_swap == bname
        if not bname_mask.any():
            continue
        biome_mask = biome_grid == bname
        if not biome_mask.any():
            continue

        target_r = swap_r[bname_mask]
        target_c = swap_c[bname_mask]

        _, (iy, ix) = _gc_dt(~biome_mask, return_indices=True)
        sampled_r = iy[target_r, target_c]
        sampled_c = ix[target_r, target_c]

        ground_cover[target_r, target_c] = ground_cover[sampled_r, sampled_c]


# ---------------------------------------------------------------------------
# S52: _apply_slope_zones() DELETED.  Was a legacy 3-zone slope system
# (45°/65° thresholds + 8px talus dilation) that wrote stone/gravel/
# cobblestone on gap==0 steep pixels.  Fully replaced by surface pipeline:
#   TemperateCliffFace (35°+), TemperateTalusApron (18-35°),
#   GrassTerrace (8-18°).  _DESERT_BIOMES_SET also removed.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# DESERT ROCK PALETTE — multi-layer composition (Session 41)
# ---------------------------------------------------------------------------

def _apply_desert_rock_palette(
    surface_blocks:    np.ndarray,   # modified in-place
    subsurface_blocks: np.ndarray,   # modified in-place
    desert_rock_px:    np.ndarray,   # bool mask of pixels to paint
    surface_y:         np.ndarray,   # (H,W) int16
    cliff_deg:         np.ndarray,   # (H,W) float32 degrees
    flow_tile:         np.ndarray,   # (H,W) float32 [0,1] drainage accumulation
    eco_grads,                       # EcoGradients namedtuple (aspect, concavity_norm)
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
    H:                 int,
    W:                 int,
) -> None:
    """Physical-realism desert rock painting.

    Replaces noise-driven blob filler with eco_grads-driven physical layers:
      Layer 1: Slope class (flat / moderate / steep)
      Layer 2: Aspect-driven coloring (south=warm, north=brown, ±0.1 edge jitter)
      Layer 3: Wind erosion (west-facing blasted brown)
      Layer 4: Flow-driven wash channels (drainage paths get sand/sandstone)
      Layer 5: Concavity fluting on cliffs (convex ridges vs concave gullies)
      Layer 6: Stratification bands draped over terrain (concavity-jittered)

    Physical signals come from eco_grads (aspect, concavity_norm) and flow_tile.
    Noise is used ONLY for ±0.1 edge jitter to break perfect contour artifacts.
    """
    if not desert_rock_px.any():
        return

    # Single noise field for edge jitter (replaces 3 noise calls)
    px_off = tile_x * W
    py_off = tile_y * H
    edge_noise = _gen_layer_noise("simplex", scale=24.0, seed=11000,
                                   H=H, W=W, px_off=px_off, py_off=py_off)
    edge_jitter = (edge_noise - 0.5) * 0.20  # ±0.10

    # Physical signals from eco_grads
    aspect = eco_grads.aspect
    concavity_norm = eco_grads.concavity_norm  # 0=convex/ridge, 1=concave/gully
    south_factor = -np.cos(aspect).astype(np.float32)            # +1 = south, -1 = north
    windward = np.cos(aspect - np.float32(np.radians(270.0))).astype(np.float32)

    # Slope class — thresholds calibrated for actual block-scale slopes
    # at 512px tile resolution (real slopes peak around 30-40° even on steep terrain)
    is_flat     = desert_rock_px & (cliff_deg <  18.0)
    is_moderate = desert_rock_px & (cliff_deg >= 18.0) & (cliff_deg < 35.0)
    is_steep    = desert_rock_px & (cliff_deg >= 35.0)
    not_steep   = desert_rock_px & ~is_steep

    # ── Step 1: BASE ── orange_terracotta default
    surface_blocks[desert_rock_px] = "orange_terracotta"

    # ── Step 2: Aspect-driven coloring (with subtle edge jitter only) ──
    # South-facing → terracotta (sun-baked oxidation)
    sf_jit = south_factor + edge_jitter
    sun_baked = not_steep & (sf_jit > 0.50)
    surface_blocks[sun_baked] = "terracotta"
    # North-facing → brown_terracotta (shaded, weathered) — tight threshold
    shaded = not_steep & (sf_jit < -0.70)
    surface_blocks[shaded] = "brown_terracotta"
    del sf_jit, sun_baked, shaded

    # ── Step 3: Wind erosion DISABLED ──
    # Wind blast was indistinguishable from north-facing shaded — both
    # painted brown_terracotta on overlapping pixels and combined to
    # dominate the rock zone at >30%. Aspect alone is enough for darkening.

    # ── Step 4: Flow-driven wash channels (wadis) ──
    # Drainage paths get sediment-filled smooth_sandstone, peaks get sand.
    # Desert flow values are tiny — thresholds tuned for that range.
    if flow_tile is not None:
        wash_zone = desert_rock_px & (flow_tile > 0.005)
        if wash_zone.any():
            surface_blocks[wash_zone] = "smooth_sandstone"
            strong_wash = desert_rock_px & (flow_tile > 0.020)
            surface_blocks[strong_wash] = "sand"
            del wash_zone, strong_wash

    # ── Step 5: Stratification bands (steep only, draped over terrain) ──
    # Use concavity_norm as the band perturbation source so band edges
    # follow local terrain rather than slicing horizontally.
    if is_steep.any():
        band_y = 14.0
        band_offset = (concavity_norm - 0.5) * 12.0  # ±6Y, terrain-following
        band_idx = (((surface_y.astype(np.float32) + band_offset) / band_y).astype(np.int32)) % 3
        band_blocks = ["terracotta", "brown_terracotta", "orange_terracotta"]
        for i, blk in enumerate(band_blocks):
            m = is_steep & (band_idx == i)
            surface_blocks[m] = blk
        del band_offset, band_idx

    # ── Step 6: subsurface base ──
    subsurface_blocks[desert_rock_px] = "terracotta"

    # ── Step 7: Basalt cap rock (LAST so nothing overwrites it) ──
    # Real geology: hard volcanic basalt resists erosion → from above
    # appears as scattered cap rock at convex high points and ridge fins.
    # Combine concavity (convex) with slope (ridges) for more coverage —
    # in real pipeline concavity_norm has narrower distribution than test.
    ridge_fin = desert_rock_px & (
        (concavity_norm < 0.45) |  # convex (loose)
        ((concavity_norm < 0.50) & (cliff_deg > 22.0))  # any sub-mean concavity on slopes
    )
    if ridge_fin.any():
        surface_blocks[ridge_fin] = "basalt"
        subsurface_blocks[ridge_fin] = "smooth_basalt"
    del ridge_fin


# ---------------------------------------------------------------------------
# RIVER BANK FEATURES
# ---------------------------------------------------------------------------

def _apply_river_banks(
    surface_blocks: np.ndarray,   # modified in-place
    ground_cover:   np.ndarray,   # modified in-place
    river_meta:     np.ndarray,   # uint8: 0=none,1=stream,2=river,3=lake bank
    flow_tile:      np.ndarray,
    biome_grid:     np.ndarray,
    moisture_tile:  np.ndarray,
    noise_b:        np.ndarray,
    cfg:            dict,
    eco_grads=None,
    noise_fields=None,
    tile_x: int = 0,
    tile_y: int = 0,
) -> None:
    """
    Apply riparian corridor features using wide, noise-modulated material
    bands.  Uses riparian_proximity from eco_grads for smooth distance-based
    transitions.  Falls back to river_meta-only when eco_grads is None.

    The key design: each band uses NOISE to stochastically MIX multiple
    materials rather than placing a single solid block.  This prevents the
    visible "stripes" that a hard band system creates.
    """
    bank_mask = river_meta > 0

    if not np.any(bank_mask) and (eco_grads is None or eco_grads.riparian_proximity.max() < 0.01):
        return

    H, W = flow_tile.shape

    # Gaussian random noise — true per-pixel random, seeded for determinism.
    # Produces 1-2 block clusters max, colloquially "noisy" speckle.
    px_off = tile_x * W
    py_off = tile_y * H
    _bank_rng = np.random.default_rng(seed=tile_x * 73856093 ^ tile_y * 19349663 ^ 999983)
    bank_noise = _bank_rng.random((H, W)).astype(np.float32)

    # Build moisture biome mask once
    high_moist_mask = np.zeros((H, W), dtype=bool)
    for biome in np.unique(biome_grid):
        if str(biome) in HIGH_MOISTURE_BIOMES:
            high_moist_mask |= (biome_grid == biome)

    # -- Compute river width field for proportional dither ──────────────────
    from scipy.ndimage import distance_transform_edt as _edt, binary_dilation as _bdil
    water_core = (river_meta > 0)
    if water_core.any():
        # River width at each bank pixel ≈ distance across the channel
        dist_from_water = _edt(~water_core).astype(np.float32)
        # Estimate local river width: max(distance_inside) near each pixel
        from scipy.ndimage import maximum_filter as _maxf
        dist_inside = _edt(water_core).astype(np.float32)
        river_half_width = _maxf(dist_inside, size=21)  # local max radius
        # Dither radius scales with river width: min 4px, max 16px
        dither_radius = np.clip(river_half_width * 1.5, 4, 16).astype(np.float32)
    else:
        dist_from_water = np.full((H, W), 999.0, dtype=np.float32)
        dither_radius = np.full((H, W), 6.0, dtype=np.float32)

    # -- 1. Direct bank pixels (river_meta > 0) ──────────────────────────────
    if water_core.any():
        # Speckle mix: mud/coarse_dirt/grass_block/rooted_dirt/dirt + rare clay
        mud_bank   = water_core & (bank_noise < 0.38)
        cdirt_bank = water_core & (bank_noise >= 0.38) & (bank_noise < 0.58)
        grass_bank = water_core & (bank_noise >= 0.58) & (bank_noise < 0.74)
        rdirt_bank = water_core & (bank_noise >= 0.74) & (bank_noise < 0.84)
        dirt_bank  = water_core & (bank_noise >= 0.84) & (bank_noise < 0.96)
        clay_bank  = water_core & (bank_noise >= 0.995) # <1% ultra-sparse clay
        surface_blocks[mud_bank]   = "mud"
        surface_blocks[cdirt_bank] = "coarse_dirt"
        surface_blocks[grass_bank] = "grass_block"
        surface_blocks[rdirt_bank] = "rooted_dirt"
        surface_blocks[dirt_bank]  = "dirt"
        surface_blocks[clay_bank]  = "clay"

        # Lake bank grass — place short grass with rare tall grass on the
        # dry lake bank pixels (river_meta == CHAN_LAKE).
        # Skip the innermost 2px to avoid grass floating at the waterline.
        lake_bank_dry = water_core & (river_meta == 3)  # CHAN_LAKE = 3
        if lake_bank_dry.any():
            # Only place grass on bank pixels > 2px from actual water
            from scipy.ndimage import distance_transform_edt as _edt_lb
            carved_or_river = (river_meta > 0) & ~lake_bank_dry  # water pixels
            dist_from_water_edge = _edt_lb(~carved_or_river | lake_bank_dry).astype(np.float32)
            far_enough = dist_from_water_edge >= 2.0
            grassable = ((surface_blocks == "grass_block") | (surface_blocks == "dirt")
                         | (surface_blocks == "podzol") | (surface_blocks == "rooted_dirt"))
            bank_grass_ok = lake_bank_dry & grassable & far_enough
            lb_tg = bank_grass_ok & (ground_cover == "") & (bank_noise > 0.90)
            ground_cover[lb_tg] = "tall_grass"
            lb_sg = bank_grass_ok & (ground_cover == "") & (bank_noise > 0.45) & (bank_noise <= 0.90)
            ground_cover[lb_sg] = "short_grass"

    # -- 2. Width-proportional dither rings ────────────────────────────────────
    # Each pixel's dither probability decays with distance / local dither_radius
    if water_core.any():
        # Expand to max possible dither extent
        max_dither = 16
        full_fringe = _bdil(water_core, iterations=max_dither) & ~water_core
        if full_fringe.any():
            # Probability = 1 - (dist / dither_radius), clamped to [0,1]
            safe_dr = np.maximum(dither_radius, 1.0)
            prob = np.clip(1.0 - dist_from_water / safe_dr, 0.0, 1.0)
            # Stochastic: place where random < probability
            dither_place = full_fringe & (bank_noise < prob)

            # Material mix in dither zone — inner gets more mud, outer more grass
            frac = np.clip(dist_from_water / safe_dr, 0.0, 1.0)  # 0=near water, 1=edge
            d_mud   = dither_place & (bank_noise < 0.25 * (1.0 - frac))
            d_cdirt = dither_place & ~d_mud & (bank_noise < 0.22 + 0.15 * frac)
            d_rdirt = dither_place & ~d_mud & ~d_cdirt & (bank_noise < 0.30 + 0.12 * frac)
            d_grass = dither_place & ~d_mud & ~d_cdirt & ~d_rdirt
            surface_blocks[d_mud]   = "mud"
            surface_blocks[d_cdirt] = "coarse_dirt"
            surface_blocks[d_rdirt] = "rooted_dirt"
            surface_blocks[d_grass] = "grass_block"

            # Short grass on dither zone with rare tall grass
            # Skip innermost 2px from water to avoid floating-looking grass
            dither_far = dither_place & (dist_from_water >= 2.0)
            tg_place = dither_far & (ground_cover == "") & (bank_noise > 0.88)
            ground_cover[tg_place] = "tall_grass"
            sg_place = dither_far & (ground_cover == "") & (bank_noise > 0.50) & (bank_noise <= 0.88)
            ground_cover[sg_place] = "short_grass"

    # -- 3. Lake fringe (wider bands, noise-modulated) ----------------------
    if eco_grads is not None and eco_grads.lake_fringe.max() > 0.01:
        lf = eco_grads.lake_fringe

        # Inner lake fringe (0.65-1.0): gradual mix, mostly grass_block
        lake_inner = (lf >= 0.65) & ~bank_mask
        if lake_inner.any():
            li_mud  = lake_inner & (bank_noise < 0.15)
            li_cdrt = lake_inner & (bank_noise >= 0.15) & (bank_noise < 0.28)
            li_rdrt = lake_inner & (bank_noise >= 0.28) & (bank_noise < 0.40)
            li_dirt = lake_inner & (bank_noise >= 0.40) & (bank_noise < 0.55)
            li_gblk = lake_inner & (bank_noise >= 0.55)
            li_clay = lake_inner & (bank_noise >= 0.995)  # <1% clay
            surface_blocks[li_mud]  = "mud"
            surface_blocks[li_cdrt] = "coarse_dirt"
            surface_blocks[li_rdrt] = "rooted_dirt"
            surface_blocks[li_dirt] = "dirt"
            surface_blocks[li_gblk] = "grass_block"
            surface_blocks[li_clay] = "clay"
            # Short grass with rare tall grass
            tg_place = lake_inner & (ground_cover == "") & (bank_noise > 0.88)
            ground_cover[tg_place] = "tall_grass"
            sg_place = lake_inner & (ground_cover == "") & (bank_noise > 0.40) & (bank_noise <= 0.88)
            ground_cover[sg_place] = "short_grass"

        # Outer lake fringe (0.25-0.65): dithered transition into biome
        lake_outer = (lf >= 0.25) & (lf < 0.65) & ~bank_mask
        if lake_outer.any():
            # Stochastic placement — probability decreases with distance
            outer_prob = (lf[lake_outer] - 0.25) / 0.40  # 1.0 near inner, 0.0 at edge
            outer_place = lake_outer.copy()
            outer_place[lake_outer] = bank_noise[lake_outer] < outer_prob
            lo_moss   = outer_place & high_moist_mask & (noise_b > 0.45)
            lo_podzol = outer_place & ~high_moist_mask & (noise_b > 0.50)
            lo_rdirt  = outer_place & (noise_b <= 0.45) & (bank_noise < 0.30)
            surface_blocks[lo_moss]   = "moss_block"
            surface_blocks[lo_podzol] = "podzol"
            surface_blocks[lo_rdirt]  = "rooted_dirt"
            # Short grass with rare tall grass in outer fringe
            otg = lake_outer & (ground_cover == "") & (bank_noise > 0.88) & (noise_b > 0.50)
            ground_cover[otg] = "tall_grass"
            osg = lake_outer & (ground_cover == "") & (bank_noise > 0.40) & (bank_noise <= 0.88)
            ground_cover[osg] = "short_grass"


# ---------------------------------------------------------------------------
# GROUND COVER
# ---------------------------------------------------------------------------

def _apply_ground_cover(
    ground_cover:   np.ndarray,   # modified in-place
    surface_blocks: np.ndarray,
    biome_grid:     np.ndarray,
    river_meta:     np.ndarray,
    noise_b:        np.ndarray,
    den_cfg:        dict,
    tile_x:         int,
    tile_y:         int,
    eco_grads=None,
    cliff_deg:      np.ndarray | None = None,
    clearing_field: np.ndarray | None = None,
) -> None:
    """
    Place ground cover blocks (surface_y + 1) per biome.

    When eco_grads is available, three ecological mechanisms modulate placement:
      1. **Canopy-driven density** — high moisture_index + soil_depth = dense canopy
         → shade-loving species (fern, moss) boosted, sun-loving (flowers) suppressed.
      2. **Ecotone transitions** — at biome boundaries, blend palettes from both biomes
         within a configurable transition width.
      3. **Species colonies** — Voronoi-like clustering where specific species dominate
         small patches, mimicking natural seed dispersal.
      4. **Slope-based suppression** — cliff faces (≥35°) get no ground cover, talus
         slopes (18-35°) get sparse cover, moderate slopes (8-18°) get reduced cover.

    Falls back to legacy noise×density when eco_grads is None.
    """
    H, W = ground_cover.shape
    floor      = den_cfg.get("floor", 0.35)
    rng        = np.random.default_rng(tile_x * 73856093 ^ tile_y * 19349663 ^ 7654321)
    rand_field = rng.random((H, W)).astype(np.float32)

    # ---- Slope-based density gating (Phase 3 foundation) ---------------------
    # Cliff faces cannot support ground cover; talus and moderate slopes have
    # reduced density.  This is the physical-realism gate: steep rock doesn't
    # grow grass.
    slope_density_mod = np.ones((H, W), dtype=np.float32)
    if cliff_deg is not None:
        slope_density_mod[cliff_deg >= 35.0] = 0.0    # cliff: no ground cover
        slope_density_mod[(cliff_deg >= 18.0) & (cliff_deg < 35.0)] = 0.10  # talus: sparse
        slope_density_mod[(cliff_deg >= 8.0) & (cliff_deg < 18.0)] = 0.50   # moderate: reduced

    # ---- Ecological density modulation ------------------------------------
    eco_density_mod = np.ones((H, W), dtype=np.float32)
    canopy_proxy    = None
    gap_mask        = None
    if eco_grads is not None:
        # Canopy proxy: high in moist, deep-soil forest zones
        canopy_proxy = np.clip(
            eco_grads.moisture_index * 0.6 + eco_grads.soil_depth * 0.4,
            0.0, 1.0,
        )
        # Under canopy: overall density slightly boosted (more litter/ground cover)
        # Open areas: slightly reduced (drier, windier)
        eco_density_mod = 0.7 + 0.6 * canopy_proxy  # [0.7, 1.3]

        # Gap mask: clearings override canopy proxy to 0 (full sun)
        if hasattr(eco_grads, 'gap_mask'):
            gap_mask = eco_grads.gap_mask
            in_gap = gap_mask > 0
            if in_gap.any():
                canopy_proxy[in_gap] = 0.0
                # Meadow/windthrow: boost ground cover density (open ground = more grass)
                meadow_px = gap_mask == 1
                windthrow_px = gap_mask == 2
                bare_px = gap_mask == 3
                flood_px = gap_mask == 4
                rock_px = gap_mask == 5
                alpine_meadow_px = gap_mask == 6
                eco_density_mod[meadow_px] = 1.4    # lush meadow
                eco_density_mod[windthrow_px] = 1.1  # moderate understory
                eco_density_mod[flood_px] = 1.5     # lush floodplain
                eco_density_mod[rock_px] = 0.15      # very sparse on rock
                eco_density_mod[alpine_meadow_px] = 1.0  # moderate alpine grass
                snow_cap_px = gap_mask == 7
                eco_density_mod[snow_cap_px] = 0.02  # almost nothing on snow
                sand_dune_px = gap_mask == 8
                # S70-f5: was 0.05 (95% suppress) → 0.20 (80% suppress).
                # Diagnostic showed only ~1160 plants per 262k tile (<0.5%).
                # User wants "rareish but visible" general desert vegetation.
                # 4x bump → ~4600 plants/tile = ~1 per 56 px = clearly visible.
                eco_density_mod[sand_dune_px] = 0.20
                beach_px = gap_mask == 9
                eco_density_mod[beach_px] = 0.02  # bare beach
                # S55 v8: thin vegetation on the beach-edge transition zone
                # (dither pixels that did NOT become sand) so the underlying
                # biome floor blocks — mud, coarse_dirt, moss_block, podzol —
                # show through the plants.  Without this, ground cover hides
                # the salt-and-pepper mix and the transition looks like a
                # hard sand→forest cutoff from aerial view.
                _beach_edge_mod = getattr(eco_grads, "beach_edge_mask", None)
                if _beach_edge_mod is not None and _beach_edge_mod.any():
                    eco_density_mod[_beach_edge_mod] = 0.15  # 15% vegetation

                # S67: KARST_BARRENS is scrubland even on rock — bushes grow in
                # limestone cracks.  Override rock_px suppression back toward 1.0
                # so the biome's bush density (0.30) shows through.
                _karst_mask = biome_grid == "KARST_BARRENS"
                if _karst_mask.any():
                    # Keep at least 0.9 — heavy bushes on rocky karst landscape
                    eco_density_mod[_karst_mask] = np.maximum(
                        eco_density_mod[_karst_mask], 0.9)

    # ---- Species colony map -----------------------------------------------
    # Hash-based colony assignment: divides tile into ~48px cells,
    # each cell dominated by one species from the biome palette.
    colony_scale = 48
    colony_boost = 1.8  # density multiplier for the dominant species
    if eco_grads is not None:
        eco_gc_cfg = den_cfg  # could read from cfg["eco_ground_cover"] if present
        # Colony seed grid: which species index dominates each cell
        cx = np.arange(W) + tile_x * W
        cz = np.arange(H) + tile_y * H
        cxx, czz = np.meshgrid(cx, cz)
        # Simple spatial hash for colony assignment
        colony_hash = ((cxx // colony_scale) * 73856093 ^
                       (czz // colony_scale) * 19349663) & 0x7FFFFFFF
        colony_idx = colony_hash.astype(np.int32)  # used per-biome below
    else:
        colony_idx = None

    # ---- Meadow/floodplain edge ring (precomputed for tall_grass boost) ----
    meadow_edge_ring = None
    if gap_mask is not None:
        meadow_any = (gap_mask == 1) | (gap_mask == 4) | (gap_mask == 6)
        if meadow_any.any():
            from scipy.ndimage import distance_transform_edt as _edt_medge
            _mdist = _edt_medge(meadow_any).astype(np.float32)
            meadow_edge_ring = meadow_any & (_mdist > 0) & (_mdist <= 5)

    # ---- Per-biome placement (weighted random selection) -------------------
    # Each candidate pixel picks ONE species via weighted random choice
    # based on density.  This guarantees species variety proportional to
    # their density weights instead of early species starving later ones.
    for biome in np.unique(biome_grid):
        biome_str = str(biome)
        palette   = GROUND_COVER_PALETTES.get(biome_str)
        if not palette:
            continue

        bio_mask = (biome_grid == biome) & (river_meta == 0) & (ground_cover == "")
        if not bio_mask.any():
            continue
        n_species = len(palette)

        # S70-f5 DIAG (kept silent — turn on by setting _DIAG_SD True).
        _DIAG_SD = False

        # Noise multiplier in [floor, 1.0], further modulated by ecology + slope
        density_mult = floor + (1.0 - floor) * noise_b
        density_mult = density_mult * eco_density_mod * slope_density_mod

        # Compute per-species final density arrays
        species_densities = []

        # Gap-specific species multipliers (applied on top of canopy adjustment)
        _MEADOW_SPECIES = {
            "short_grass": 3.0, "poppy": 2.5, "dandelion": 2.5,
            "cornflower": 2.0, "allium": 2.0, "azure_bluet": 2.0,
            "oxeye_daisy": 2.0,
            "tall_grass": 0.3, "fern": 0.0, "large_fern": 0.0,
            "azalea": 0.0, "flowering_azalea": 0.0, "leaf_litter": 0.0,
            "moss_carpet": 0.0, "pale_moss_carpet": 0.0,
        }
        _WINDTHROW_SPECIES = {
            # Windthrow gaps: grass + very sparse bush
            "short_grass": 3.0, "tall_grass": 2.5,
            "bush": 0.3,
        }
        # _ALPINE_MEADOW_SPECIES retired S56
        _ROCK_SPECIES = {
            # Bare rock: almost nothing grows
            "dead_bush": 2.0, "short_dry_grass": 1.5,
        }
        _SNOW_CAP_SPECIES = {
            # Snow caps: essentially nothing
            "dead_bush": 0.02,
        }
        _SAND_DUNE_SPECIES = {
            # Sand dunes (gap==8): S70 — gap==8 strictly fires only in
            # SAND_DUNE_DESERT after the Item D strict gate, so these
            # multipliers are by definition the SAND_DUNE_DESERT defaults.
            # User direction (S70-f5): general desert vegetation on dunes,
            # NOT wadi-near-river patches.  Grass species + dead bushes +
            # very rare cacti.  No wet-biome species (ferns, moss, flowers).
            "bush": 0.5,
            "dead_bush": 0.6,
            "short_dry_grass": 0.6,
            "tall_dry_grass": 0.5,
            "cactus": 0.05,         # S71: was 0.7 (S70-f5 restore) — user said much much rarer
            # Suppress everything else (ferns, mosses, flowers — wet)
            "tall_grass": 0.0,
            "fern": 0.0, "large_fern": 0.0, "leaf_litter": 0.0,
            "moss_carpet": 0.0, "pale_moss_carpet": 0.0,
            "azalea": 0.0, "flowering_azalea": 0.0,
        }
        _FLOODPLAIN_SPECIES = {
            "tall_grass": 3.5, "short_grass": 2.0,
            "fern": 1.5,
            "dead_bush": 0.8,
            "poppy": 0.4, "cornflower": 0.3, "oxeye_daisy": 0.3,
            "azalea": 0.0, "flowering_azalea": 0.0,
            "leaf_litter": 0.0, "moss_carpet": 0.0,
            "pale_moss_carpet": 0.0, "large_fern": 0.5,
        }

        for sp_idx, (block, base_density) in enumerate(palette):
            adjusted_density = base_density
            if canopy_proxy is not None:
                if block in ("fern", "large_fern", "leaf_litter",
                             "moss_carpet", "pale_moss_carpet",
                             "short_dry_grass", "lily_of_the_valley"):
                    adjusted_density = base_density * (0.5 + canopy_proxy * 1.0)
                elif block in ("poppy", "dandelion", "cornflower", "allium",
                               "azure_bluet", "oxeye_daisy", "sunflower",
                               "pink_tulip", "white_tulip", "red_tulip",
                               "orange_tulip", "torchflower", "dead_bush",
                               "short_dry_grass", "tall_dry_grass"):
                    adjusted_density = base_density * (1.2 - canopy_proxy * 0.7)

            fd = np.clip(adjusted_density * density_mult, 0.0, 1.0)

            # Gap-specific overrides: shift species weights inside clearings
            if gap_mask is not None:
                meadow_px = bio_mask & (gap_mask == 1)
                if meadow_px.any():
                    mult = _MEADOW_SPECIES.get(block, 0.5)
                    fd[meadow_px] = np.clip(fd[meadow_px] * mult, 0.0, 1.0)
                    # Meadow edge effect: tall_grass boosted at forest-meadow
                    # boundary (partial shade + nutrients = ranker growth)
                    if block == "tall_grass" and meadow_edge_ring is not None:
                        edge_here = bio_mask & meadow_edge_ring
                        if edge_here.any():
                            fd[edge_here] = np.clip(fd[edge_here] * 4.0, 0.0, 1.0)
                windthrow_px = bio_mask & (gap_mask == 2)
                if windthrow_px.any():
                    mult = _WINDTHROW_SPECIES.get(block, 0.0)
                    fd[windthrow_px] = np.clip(fd[windthrow_px] * mult, 0.0, 1.0)
                flood_px = bio_mask & (gap_mask == 4)
                if flood_px.any():
                    mult = _FLOODPLAIN_SPECIES.get(block, 0.4)
                    fd[flood_px] = np.clip(fd[flood_px] * mult, 0.0, 1.0)
                rock_exp_px = bio_mask & (gap_mask == 5)
                if rock_exp_px.any():
                    mult = _ROCK_SPECIES.get(block, 0.05)
                    fd[rock_exp_px] = np.clip(fd[rock_exp_px] * mult, 0.0, 1.0)
                # gap==6 (alpine_meadow) species suppression retired S56
                snow_cap_px = bio_mask & (gap_mask == 7)
                if snow_cap_px.any():
                    # S71: ARCTIC_TUNDRA exception — gap==7 covers most of
                    # this biome, but user wants the F.1 palette (bushes,
                    # short_dry_grass, etc.) to fire even on the snow surface.
                    # Use full palette density there instead of the near-zero
                    # _SNOW_CAP_SPECIES multiplier that empties everything.
                    if False:  # S71-3 swap: AT exception removed (back to harsh-mountain)
                        pass
                    else:
                        mult = _SNOW_CAP_SPECIES.get(block, 0.0)
                        fd[snow_cap_px] = np.clip(fd[snow_cap_px] * mult, 0.0, 1.0)
                sand_dune_px = bio_mask & (gap_mask == 8)
                if sand_dune_px.any():
                    mult = _SAND_DUNE_SPECIES.get(block, 0.0)
                    fd[sand_dune_px] = np.clip(fd[sand_dune_px] * mult, 0.0, 1.0)

            # S57 Phase 3a: Boreal moss carpet concavity modulation.
            # moss density scales with concavity_norm — denser in basins/hollows,
            # sparser on convex ridges.  Scope: BOREAL_TAIGA only.  Snow biomes
            # (SNOWY_BOREAL_TAIGA, ARCTIC_TUNDRA, FROZEN_FLATS) are excluded
            # because moss_carpet blocks MC snow_layer accumulation.
            if block in ("moss_carpet", "pale_moss_carpet"):
                if biome_str == "BOREAL_TAIGA" and eco_grads is not None:
                    # moss_density_mult = 0.5 + 0.5 * concavity_norm -> [0.5, 1.0]
                    _moss_mult = (0.5 + 0.5 * eco_grads.concavity_norm).astype(np.float32)
                    fd = np.clip(fd * _moss_mult, 0.0, 1.0)
                    del _moss_mult
                elif biome_str in ("SNOWY_BOREAL_TAIGA", "ARCTIC_TUNDRA", "FROZEN_FLATS"):
                    # Zero out moss in snow biomes (snow_layer accumulation fix)
                    fd = np.zeros_like(fd)

            if colony_idx is not None and n_species > 0:
                is_dominant = (colony_idx % n_species) == sp_idx
                fd = np.where(is_dominant,
                              np.clip(fd * colony_boost, 0.0, 1.0),
                              fd * 0.6)
            species_densities.append(fd)

        # Total density = probability that ANY species is placed
        total_density = np.zeros((H, W), dtype=np.float32)
        for fd in species_densities:
            total_density += fd
        total_density = np.clip(total_density, 0.001, None)

        # Decide which pixels get ground cover
        covered = bio_mask & (rand_field < np.clip(total_density, 0, 1))

        # For covered pixels, select species by cumulative weight
        if covered.any():
            # Second random draw for species selection
            species_rand = rng.random((H, W)).astype(np.float32)

            # Build cumulative thresholds per species
            cumulative = np.zeros((H, W), dtype=np.float32)
            for sp_idx, (block, _) in enumerate(palette):
                prev_cum = cumulative.copy()
                cumulative += species_densities[sp_idx] / total_density

                # This species wins where species_rand falls in its band
                sp_mask = covered & (species_rand >= prev_cum) & (species_rand < cumulative)

                # Sugar cane: only on specific surfaces
                if block == "sugar_cane":
                    valid_surf = ((surface_blocks == "mud") |
                                  (surface_blocks == "clay") |
                                  (surface_blocks == "grass_block"))
                    sp_mask = sp_mask & valid_surf

                if sp_mask.any():
                    ground_cover[sp_mask] = block

    # ---- Meadow clearing ground cover override (S57 Phase 3a, rewritten) ---
    # Gradient salt-and-pepper pattern matching surface_decorator Step 5.
    # Uses SAME RNG seed (0xC1EA5F) as the clearing surface block decision
    # so pixels align: a pixel with clearing surface also gets clearing cover.
    # Species ratios follow salt-and-pepper rule (no block >55%) — balanced
    # mix at 1-block scale, not dominant+trace.
    # Biome-gated to forested biomes + floodplain gap==4.  Runs AFTER the
    # main per-biome species loop so it overrides palette output.
    _CLEARING_BIOMES_GC = frozenset({
        "TEMPERATE_RAINFOREST", "TEMPERATE_DECIDUOUS", "BOREAL_TAIGA",
        "MIXED_FOREST", "BIRCH_FOREST", "RIPARIAN_WOODLAND",
    })
    if clearing_field is not None:
        from core.meadow_clearing_field import (
            CLEARING_INTERIOR_THRESHOLD as _CF_THR_GC,
            CLEARING_EDGE_BAND as _CF_HALF_GC,
        )
        _cgc_biome = np.zeros((H, W), dtype=bool)
        for _cb in _CLEARING_BIOMES_GC:
            _cgc_biome |= (biome_grid == _cb)
        # Also include floodplain corridors regardless of biome
        if gap_mask is not None:
            _cgc_biome |= (gap_mask == 4)
        if _cgc_biome.any():
            # Gradient clearing probability (identical to surface Step 5)
            _cgc_prob = np.clip(
                ((_CF_THR_GC + _CF_HALF_GC) - clearing_field) / (2.0 * _CF_HALF_GC),
                0.0, 1.0,
            ).astype(np.float32)

            # Decision coin with SAME seed as surface block decision →
            # pixel-aligned clearing/forest membership across both layers.
            _cgc_rng = np.random.default_rng(
                tile_x * 48271 ^ tile_y * 31337 ^ 0xC1EA5F)
            _cgc_decision = _cgc_rng.random((H, W)).astype(np.float32)
            _is_clearing_gc = _cgc_biome & (_cgc_decision < _cgc_prob)

            # Only paint over grass_block surface (the clearing-painted pixels).
            _cgc_grass = surface_blocks == "grass_block"
            _paint_gc = _is_clearing_gc & _cgc_grass

            if _paint_gc.any():
                # Clear existing ground cover on clearing pixels so the mix is fresh.
                ground_cover[_paint_gc] = ""

                # Salt-and-pepper species mix.  Interior vs seam proximity
                # decides mix flavor via a secondary gradient: deep interior
                # favors short_grass/fern; closer to edge mixes in tall_grass.
                # This creates a smooth tall_grass-band character at the seam
                # without a hard interior/seam split.
                # edge_factor: 0 in deep interior, 1 near threshold.
                _edge_factor = 1.0 - _cgc_prob  # [0..1], 0=deep clearing, 1=near forest
                _edge_factor = np.clip(_edge_factor, 0.0, 1.0).astype(np.float32)

                # Density floor: ~0.70 base (pixels get cover).  No density
                # gradient based on distance; variation comes from species mix.
                _den_rng = np.random.default_rng(
                    tile_x * 11111 ^ tile_y * 22222 ^ 0xC1EAD0)
                _den_roll = _den_rng.random((H, W)).astype(np.float32)
                _covered = _paint_gc & (_den_roll < 0.70)

                if _covered.any():
                    # Species selection — interior favors short_grass (clearing
                    # = grass sea per spec); tall_grass migrates to seam zone
                    # (boundary dither band).  Interpolate cumulative thresholds
                    # by edge_factor so the interior→seam transition is smooth.
                    _sp_rng = np.random.default_rng(
                        tile_x * 33333 ^ tile_y * 44444 ^ 0xC1EA5C)
                    _sp_noise = _sp_rng.random((H, W)).astype(np.float32)

                    # Interior distribution (deep clearing, edge_factor ~ 0):
                    #   short_grass=76.8 | fern=15 | bush=8 | flower=0.2
                    #   (NO tall_grass — clearing interior is a grass sea)
                    #   Flowers EXCEEDINGLY rare — ~1 per 500 covered pixels
                    # Seam distribution (near forest, edge_factor ~ 1):
                    #   short_grass=31 | tall_grass=36 | fern=21 | bush=11.5 | flower=0.5
                    #   (tall_grass dominant in the transition band,
                    #    flowers still very rare — 1 per 200 covered pixels)
                    # Interpolation is per-pixel via edge_factor (1 - clearing_prob).

                    # Interior cumulative thresholds:
                    _int_t1 = 0.768  # short_grass end
                    _int_t2 = 0.768  # tall_grass skipped in interior (0 width)
                    _int_t3 = 0.918  # fern end (0.768 + 0.150)
                    _int_t4 = 0.998  # bush end (0.918 + 0.080)
                    # flower: >= 0.998 (0.2% = ~1 per 500)

                    # Seam cumulative thresholds:
                    _seam_t1 = 0.310  # short_grass end
                    _seam_t2 = 0.670  # tall_grass end (0.310 + 0.360)
                    _seam_t3 = 0.880  # fern end (0.670 + 0.210)
                    _seam_t4 = 0.995  # bush end (0.880 + 0.115)
                    # flower: >= 0.995 (0.5% = ~1 per 200)

                    _t1 = _int_t1 + _edge_factor * (_seam_t1 - _int_t1)
                    _t2 = _int_t2 + _edge_factor * (_seam_t2 - _int_t2)
                    _t3 = _int_t3 + _edge_factor * (_seam_t3 - _int_t3)
                    _t4 = _int_t4 + _edge_factor * (_seam_t4 - _int_t4)

                    # Band 1: short_grass (always)
                    ground_cover[_covered & (_sp_noise < _t1)] = "short_grass"
                    # Band 2: tall_grass (near-zero width in interior, dominant in seam)
                    ground_cover[_covered & (_sp_noise >= _t1) & (_sp_noise < _t2)] = "tall_grass"
                    # Band 3: fern
                    ground_cover[_covered & (_sp_noise >= _t2) & (_sp_noise < _t3)] = "fern"
                    # Band 4: bush
                    ground_cover[_covered & (_sp_noise >= _t3) & (_sp_noise < _t4)] = "bush"
                    # Band 5: flower mix
                    _fl = _covered & (_sp_noise >= _t4)
                    if _fl.any():
                        _fl_sub = np.random.default_rng(
                            tile_x * 55555 ^ tile_y * 66666 ^ 0xC1EAFA).random((H, W)).astype(np.float32)
                        ground_cover[_fl & (_fl_sub < 0.25)] = "dandelion"
                        ground_cover[_fl & (_fl_sub >= 0.25) & (_fl_sub < 0.50)] = "poppy"
                        ground_cover[_fl & (_fl_sub >= 0.50) & (_fl_sub < 0.75)] = "oxeye_daisy"
                        ground_cover[_fl & (_fl_sub >= 0.75)] = "cornflower"
                        del _fl_sub

                    del _sp_rng, _sp_noise, _t1, _t2, _t3, _t4, _fl
                del _den_rng, _den_roll, _covered, _edge_factor

            del _cgc_rng, _cgc_decision, _cgc_prob, _is_clearing_gc, _cgc_grass, _paint_gc
        del _cgc_biome

    # ---- Floodplain (gap==4) edge ground cover softening (S57 Phase 3a) ----
    # Matches the surface_decorator gap==4 EDT softening. Uses same FP_SOFT_WIDTH
    # distance + per-pixel decision to paint clearing-style ground cover on
    # forest pixels just outside the gap==4 boundary.
    if gap_mask is not None:
        _fpgc_gap = gap_mask == 4
        if _fpgc_gap.any():
            from scipy.ndimage import distance_transform_edt as _edt_fpgc
            _fpgc_dist = _edt_fpgc(~_fpgc_gap).astype(np.float32)
            FPGC_SOFT_WIDTH = 8.0
            _fpgc_prob = np.clip(
                1.0 - (_fpgc_dist / FPGC_SOFT_WIDTH), 0.0, 1.0,
            ).astype(np.float32)
            _fpgc_zone = (~_fpgc_gap) & (_fpgc_dist > 0) & (_fpgc_dist <= FPGC_SOFT_WIDTH)
            if _fpgc_zone.any():
                _fpgc_rng = np.random.default_rng(
                    tile_x * 48271 ^ tile_y * 31337 ^ 0xF10D50)
                _fpgc_decision = _fpgc_rng.random((H, W)).astype(np.float32)
                _fpgc_paint_decision = _fpgc_zone & (_fpgc_decision < _fpgc_prob)
                _fpgc_grass = surface_blocks == "grass_block"
                _fpgc_paint = _fpgc_paint_decision & _fpgc_grass

                if _fpgc_paint.any():
                    # Clear then apply salt-and-pepper seam mix (tall_grass-leaning
                    # because this is the edge, not deep interior).
                    ground_cover[_fpgc_paint] = ""
                    _fpgc_den = np.random.default_rng(
                        tile_x * 11111 ^ tile_y * 22222 ^ 0xF10DD0).random((H, W)).astype(np.float32)
                    _fpgc_covered = _fpgc_paint & (_fpgc_den < 0.70)
                    if _fpgc_covered.any():
                        _fpgc_sp = np.random.default_rng(
                            tile_x * 33333 ^ tile_y * 44444 ^ 0xF10D5C).random((H, W)).astype(np.float32)
                        # Seam-leaning mix (same as clearing seam):
                        #   30% short_grass | 35% tall_grass | 20% fern | 10% bush | 5% flower
                        ground_cover[_fpgc_covered & (_fpgc_sp < 0.30)] = "short_grass"
                        ground_cover[_fpgc_covered & (_fpgc_sp >= 0.30) & (_fpgc_sp < 0.65)] = "tall_grass"
                        ground_cover[_fpgc_covered & (_fpgc_sp >= 0.65) & (_fpgc_sp < 0.85)] = "fern"
                        ground_cover[_fpgc_covered & (_fpgc_sp >= 0.85) & (_fpgc_sp < 0.95)] = "bush"
                        _fpgc_fl = _fpgc_covered & (_fpgc_sp >= 0.95)
                        if _fpgc_fl.any():
                            _fpgc_fl_sub = np.random.default_rng(
                                tile_x * 55555 ^ tile_y * 66666 ^ 0xF10DFA).random((H, W)).astype(np.float32)
                            ground_cover[_fpgc_fl & (_fpgc_fl_sub < 0.25)] = "dandelion"
                            ground_cover[_fpgc_fl & (_fpgc_fl_sub >= 0.25) & (_fpgc_fl_sub < 0.50)] = "poppy"
                            ground_cover[_fpgc_fl & (_fpgc_fl_sub >= 0.50) & (_fpgc_fl_sub < 0.75)] = "oxeye_daisy"
                            ground_cover[_fpgc_fl & (_fpgc_fl_sub >= 0.75)] = "cornflower"
                            del _fpgc_fl_sub
                    del _fpgc_sp, _fpgc_covered
                del _fpgc_rng, _fpgc_decision, _fpgc_paint_decision, _fpgc_grass, _fpgc_paint
            del _fpgc_dist, _fpgc_prob, _fpgc_zone
        del _fpgc_gap


# ---------------------------------------------------------------------------
# SMOKE TEST (stdlib only — no amulet, no rasterio)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("surface_decorator.py — smoke test")

    try:
        from opensimplex import OpenSimplex
    except ImportError:
        print("SKIP: opensimplex not installed — using stub")

        class OpenSimplex:  # type: ignore
            def __init__(self, seed=0): pass
            def noise2(self, x, y): return 0.0

    H, W = 64, 64
    tile_x, tile_y = 3, 5

    rng = np.random.default_rng(42)

    surface_y    = np.full((H, W), 80, dtype=np.int16)
    biome_grid   = np.full((H, W), "MIXED_FOREST",   dtype=object)
    biome_grid[:H//3, :]  = "ARCTIC_TUNDRA"
    biome_grid[H//3:H//2, :W//2] = "RIPARIAN_WOODLAND"
    biome_grid[H//2:, W//2:] = "SAND_DUNE_DESERT"

    erosion_tile  = rng.random((H, W)).astype(np.float32)
    moisture_tile = rng.random((H, W)).astype(np.float32)
    height_tile   = rng.random((H, W)).astype(np.float32)
    flow_tile     = rng.random((H, W)).astype(np.float32)

    river_meta = np.zeros((H, W), dtype=np.uint8)
    river_meta[28:36, :] = 2   # fake river band

    noise_fields = {
        "decoration_density": OpenSimplex(seed=42002),
    }

    cfg = {
        "block_mixing": {
            "erosion_threshold":   0.60,
            "erosion2_threshold":  0.75,
            "moisture_threshold":  0.65,
            "moisture2_threshold": 0.80,
            "noise_scale":         25,
            "noise_threshold":     0.68,
            "noise2_threshold":    0.75,
            "noise3_threshold":    0.82,
            "noise4_threshold":    0.88,
            "altitude_threshold":  0.72,
            "sparse_overrides": {
                "SAND_DUNE_DESERT": {"noise3_threshold": 0.85},
                "ARCTIC_TUNDRA":    {"noise2_threshold": 0.75},
            },
        },
        "decoration_density_noise": {"scale": 60, "octaves": 3, "floor": 0.15},
        "river_carving": {"bend_gradient_threshold": 0.30},
    }

    # ---- Test 1: Legacy path (no eco_grads) ----
    surf, sub, cover = decorate_surface(
        surface_y, biome_grid,
        erosion_tile, moisture_tile, height_tile,
        river_meta, flow_tile,
        noise_fields, cfg,
        tile_x, tile_y,
    )

    assert surf.shape  == (H, W), "surface shape mismatch"
    assert sub.shape   == (H, W), "subsurface shape mismatch"
    assert cover.shape == (H, W), "ground cover shape mismatch"

    # No bare dirt on surface
    dirt_pixels = np.sum(surf == "dirt")
    assert dirt_pixels == 0, f"bare dirt found on surface: {dirt_pixels} pixels"

    # Bank pixels should be mud, clay, or gravel
    bank_pixels = river_meta > 0
    bank_surf   = surf[bank_pixels]
    valid_bank  = {"mud", "clay", "gravel"}
    invalid     = [b for b in np.unique(bank_surf) if b not in valid_bank]
    assert not invalid, f"unexpected bank surface blocks: {invalid}"

    # Ground cover on sand dune (non-river) should only be dry-desert flora
    dune_mask  = (biome_grid == "SAND_DUNE_DESERT") & (river_meta == 0)
    dune_cover = np.unique(cover[dune_mask])
    valid_dune = {"", "dead_bush", "short_dry_grass", "tall_dry_grass", "cactus"}
    bad_cover  = [c for c in dune_cover if c not in valid_dune]
    assert not bad_cover, f"unexpected dune ground cover: {bad_cover}"

    print(f"  [legacy] surface blocks : {sorted(set(surf.flat))[:10]} ...")
    print(f"  [legacy] ground cover   : {sorted(set(cover.flat))}")

    # ---- Test 2: Eco path (with eco_grads) ----
    import os, sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from core.eco_gradients import compute_eco_gradients, compute_cliff_deg

    # Build cliff_deg from surface_y gradient (smoothed to avoid staircase aliasing)
    cliff_deg = compute_cliff_deg(surface_y)
    land_mask = surface_y >= 63

    eco_grads = compute_eco_gradients(
        surface_y   = surface_y,
        flow_f      = flow_tile,
        erosion_f   = erosion_tile,
        cliff_deg   = cliff_deg,
        hydro_order = np.zeros((H, W), dtype=np.float32),
        hydro_width = np.zeros((H, W), dtype=np.float32),
        hydro_lake  = np.zeros((H, W), dtype=np.float32),
        land_mask   = land_mask,
        cfg         = cfg,
    )

    surf2, sub2, cover2 = decorate_surface(
        surface_y, biome_grid,
        erosion_tile, moisture_tile, height_tile,
        river_meta, flow_tile,
        noise_fields, cfg,
        tile_x, tile_y,
        eco_grads=eco_grads,
        cliff_deg=cliff_deg,
    )

    assert surf2.shape  == (H, W), "eco surface shape mismatch"
    assert sub2.shape   == (H, W), "eco subsurface shape mismatch"
    assert cover2.shape == (H, W), "eco ground cover shape mismatch"

    # Eco path should produce different block distribution than legacy
    eco_unique = set(surf2.flat)
    print(f"  [eco] surface blocks    : {sorted(eco_unique)[:10]} ...")
    print(f"  [eco] ground cover      : {sorted(set(cover2.flat))}")
    print(f"  bank pixels             : {np.sum(bank_pixels)}")
    print(f"  ground cover placed     : {np.sum(cover2 != '')}")

    # ---- Test 3: noise_layers_biome path ----------------------------------
    cfg3 = dict(cfg)
    cfg3["noise_layers_biome"] = {
        "MIXED_FOREST": [
            {"name": "podzol scatter", "noise": "simplex", "enabled": True,
             "block": "podzol", "sub": "rooted_dirt", "coverage": 0.35,
             "scale": 48, "seed": 42, "is_base": False},
            {"name": "grass_block", "noise": "simplex", "enabled": True,
             "block": "grass_block", "sub": "dirt", "coverage": 1.0,
             "scale": 60, "seed": 42, "is_base": True},
        ],
        "ARCTIC_TUNDRA": [
            {"name": "snow_block", "noise": "simplex_fbm", "enabled": True,
             "block": "snow_block", "sub": "stone", "coverage": 1.0,
             "scale": 60, "seed": 42, "is_base": True},
            {"name": "gravel", "noise": "simplex_fbm", "enabled": True,
             "block": "gravel", "sub": "stone", "coverage": 0.20,
             "scale": 15, "seed": 243, "is_base": False},
        ],
    }

    surf3, sub3, cover3 = decorate_surface(
        surface_y, biome_grid,
        erosion_tile, moisture_tile, height_tile,
        river_meta, flow_tile,
        noise_fields, cfg3,
        tile_x, tile_y,
        eco_grads=eco_grads,
        cliff_deg=cliff_deg,
    )

    assert surf3.shape == (H, W), "noise_layers surface shape mismatch"

    # MIXED_FOREST should have grass_block (base) + podzol (layer)
    mf_mask = (biome_grid == "MIXED_FOREST") & (river_meta == 0)
    mf_blocks = set(surf3[mf_mask].flat)
    assert "grass_block" in mf_blocks, f"Missing grass_block base in noise_layers: {mf_blocks}"
    assert "podzol" in mf_blocks, f"Missing podzol layer in noise_layers: {mf_blocks}"

    # ARCTIC_TUNDRA should have snow_block (base) + gravel (layer)
    at_mask = (biome_grid == "ARCTIC_TUNDRA") & (river_meta == 0)
    at_blocks = set(surf3[at_mask].flat)
    assert "snow_block" in at_blocks, f"Missing snow_block base: {at_blocks}"

    nl_unique = sorted(set(surf3.flat))
    print(f"  [noise_layers] blocks   : {nl_unique[:10]} ...")
    print(f"  [noise_layers] MF       : {sorted(mf_blocks)}")
    print(f"  [noise_layers] AT       : {sorted(at_blocks)}")
    print("  [noise_layers] OK")

    sys.exit(0)